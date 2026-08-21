from __future__ import annotations

import uuid
from typing import Any

from chains.ton.transfer import PAYMENT_OPCODE

from .types import NonceMeta

# Wire nonce shape (PROTOCOL.md §5): "{16 hex}:{sidecar_id}", where the 16
# hex chars split into pub(8) + sec(8) (v2 split-nonce, see
# TODO-claim-auth.md). `pub` is the on-chain correlator; `sec` is a
# capability secret that must never be written on-chain.
_HEX_LEN = 16
_PUB_LEN = 8


def parse_nonce(raw_nonce: str) -> NonceMeta:
    return NonceMeta(value=raw_nonce.strip())


def _join_pub_nonce(pub: str, sidecar_id: str) -> str:
    """Single source of truth for the on-chain ``pub:sidecar_id`` wire form."""
    return f"{pub}:{sidecar_id}"


def mint_nonce(sidecar_id: str) -> tuple[str, str, str, str]:
    """Mint a fresh split nonce for a 402 response.

    Returns ``(pub, sec, pub_nonce, full)``:
      - ``pub``  — 8 hex chars, the on-chain correlator.
      - ``sec``  — 8 hex chars, the capability secret. Travels only in the
        402 JSON body (``payment_options[].nonce``), never on-chain.
      - ``pub_nonce`` — ``f"{pub}:{sidecar_id}"``, what
        ``ChainRail.payment_option()`` embeds in the memo/cell.
      - ``full`` — ``f"{pub}{sec}:{sidecar_id}"``, the wire-format claim
        nonce (``{16 hex}:{sidecar_id}``) the client presents back at claim
        time (``payment_options[].nonce``, distinct from ``memo``).
    """
    raw = uuid.uuid4().hex[:_HEX_LEN]
    pub, sec = raw[:_PUB_LEN], raw[_PUB_LEN:]
    return pub, sec, _join_pub_nonce(pub, sidecar_id), f"{raw}:{sidecar_id}"


def split_full_nonce(full_nonce: str) -> tuple[str, str, str] | None:
    """Split a client-presented full claim nonce into ``(pub, pub_nonce, sec)``.

    ``full_nonce`` is the wire-format value from ``POST /invoke {nonce}``:
    ``{16 hex}:{sidecar_id}``. ``pub_nonce = f"{pub}:{sidecar_id}"`` is the
    public-only value actually embedded on-chain — what the chain verifier
    must key its lookup on and what ``claim_secrets`` is keyed by. ``sec`` is
    the raw 8-hex secret half, to be hash-compared against the stored claim
    secret.

    Never raises: any malformed input (wrong length, missing ``:sidecar_id``
    suffix, non-hex payload) returns ``None`` so callers can reject cleanly
    with a 403 instead of an unhandled exception turning into a 500.
    """
    if not full_nonce:
        return None
    hex_part, sep, suffix = full_nonce.partition(":")
    if not sep or not suffix:
        return None
    if len(hex_part) != _HEX_LEN:
        return None
    try:
        int(hex_part, 16)
    except ValueError:
        return None
    pub, sec = hex_part[:_PUB_LEN], hex_part[_PUB_LEN:]
    return pub, _join_pub_nonce(pub, suffix), sec


def _parse_payment_nonce(body: Any) -> str:
    if body is None:
        return ""
    try:
        s = body.begin_parse()
        if s.remaining_bits < 32:
            return ""
        if s.load_uint(32) != PAYMENT_OPCODE:
            return ""
        return s.load_snake_string()
    except Exception:
        return ""
