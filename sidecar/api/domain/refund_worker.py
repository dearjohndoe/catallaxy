from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, AsyncIterator

from payments import PendingRefund

from api.domain.refund import find_existing_refund_tx, refund_user

if TYPE_CHECKING:
    from api.app import SidecarApp

logger = logging.getLogger("sidecar")


# Exponential backoff in seconds: 30s, 1m, 2m, 5m, 15m, 30m, 1h, 4h, 12h, 24h
_BACKOFF_SCHEDULE = (30, 60, 120, 300, 900, 1800, 3600, 14400, 43200, 86400)


def _backoff_for_attempt(attempts: int) -> int:
    idx = min(max(attempts - 1, 0), len(_BACKOFF_SCHEDULE) - 1)
    return _BACKOFF_SCHEDULE[idx]


async def refund_worker_loop(app: "SidecarApp") -> None:
    """Periodically drain the pending-refund queue.

    Runs as a background task. Exits when ``app.stop_event`` is set.
    """
    interval = max(app.settings.refund_worker_interval, 5)
    try:
        await _recover_stale_refunding(app, older_than_seconds=600)
    except Exception:
        logger.exception("refund_worker: stale recovery failed")

    while not app.stop_event.is_set():
        try:
            await asyncio.wait_for(app.stop_event.wait(), timeout=interval)
            return  # stop_event set
        except asyncio.TimeoutError:
            pass
        try:
            await _tick(app)
        except Exception:
            logger.exception("refund_worker tick failed")


@contextlib.asynccontextmanager
async def _acquire_lite_client(app: "SidecarApp") -> AsyncIterator[object]:
    """Yield a LiteBalancer for on-chain probes. Reuses the TON verifier's
    client when available, otherwise opens an ad-hoc one and closes it on exit.
    """
    if app.verifier is not None and app.verifier._client is not None:
        yield app.verifier._client
        return
    from tonutils.clients import LiteBalancer
    from tonutils.types import NetworkGlobalID
    network = NetworkGlobalID.TESTNET if app.settings.testnet else NetworkGlobalID.MAINNET
    client = LiteBalancer.from_network_config(network)
    await client.connect()
    try:
        yield client
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def _recover_stale_refunding(app: "SidecarApp", older_than_seconds: int) -> None:
    """Resolve entries stuck in 'refunding' since before a crash/restart.

    For each stale entry, probe the chain for a matching refund tx. If found,
    the previous worker did send the refund — record the hash and finish.
    Otherwise revert to 'pending' for retry. Replaces a blind revert that
    could cause double-refunds.
    """
    stale = await app.refund_queue.list_stale_refunding(older_than_seconds)
    if not stale:
        return
    logger.warning("refund_worker: recovering %d stale 'refunding' entries", len(stale))

    async with _acquire_lite_client(app) as client:
        for entry in stale:
            try:
                found = await find_existing_refund_tx(
                    client=client,
                    agent_wallet=app.settings.agent_wallet,
                    rail=entry.rail,
                    original_tx_hash=entry.tx_hash,
                    sidecar_id=app.sidecar_id,
                )
            except Exception:
                logger.exception(
                    "refund_worker: probe failed during stale recovery tx=%s", entry.tx_hash,
                )
                found = None

            if found:
                await app.refund_queue.mark_refunded(entry.tx_hash, found)
                logger.warning(
                    "refund_worker: stale entry already refunded on-chain tx=%s refund_tx=%s",
                    entry.tx_hash, found,
                )
            else:
                await app.refund_queue.mark_failed_transient(
                    entry.tx_hash,
                    "recovered from stale 'refunding' (no on-chain refund found)",
                    backoff_seconds=_backoff_for_attempt(entry.attempts),
                )
                logger.warning(
                    "refund_worker: stale entry reverted to pending tx=%s attempts=%d",
                    entry.tx_hash, entry.attempts,
                )


async def _tick(app: "SidecarApp") -> None:
    due = await app.refund_queue.fetch_due(limit=10)
    if not due:
        return
    for entry in due:
        try:
            await _process_entry(app, entry)
        except Exception:
            logger.exception(
                "refund_worker: unexpected error processing tx=%s", entry.tx_hash,
            )


