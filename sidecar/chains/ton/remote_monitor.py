"""Remote wallet monitor — thin HTTP client to tonapi-relay.

Used by `PaymentVerifier` / `JettonPaymentVerifier` when env var
`MONITOR_SERVICE_URL` is set. The relay receives TonAPI webhooks
and stores tx history; this client just looks up by nonce.

Interface mirrors `WalletMonitor` / `JettonWalletMonitor` so the
verifiers don't need rail-specific branches: `get`, `consume`, `force`,
`is_healthy`, `start`, `stop`, `replace_client` (no-op).
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


# Health-check cache: don't hit /health on every is_healthy() call,
# verify() may call it inline. Refreshed lazily by an async helper.
# Unhealthy is cached briefly so a missed refresh cannot latch 503.
_HEALTH_CHECK_INTERVAL = 30.0
_UNHEALTHY_CHECK_INTERVAL = 2.0

# Startup race: sidecar often wins vs tonapi-relay. Retry this long
# during start(), then keep trying in the background.
_SUBSCRIBE_STARTUP_BUDGET = 45.0
_SUBSCRIBE_RETRY_INITIAL = 0.5
_SUBSCRIBE_RETRY_MAX = 5.0


class _RetryableSubscribeError(Exception):
    """Connection / 5xx — worth retrying."""


class _RelayClient:
    """Shared aiohttp session + tonapi-relay endpoint URLs."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        *,
        subscribe_budget: float = _SUBSCRIBE_STARTUP_BUDGET,
        subscribe_initial_delay: float = _SUBSCRIBE_RETRY_INITIAL,
        subscribe_max_delay: float = _SUBSCRIBE_RETRY_MAX,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self._subscribe_budget = subscribe_budget
        self._subscribe_initial_delay = subscribe_initial_delay
        self._subscribe_max_delay = subscribe_max_delay
        self._subscribed = False
        self._closed = False
        self._resubscribe_task: Optional[asyncio.Task[None]] = None

    @property
    def is_subscribed(self) -> bool:
        return self._subscribed

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        self._closed = True
        task = self._resubscribe_task
        self._resubscribe_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        s = self._session
        self._session = None
        if s is not None and not s.closed:
            await s.close()

    async def _reset_session(self) -> None:
        """Drop the current session so the next call reconnects.

        A session whose underlying keep-alive connection went stale is NOT
        reported as ``closed``, so ``_ensure`` would otherwise keep reusing a
        dead session forever — every request then times out and the remote
        monitor latches unhealthy with no way to self-heal. Resetting here
        forces ``_ensure`` to build a fresh session on the next call.
        """
        s = self._session
        self._session = None
        if s is not None and not s.closed:
            try:
                await s.close()
            except Exception:
                pass

    async def fetch_by_nonce(self, nonce: str, rail: str) -> Optional[dict[str, Any]]:
        s = await self._ensure()
        try:
            async with s.get(
                f"{self._base}/tx/by_nonce",
                params={"nonce": nonce, "rail": rail},
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    logger.warning("relay /tx/by_nonce HTTP %d: %s", resp.status, body)
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("relay /tx/by_nonce error: %s", e)
            await self._reset_session()
            return None

    async def _subscribe_once(
        self,
        agent_wallet: Optional[str],
        jetton_wallet: Optional[str],
        label: Optional[str],
    ) -> dict[str, Any]:
        s = await self._ensure()
        body = {"agent_wallet": agent_wallet, "jetton_wallet": jetton_wallet, "label": label}
        try:
            async with s.post(f"{self._base}/subscribe", json=body) as resp:
                if resp.status >= 500:
                    text = (await resp.text())[:200]
                    raise _RetryableSubscribeError(
                        f"relay /subscribe HTTP {resp.status}: {text}"
                    )
                if resp.status >= 400:
                    text = (await resp.text())[:200]
                    raise RuntimeError(f"relay /subscribe HTTP {resp.status}: {text}")
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise _RetryableSubscribeError(str(e)) from e

    async def subscribe(
        self,
        agent_wallet: Optional[str],
        jetton_wallet: Optional[str],
        label: Optional[str],
        *,
        budget: Optional[float] = None,
        initial_delay: Optional[float] = None,
        max_delay: Optional[float] = None,
    ) -> dict[str, Any]:
        """POST /subscribe, retrying connection errors and 5xx until budget.

        First successful attempt returns immediately (no extra delay).
        Raises the last error if the budget is exhausted. HTTP 4xx is
        not retried — it fails this call on the first response.
        """
        budget = self._subscribe_budget if budget is None else budget
        delay = self._subscribe_initial_delay if initial_delay is None else initial_delay
        max_delay = self._subscribe_max_delay if max_delay is None else max_delay
        deadline = time.monotonic() + budget
        last_error: Optional[BaseException] = None
        attempt = 0
        while True:
            if self._closed:
                raise RuntimeError("relay client closed")
            attempt += 1
            try:
                result = await self._subscribe_once(agent_wallet, jetton_wallet, label)
                self._subscribed = True
                return result
            except _RetryableSubscribeError as e:
                last_error = e
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sleep_for = min(delay, remaining)
                logger.warning(
                    "relay /subscribe attempt %d failed: %s; retry in %.1fs",
                    attempt, e, sleep_for,
                )
                await self._reset_session()
                await asyncio.sleep(sleep_for)
                delay = min(delay * 2, max_delay)
            except Exception:
                await self._reset_session()
                raise
        assert last_error is not None
        raise last_error

    async def subscribe_or_keep_trying(
        self,
        agent_wallet: Optional[str],
        jetton_wallet: Optional[str],
        label: Optional[str],
    ) -> None:
        """Subscribe during start(); on failure, retry in the background.

        Does not raise after the startup budget — paid rails stay 503
        until subscribe succeeds, instead of leaving the verifier unstarted.
        """
        try:
            await self.subscribe(agent_wallet, jetton_wallet, label)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "relay /subscribe failed after %.0fs; retrying in background",
                self._subscribe_budget,
                exc_info=True,
            )
        self._start_resubscribe_loop(agent_wallet, jetton_wallet, label)

    def _start_resubscribe_loop(
        self,
        agent_wallet: Optional[str],
        jetton_wallet: Optional[str],
        label: Optional[str],
    ) -> None:
        if self._subscribed or self._closed:
            return
        pending = self._resubscribe_task
        if pending is not None and not pending.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("relay /subscribe: no event loop for background retry")
            return
        self._resubscribe_task = loop.create_task(
            self._resubscribe_loop(agent_wallet, jetton_wallet, label),
            name="relay-resubscribe",
        )
        self._resubscribe_task.add_done_callback(self._on_resubscribe_done)

    def _on_resubscribe_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("relay /subscribe background retry died: %s", exc)

    async def _resubscribe_loop(
        self,
        agent_wallet: Optional[str],
        jetton_wallet: Optional[str],
        label: Optional[str],
    ) -> None:
        delay = self._subscribe_initial_delay
        while not self._closed and not self._subscribed:
            await asyncio.sleep(delay)
            if self._closed or self._subscribed:
                return
            try:
                await self._reset_session()
                await self._subscribe_once(agent_wallet, jetton_wallet, label)
                self._subscribed = True
                logger.info("relay /subscribe recovered")
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("relay /subscribe background retry failed: %s", e)
                delay = min(delay * 2, self._subscribe_max_delay)

    async def health(self) -> Optional[dict[str, Any]]:
        s = await self._ensure()
        try:
            async with s.get(f"{self._base}/health") as resp:
                if resp.status >= 400:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("relay /health error: %s", e)
            await self._reset_session()
            return None


