"""Chain-agnostic payment + discovery interfaces.

A *rail* is a ``(chain, asset)`` pair identified by ``rail_id`` — e.g. "TON",
"USDT" (both on the TON chain), later "sol"/"sol:usdc". An agent advertises any
set of rails independently of which registries it heartbeats into.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from payments.types import VerifiedPayment


# Persisted identifiers: processed_txs records both ``{chain}:{tx_hash}``
# (canonical after verify) and ``{chain}:pub:{pub}`` (claim-block identity).
# refund_queue PK and the refund memo's "tx" field are ``{chain}:pub:{pub}``.
# Both TON rails settle on "ton".
_RAIL_TO_CHAIN = {"TON": "ton", "USDT": "ton"}


def chain_for_rail(rail_id: str) -> str:
    """Chain a rail settles on (e.g. "TON"/"USDT" → "ton")."""
    return _RAIL_TO_CHAIN.get(rail_id, rail_id.lower())


def namespaced_tx_key(chain: str, tx_hash: str) -> str:
    """``{chain}:{tx_hash}``. Idempotent: a value already containing ``:`` (bare
    TON hashes / Solana sigs never do) is returned unchanged, so wrapping twice
    is a safe no-op."""
    if ":" in tx_hash:
        return tx_hash
    return f"{chain}:{tx_hash}"


def namespaced_pub_key(chain: str, pub: str) -> str:
    """Payment identity before the on-chain hash is known: ``{chain}:pub:{pub}``.

    Distinct from ``namespaced_tx_key`` so an 8-hex client-supplied ``tx`` cannot
    alias a split-nonce ``pub``. Idempotent on values that already contain
    ``:pub:``.
    """
    if ":pub:" in pub:
        return pub
    return f"{chain}:pub:{pub}"


@runtime_checkable
class ChainRail(Protocol):
    """One payment rail: verify an incoming payment, refund it, describe itself
    in a 402 response, and report monitor freshness for the health gate."""

    #: Stable rail identifier, e.g. "TON", "USDT", "sol:usdc".
    rail_id: str

    async def verify(self, proof: str, nonce: str, min_amount: int) -> VerifiedPayment:
        """Confirm an on-chain payment for ``nonce`` of at least ``min_amount``.

        ``proof`` is rail-specific: a TON tx hash, a Solana signature, or the
        contents of an x402 ``X-PAYMENT`` header. Returns the verified payment
        (with the *real* on-chain tx hash, not the caller-supplied one) or
        raises ``PaymentVerificationError`` on a definitive on-chain rejection.
        """
        ...

    async def refund(
        self, to: str, amount: int, *, original_tx_hash: str, reason: str,
    ) -> str | None:
        """Send ``amount`` (rail base units, pre-fee) back to ``to``.

        Returns the refund tx id, or ``None`` when the refund is skipped
        (amount ≤ fee) or the send fails — best-effort, never raises.
        ``original_tx_hash``/``reason`` are stamped into the refund body so a
        later worker can dedup via an on-chain scan.
        """
        ...

    def payment_option(self, amount: int, nonce: str) -> dict[str, Any]:
        """Build this rail's slice of a 402 ``payment_options`` entry
        (``rail``/``address``/``amount``/``memo`` and, for jettons, ``token``).
        The caller stitches in cross-rail fields like ``sku``.
        """
        ...

    def monitor_healthy(self, max_age_seconds: float = 60.0) -> bool:
        """True iff this rail's monitor had a fresh successful poll within
        ``max_age_seconds`` — gates whether we advertise the rail."""
        ...


@runtime_checkable
class ChainRegistry(Protocol):
    """Publishes heartbeats so the agent is discoverable on a chain's registry."""

    async def publish_heartbeat(self, payload: dict[str, Any]) -> str:
        """Publish ``payload`` to the registry; returns the registry tx id."""
        ...