async def _process_entry(app: "SidecarApp", entry: PendingRefund) -> None:
    """Resolve, claim, and refund a single pending entry."""
    # Race-guard: if the same tx already succeeded via /invoke after enqueue,
    # don't refund — mark as processed and skip. Skip the guard when
    # ``force_refund`` is set: those entries come from paths where
    # mark_processed already ran but service was NOT delivered.
    if not entry.force_refund:
        try:
            if await app.tx_store.is_processed(entry.tx_hash):
                await app.refund_queue.mark_processed(entry.tx_hash)
                logger.info(
                    "refund_worker: tx already processed, skipping refund tx=%s",
                    entry.tx_hash,
                )
                return
        except Exception:
            logger.exception("refund_worker: tx_store.is_processed check failed")

    # Dedup-on-retry: if a prior attempt already sent a refund (e.g., crash
    # between send() and mark_refunded()), pick up its on-chain hash instead
    # of sending again. Skipped on the very first attempt — nothing to dedup
    # against, and probing wastes one RPC per refund.
    if entry.attempts >= 1 and entry.sender and entry.amount is not None:
        try:
            async with _acquire_lite_client(app) as client:
                existing = await find_existing_refund_tx(
                    client=client,
                    agent_wallet=app.settings.agent_wallet,
                    rail=entry.rail,
                    original_tx_hash=entry.tx_hash,
                    sidecar_id=app.sidecar_id,
                )
        except Exception:
            logger.exception("refund_worker: pre-send dedup probe failed tx=%s", entry.tx_hash)
            existing = None
        if existing:
            await app.refund_queue.mark_refunded(entry.tx_hash, existing)
            logger.warning(
                "refund_worker: refund already on-chain from prior attempt tx=%s refund_tx=%s",
                entry.tx_hash, existing,
            )
            return

    # Permanent give-up after too many attempts. Ops can resurrect manually.
    if entry.attempts >= app.settings.refund_max_attempts:
        await app.refund_queue.mark_failed_permanent(
            entry.tx_hash,
            f"max_attempts_exceeded ({entry.attempts}); last_error={entry.last_error or 'n/a'}",
        )
        logger.critical(
            "refund_worker: permanent failure tx=%s nonce=%s rail=%s — manual intervention required",
            entry.tx_hash, entry.nonce, entry.rail,
        )
        return

    # If sender/amount unknown, try to recover from on-chain monitor.
    if not entry.sender or entry.amount is None:
        recovered = await _recover_payment_info(app, entry)
        if not recovered:
            await app.refund_queue.mark_failed_transient(
                entry.tx_hash,
                "could not recover sender/amount from on-chain monitor",
                _backoff_for_attempt(entry.attempts + 1),
            )
            return
        entry = await app.refund_queue.get(entry.tx_hash) or entry
        if not entry.sender or entry.amount is None:
            return

    # Sanity check: agent has enough balance to cover the refund.
    sufficient, balance_err = await _check_balance_for_refund(app, entry)
    if not sufficient:
        await app.refund_queue.mark_failed_transient(
            entry.tx_hash,
            f"balance_check_failed: {balance_err}",
            _backoff_for_attempt(entry.attempts + 1),
        )
        logger.warning(
            "refund_worker: balance check failed tx=%s reason=%s",
            entry.tx_hash, balance_err,
        )
        return

    # Atomically claim the entry. Lose the race → another worker handles it.
    if not await app.refund_queue.claim(entry.tx_hash):
        return

    try:
        refund_tx = await refund_user(
            sender=app.sender,
            agent_jetton_wallet=app._agent_jetton_wallet,
            sidecar_id=app.sidecar_id,
            refund_fee_nanoton=app.settings.refund_fee_nanoton,
            recipient=entry.sender,
            payment_amount=entry.amount,
            original_tx_hash=entry.tx_hash,
            reason="verifier_unavailable",
            rail=entry.rail,
        )
    except Exception as exc:
        await app.refund_queue.mark_failed_transient(
            entry.tx_hash,
            f"send error: {exc!r}",
            _backoff_for_attempt(entry.attempts),
        )
        logger.exception("refund_worker: send_jetton/send failed tx=%s", entry.tx_hash)
        return

    if refund_tx:
        await app.refund_queue.mark_refunded(entry.tx_hash, refund_tx)
        logger.info(
            "refund_worker: refund sent tx=%s refund_tx=%s rail=%s amount=%s recipient=%s",
            entry.tx_hash, refund_tx, entry.rail, entry.amount, entry.sender,
        )
    else:
        # refund_user returned None — usually amount-too-small-after-fee. Permanent.
        await app.refund_queue.mark_failed_permanent(
            entry.tx_hash, "refund_user returned None (likely amount-after-fee <= 0)",
        )