def _wrap_ton_tx(data: dict[str, Any]) -> SimpleNamespace:
    """Build a duck-typed Transaction matching what PaymentVerifier.verify() reads.

    Required fields: tx.now, tx.in_msg.info.src.to_str(), tx.in_msg.info.value.grams,
    tx.in_msg.body (used by _parse_payment_nonce — None is fine, parser returns ""),
    tx.cell.hash (bytes; verify calls `.hex()`).
    """
    sender = data.get("sender") or ""
    amount = int(data.get("amount") or 0)
    utime = int(data.get("utime") or 0)
    tx_hash_hex = data.get("tx_hash") or ""
    try:
        tx_hash_bytes = bytes.fromhex(tx_hash_hex)
    except ValueError:
        tx_hash_bytes = b""
    return SimpleNamespace(
        now=utime,
        in_msg=SimpleNamespace(
            info=SimpleNamespace(
                src=SimpleNamespace(
                    to_str=lambda *_, **__: sender,
                ),
                value=SimpleNamespace(grams=amount),
            ),
            body=None,
        ),
        cell=SimpleNamespace(hash=tx_hash_bytes),
    )


def _wrap_jetton_entry(data: dict[str, Any]) -> Any:
    """Reconstruct a JettonPaymentTx from the relay's flat row."""
    from payments.types import JettonPaymentTx
    utime = int(data.get("utime") or 0)
    tx_hash_hex = data.get("tx_hash") or ""
    try:
        tx_hash_bytes = bytes.fromhex(tx_hash_hex)
    except ValueError:
        tx_hash_bytes = b""
    tx_wrapper = SimpleNamespace(
        now=utime,
        cell=SimpleNamespace(hash=tx_hash_bytes),
    )
    return JettonPaymentTx(
        tx=tx_wrapper,  # type: ignore[arg-type]
        amount=int(data.get("amount") or 0),
        sender=str(data.get("sender") or ""),
        nonce=str(data.get("nonce") or ""),
    )


