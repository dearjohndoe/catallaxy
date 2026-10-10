"""SVM ``exact`` scheme: inspect a partial tx, co-sign as fee payer, publish.

Verification is local (instruction whitelist). We are the fee payer — no
facilitator. See x402 ``scheme_exact_svm.md``.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass
from typing import Any

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.token.associated import get_associated_token_address
from solders.transaction import VersionedTransaction

from chains.base import namespaced_pub_key
from chains.solana.constants import (
    COMPUTE_BUDGET_PROGRAM,
    CU_LIMIT_DISC,
    CU_PRICE_DISC,
    LIGHTHOUSE_PROGRAM,
    MAX_CU_LIMIT,
    MAX_CU_PRICE_MICROLAMPORTS,
    MAX_PRIORITY_FEE_LAMPORTS,
    MEMO_PROGRAM,
    SETTLE_BUSY_RETRY_SECONDS,
    SETTLE_CACHE_TTL_SECONDS,
    SETTLE_IP_FAILURE_WINDOW_SECONDS,
    SETTLE_IP_MAX_FAILURES,
    SETTLE_SOURCE_COOLDOWN_SECONDS,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    TRANSFER_CHECKED_DISC,
)
from chains.solana.rpc import SolanaRpc, SolanaRpcError
from chains.solana.x402 import (
    X402ParseError,
    decode_transaction_bytes,
    network_matches_cluster,
    parse_payment_payload,
)
from payments.types import PaymentVerificationError, VerifiedPayment

logger = logging.getLogger("sidecar")

_TOKEN_PROGRAMS = frozenset({TOKEN_PROGRAM, TOKEN_2022_PROGRAM})
_OPTIONAL_PROGRAMS = frozenset({LIGHTHOUSE_PROGRAM, MEMO_PROGRAM})


@dataclass(frozen=True)
class InspectedExactPayment:
    tx: VersionedTransaction
    raw: bytes
    sender: str
    amount: int
    mint: str
    source: str
    dest_ata: str
    client_signature: str
    identity: str
    signing_payload: bytes


def _is_v0(tx: VersionedTransaction) -> bool:
    return tx.version() == 0


def signing_payload(tx: VersionedTransaction) -> bytes:
    body = bytes(tx.message)
    if _is_v0(tx):
        return b"\x80" + body
    return body


def _parse_tx(raw: bytes) -> VersionedTransaction:
    try:
        return VersionedTransaction.from_bytes(raw)
    except Exception as exc:
        raise PaymentVerificationError("invalid solana transaction") from exc


def _program_id(tx: VersionedTransaction, program_index: int) -> Pubkey:
    keys = tx.message.account_keys
    if program_index < 0 or program_index >= len(keys):
        raise PaymentVerificationError("instruction program_id out of range")
    return keys[program_index]


def _ix_accounts(tx: VersionedTransaction, indexes: Any) -> list[Pubkey]:
    keys = tx.message.account_keys
    out: list[Pubkey] = []
    for i in indexes:
        if i < 0 or i >= len(keys):
            raise PaymentVerificationError("instruction account out of range")
        out.append(keys[i])
    return out


def _decode_cu_price(data: bytes) -> int:
    if len(data) < 9 or data[0] != CU_PRICE_DISC:
        raise PaymentVerificationError("invalid compute unit price instruction")
    return int(struct.unpack_from("<Q", data, 1)[0])


def _decode_cu_limit(data: bytes) -> int:
    if len(data) < 5 or data[0] != CU_LIMIT_DISC:
        raise PaymentVerificationError("invalid compute unit limit instruction")
    return int(struct.unpack_from("<I", data, 1)[0])


def _decode_transfer_checked(data: bytes) -> tuple[int, int]:
    if len(data) < 10 or data[0] != TRANSFER_CHECKED_DISC:
        raise PaymentVerificationError("expected TransferChecked")
    amount = int(struct.unpack_from("<Q", data, 1)[0])
    decimals = data[9]
    return amount, decimals


def inspect_exact(
    proof: str,
    *,
    min_amount: int,
    pay_to: Pubkey,
    mint: Pubkey,
    fee_payer: Pubkey,
    cluster: str,
) -> InspectedExactPayment:
    """Decode + whitelist a partial SVM exact payload. Does not publish."""
    try:
        payload = parse_payment_payload(proof)
        raw = decode_transaction_bytes(payload.transaction_b64)
    except X402ParseError as exc:
        raise PaymentVerificationError(str(exc)) from exc
    if payload.network and not network_matches_cluster(payload.network, cluster):
        raise PaymentVerificationError(
            f"x402 network {payload.network!r} does not match cluster {cluster}"
        )

    tx = _parse_tx(raw)
    if tx.message.address_table_lookups:
        raise PaymentVerificationError("address lookup tables are not allowed")

    keys = list(tx.message.account_keys)
    if not keys:
        raise PaymentVerificationError("transaction has no accounts")
    if keys[0] != fee_payer:
        raise PaymentVerificationError("fee payer mismatch")

    ixs = list(tx.message.instructions)
    if not (3 <= len(ixs) <= 6):
        raise PaymentVerificationError("exact svm tx must have 3–6 instructions")

    # (1) CU limit (2) CU price (3) TransferChecked (4–6) lighthouse/memo
    p0 = _program_id(tx, ixs[0].program_id_index)
    p1 = _program_id(tx, ixs[1].program_id_index)
    if p0 != COMPUTE_BUDGET_PROGRAM or p1 != COMPUTE_BUDGET_PROGRAM:
        raise PaymentVerificationError("first two instructions must be compute budget")
    d0 = bytes(ixs[0].data)
    d1 = bytes(ixs[1].data)
    if not d0 or d0[0] != CU_LIMIT_DISC:
        raise PaymentVerificationError("instruction 0 must be SetComputeUnitLimit")
    if not d1 or d1[0] != CU_PRICE_DISC:
        raise PaymentVerificationError("instruction 1 must be SetComputeUnitPrice")
    cu_price = _decode_cu_price(d1)
    if cu_price > MAX_CU_PRICE_MICROLAMPORTS:
        raise PaymentVerificationError(
            f"compute unit price exceeds cap ({cu_price} > {MAX_CU_PRICE_MICROLAMPORTS} µlamports)"
        )
    # We are the fee payer: bound what the client can make us spend.
    cu_limit = _decode_cu_limit(d0)
    if cu_limit > MAX_CU_LIMIT:
        raise PaymentVerificationError(f"compute unit limit exceeds cap ({cu_limit} > {MAX_CU_LIMIT})")
    priority_fee = -(-cu_limit * cu_price // 1_000_000)  # runtime rounds up
    if priority_fee > MAX_PRIORITY_FEE_LAMPORTS:
        raise PaymentVerificationError(
            f"priority fee exceeds cap ({priority_fee} > {MAX_PRIORITY_FEE_LAMPORTS} lamports)"
        )

    p2 = _program_id(tx, ixs[2].program_id_index)
    if p2 not in _TOKEN_PROGRAMS:
        raise PaymentVerificationError("instruction 2 must be TransferChecked")
    amount, decimals = _decode_transfer_checked(bytes(ixs[2].data))
    if decimals != 6:
        raise PaymentVerificationError("USDC TransferChecked decimals must be 6")
    if amount != min_amount:
        raise PaymentVerificationError(
            f"amount mismatch: got {amount}, required {min_amount}"
        )
    accs = _ix_accounts(tx, ixs[2].accounts)
    if len(accs) < 4:
        raise PaymentVerificationError("TransferChecked missing accounts")
    source, ix_mint, dest, authority = accs[0], accs[1], accs[2], accs[3]
    if ix_mint != mint:
        raise PaymentVerificationError("wrong token mint")
    expected_dest = get_associated_token_address(pay_to, mint, p2)
    if dest != expected_dest:
        raise PaymentVerificationError("destination is not seller USDC ATA")

    for extra in ixs[3:]:
        prog = _program_id(tx, extra.program_id_index)
        if prog not in _OPTIONAL_PROGRAMS:
            raise PaymentVerificationError("disallowed extra instruction")

    # Fee payer must not appear in any instruction account (sponsor safety).
    for ix in ixs:
        for acc in _ix_accounts(tx, ix.accounts):
            if acc == fee_payer:
                raise PaymentVerificationError("fee payer appears in instruction accounts")
    if authority == fee_payer or source == fee_payer:
        raise PaymentVerificationError("fee payer must not be transfer authority or source")

    sig_payload = signing_payload(tx)
    n_req = tx.message.header.num_required_signatures
    sigs = list(tx.signatures)
    if len(sigs) < n_req:
        raise PaymentVerificationError("missing signatures array")

    client_sig: Signature | None = None
    # Required signers are account_keys[0:n_req]; index 0 is fee payer.
    for i in range(1, n_req):
        sig = sigs[i]
        if sig == Signature.default():
            continue
        signer = keys[i]
        if not sig.verify(signer, sig_payload):
            raise PaymentVerificationError("invalid client signature")
        if signer == authority:
            client_sig = sig
    if client_sig is None:
        raise PaymentVerificationError("transfer authority has not signed")
    if sigs[0] != Signature.default() and sigs[0].verify(fee_payer, sig_payload):
        # Already fully signed by us — still acceptable (retry after our sign
        # but before land). Don't reject.
        pass
    elif sigs[0] != Signature.default():
        raise PaymentVerificationError("fee payer slot has a foreign signature")

    identity = namespaced_pub_key("solana", str(client_sig))
    return InspectedExactPayment(
        tx=tx,
        raw=raw,
        sender=str(authority),
        amount=amount,
        mint=str(mint),
        source=str(source),
        dest_ata=str(dest),
        client_signature=str(client_sig),
        identity=identity,
        signing_payload=sig_payload,
    )


class SettleThrottled(PaymentVerificationError):
    """Settle refused before broadcast; the client may retry after ``retry_after``."""

    def __init__(self, reason: str, retry_after: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = max(1, retry_after)


class SettlementFailedOnChain(PaymentVerificationError):
    """Broadcast went out, then the tx failed or never confirmed: we may have paid the fee."""


class ExactSettler:
    """Co-sign as fee payer and publish.

    In-process guards: a duplicate-settlement cache, one settle in flight per
    source token account, a cooldown on that account after an on-chain failure,
    and a cap on on-chain failures per client (IP). See constants.py.
    """

    def __init__(
        self,
        *,
        keypair: Keypair,
        rpc: SolanaRpc,
        source_cooldown_seconds: float = SETTLE_SOURCE_COOLDOWN_SECONDS,
        client_max_failures: int = SETTLE_IP_MAX_FAILURES,
        client_failure_window_seconds: float = SETTLE_IP_FAILURE_WINDOW_SECONDS,
    ) -> None:
        self._kp = keypair
        self._rpc = rpc
        self._inflight: dict[str, float] = {}
        self._source_busy: set[str] = set()
        self._source_cooldown: dict[str, float] = {}
        self._client_failures: dict[str, list[float]] = {}
        self._source_cooldown_seconds = source_cooldown_seconds
        self._client_max_failures = client_max_failures
        self._client_failure_window = client_failure_window_seconds

    def _check_throttles(self, source: str, client_key: str | None, now: float) -> None:
        if source in self._source_busy:
            raise SettleThrottled("settle_in_progress", SETTLE_BUSY_RETRY_SECONDS)
        until = self._source_cooldown.get(source, 0.0)
        if until > now:
            raise SettleThrottled("source_cooldown", int(until - now) + 1)
        self._source_cooldown.pop(source, None)
        if client_key and self._client_max_failures > 0:
            cutoff = now - self._client_failure_window
            history = [t for t in self._client_failures.get(client_key, ()) if t > cutoff]
            if history:
                self._client_failures[client_key] = history
            else:
                self._client_failures.pop(client_key, None)
            if len(history) >= self._client_max_failures:
                raise SettleThrottled(
                    "too_many_failed_settlements", int(history[0] - cutoff) + 1,
                )

    def _record_onchain_failure(self, source: str, client_key: str | None, now: float) -> None:
        if self._source_cooldown_seconds > 0:
            self._source_cooldown[source] = now + self._source_cooldown_seconds
        if client_key:
            self._client_failures.setdefault(client_key, []).append(now)
        # Bound memory against rotating sources/IPs.
        for key, until in list(self._source_cooldown.items()):
            if until <= now:
                self._source_cooldown.pop(key, None)
        cutoff = now - self._client_failure_window
        for key, history in list(self._client_failures.items()):
            if not history or history[-1] <= cutoff:
                self._client_failures.pop(key, None)

    def _cache_ok(self, identity: str) -> bool:
        now = time.time()
        stale = [k for k, t in self._inflight.items() if now - t > SETTLE_CACHE_TTL_SECONDS]
        for k in stale:
            self._inflight.pop(k, None)
        if identity in self._inflight:
            return False
        self._inflight[identity] = now
        return True

    async def settle(
        self, inspected: InspectedExactPayment, *, client_key: str | None = None,
    ) -> VerifiedPayment:
        source = inspected.source
        self._check_throttles(source, client_key, time.time())
        if not self._cache_ok(inspected.identity):
            raise PaymentVerificationError("duplicate_settlement")
        self._source_busy.add(source)
        try:
            return await self._settle(inspected)
        except SettlementFailedOnChain:
            self._inflight.pop(inspected.identity, None)
            self._record_onchain_failure(source, client_key, time.time())
            logger.warning(
                "USDC settle failed after broadcast: source=%s client=%s", source, client_key,
            )
            raise
        except Exception:
            self._inflight.pop(inspected.identity, None)
            raise
        finally:
            self._source_busy.discard(source)

    async def _settle(self, inspected: InspectedExactPayment) -> VerifiedPayment:
        tx = inspected.tx
        sigs = list(tx.signatures)
        n_req = tx.message.header.num_required_signatures
        our_sig = self._kp.sign_message(inspected.signing_payload)
        if not our_sig.verify(self._kp.pubkey(), inspected.signing_payload):
            raise PaymentVerificationError("fee payer sign failed")
        sigs[0] = our_sig
        if len(sigs) < n_req:
            raise PaymentVerificationError("signatures truncated")
        signed = VersionedTransaction.populate(tx.message, sigs)
        import base64
        tx_b64 = base64.b64encode(bytes(signed)).decode("ascii")
        try:
            signature = await self._rpc.send_transaction(tx_b64)
        except SolanaRpcError as exc:
            raise PaymentVerificationError(f"settle send failed: {exc}") from exc
        await self._wait_confirmed(signature)
        return VerifiedPayment(
            tx_hash=signature,
            sender=inspected.sender,
            recipient=str(self._kp.pubkey()),
            amount=inspected.amount,
            comment=inspected.client_signature,
        )

    async def _wait_confirmed(self, signature: str, *, attempts: int = 20) -> None:
        import asyncio
        for _ in range(attempts):
            try:
                status = await self._rpc.signature_status(signature)
            except SolanaRpcError:
                status = None
            if status is not None:
                if status.get("err"):
                    raise SettlementFailedOnChain(f"settlement failed on-chain: {status['err']}")
                conf = status.get("confirmationStatus") or ""
                # Not `confirmations is not None`: that is set already at
                # "processed", which one RPC node alone has seen.
                if conf in ("confirmed", "finalized"):
                    return
            await asyncio.sleep(0.4)
        raise SettlementFailedOnChain("settlement not confirmed in time")