async def _recover_payment_info(app: "SidecarApp", entry: PendingRefund) -> bool:
    """Use the appropriate monitor to find sender + amount for a known nonce."""
    if entry.rail == "USDT":
        ok = await app.ensure_jetton_verifier()
        if not ok or app.jetton_verifier is None or app.jetton_verifier._monitor is None:
            return False
        monitor = app.jetton_verifier._monitor
        # Force a fresh poll to pick up the tx if monitor missed it.
        monitor.force()
        await asyncio.sleep(2)
        from payments import parse_nonce
        nonce_value = parse_nonce(entry.nonce).value
        cached = monitor.get(nonce_value)
        if cached is None:
            return False
        await app.refund_queue.update_payment_info(
            entry.tx_hash, cached.sender, cached.amount,
        )
        return True

    if entry.rail == "TON":
        if app.verifier is None or app.verifier._monitor is None:
            return False
        monitor = app.verifier._monitor
        monitor.force()
        await asyncio.sleep(2)
        from payments import parse_nonce
        nonce_value = parse_nonce(entry.nonce).value
        tx = monitor.get(nonce_value)
        if tx is None:
            return False
        try:
            sender = tx.in_msg.info.src.to_str(is_user_friendly=True, is_bounceable=False)
            amount = int(tx.in_msg.info.value.grams)
        except Exception:
            return False
        await app.refund_queue.update_payment_info(entry.tx_hash, sender, amount)
        return True

    return False


async def _check_balance_for_refund(
    app: "SidecarApp", entry: PendingRefund,
) -> tuple[bool, str]:
    """Defense-in-depth: ensure agent's wallet can actually pay the refund.

    Doesn't prevent double-refund on its own (the SQL state machine does that),
    but catches the case where prior refunds drained the wallet — and avoids
    sending into a known failure.
    """
    if entry.amount is None:
        return False, "amount unknown"
    try:
        from tonutils.clients import LiteBalancer
        from tonutils.types import NetworkGlobalID

        network = NetworkGlobalID.TESTNET if app.settings.testnet else NetworkGlobalID.MAINNET
        client = LiteBalancer.from_network_config(network)
        await client.connect()
        try:
            if entry.rail == "USDT":
                if not app._agent_jetton_wallet:
                    return False, "agent_jetton_wallet unknown"
                from tonutils.contracts.jetton.wallet import JettonWalletStablecoin
                wallet = await JettonWalletStablecoin.from_address(client, app._agent_jetton_wallet)
                balance = int(wallet.jetton_balance)
                from jetton import USDT_REFUND_FEE
                required = max(entry.amount - USDT_REFUND_FEE, 0)
                if balance < required:
                    return False, f"jetton balance {balance} < required {required}"
                return True, ""

            # TON rail
            account, _ = await client.get_account_state(app.settings.agent_wallet)
            if account is None:
                return False, "agent account state not found"
            balance = int(account.storage.balance.grams)
            required = entry.amount  # outgoing tx pays its own fees from the value
            if balance < required:
                return False, f"TON balance {balance} < required {required}"
            return True, ""
        finally:
            try:
                await client.close()
            except Exception:
                pass
    except Exception as exc:
        return False, f"balance probe error: {exc!r}"