class _BaseRemoteMonitor:
    """Common scaffolding for TON and Jetton remote monitors."""
    RAIL: str = "TON"

    def __init__(
        self,
        relay: _RelayClient,
        account_id: str,
        label: Optional[str] = None,
    ) -> None:
        self._relay = relay
        self._account_id = account_id
        self._label = label
        # Local cache. Filled by get(); cleared by consume() *after* a durable
        # mark/enqueue. Replay protection lives in `tx_store.is_processed`.
        self._by_nonce: dict[str, Any] = {}
        self._last_successful_poll_at: float = 0.0
        # Cached health (avoid hammering /health on every is_healthy call)
        self._health_cache: tuple[float, bool] = (0.0, True)
        self._pending_refresh: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        # Subscription is performed at the verifier level so it can pass both
        # agent_wallet and jetton_wallet in a single /subscribe call.
        # Subclasses may override if they want to self-subscribe.
        pass

    async def stop(self) -> None:
        pending = self._pending_refresh
        self._pending_refresh = None
        if pending is not None and not pending.done():
            pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, Exception):
                pass

    async def replace_client(self, client: Any) -> None:
        # No LiteBalancer to replace.
        return

    def force(self) -> None:
        # Relay polls TonAPI on its own schedule; we cannot push.
        return

    def is_healthy(self, max_age_seconds: float = 120.0) -> bool:
        """Best-effort. Returns cached True unless /health was recently checked
        and reported staleness beyond max_age_seconds. We deliberately allow
        verify() to attempt anyway — if relay is slow but not dead, the
        per-call 3x retry covers most cases.

        Until /subscribe succeeds, always False — otherwise preflight would
        402 before the relay is watching this wallet.
        """
        if getattr(self._relay, "is_subscribed", True) is False:
            return False
        cached_at, cached_ok = self._health_cache
        ttl = _HEALTH_CHECK_INTERVAL if cached_ok else _UNHEALTHY_CHECK_INTERVAL
        if time.time() - cached_at < ttl:
            return cached_ok
        self._schedule_health_refresh(max_age_seconds)
        return cached_ok

    def _schedule_health_refresh(self, max_age_seconds: float) -> None:
        pending = self._pending_refresh
        if pending is not None and not pending.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._refresh_health(max_age_seconds),
            name="relay-health-refresh",
        )
        self._pending_refresh = task
        task.add_done_callback(self._on_health_refresh_done)

    def _on_health_refresh_done(self, task: asyncio.Task[None]) -> None:
        if self._pending_refresh is task:
            self._pending_refresh = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("remote monitor health refresh failed: %s", exc)

    async def _refresh_health(self, max_age_seconds: float) -> None:
        """Remote-relay semantics for `is_healthy`:

        - Relay unreachable → unhealthy (can't verify anything).
        - Relay alive + at least one initial sync completed → healthy.
          Even if `last_webhook_at` is null and `last_sync_age_sec` is high
          (e.g. between 10-min sync cycles), the relay can still pick up the
          user's tx via webhook within seconds or via next sync ≤10 min.
        - Relay alive but sync never ran → unhealthy (no catch-up channel yet).

        The `max_age_seconds` param is kept for interface compatibility with
        local WalletMonitor but isn't applied here — remote means we trust
        the relay's own cadence, not absolute recency of a specific source.
        """
        try:
            info = await self._relay.health()
            if info is None:
                # The failed call above reset the session; retry once on a fresh
                # connection so a single stale keep-alive doesn't latch unhealthy.
                info = await self._relay.health()
            if info is None:
                self._health_cache = (time.time(), False)
                return
            last_sync_at = info.get("last_sync_at") or 0
            ok = bool(last_sync_at and last_sync_at > 0)
            self._health_cache = (time.time(), ok)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("remote monitor health refresh error")
            self._health_cache = (time.time(), False)

    async def get(self, nonce: str) -> Optional[Any]:
        """Single-shot lookup against the relay.

        No internal retry/sleep: the caller (`PaymentVerifier.verify`) already
        loops to its own deadline polling every VERIFY_POLL (~0.5s), so a tx
        gets picked up within ~0.5s of landing in the relay instead of being
        gated by a coarse multi-second internal retry. One retry layer, one
        place that owns the timeout.
        """
        nonce = nonce.strip()
        if nonce in self._by_nonce:
            return self._by_nonce[nonce]
        data = await self._relay.fetch_by_nonce(nonce, self.RAIL)
        if data is not None:
            self._last_successful_poll_at = time.time()
            wrapped = self._wrap(data)
            self._by_nonce[nonce] = wrapped
            return wrapped
        return None

    async def consume(self, nonce: str) -> Optional[Any]:
        return self._by_nonce.pop(nonce.strip(), None)

    def _wrap(self, data: dict[str, Any]) -> Any:
        raise NotImplementedError


class RemoteWalletMonitor(_BaseRemoteMonitor):
    """TON-rail remote monitor."""
    RAIL = "TON"

    def _wrap(self, data: dict[str, Any]) -> Any:
        return _wrap_ton_tx(data)


class RemoteJettonWalletMonitor(_BaseRemoteMonitor):
    """USDT-rail remote monitor."""
    RAIL = "USDT"

    def _wrap(self, data: dict[str, Any]) -> Any:
        return _wrap_jetton_entry(data)


# Module-level helpers -------------------------------------------------------


def get_relay_url() -> Optional[str]:
    """Read MONITOR_SERVICE_URL env. Empty / unset means 'don't use remote'."""
    import os
    url = os.environ.get("MONITOR_SERVICE_URL", "").strip()
    return url or None
