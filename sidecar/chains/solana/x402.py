"""x402 HTTP envelope — isolated from Solana instruction checks.

v1: ``X-PAYMENT`` header, body ``accepts[]``.
v2: ``PAYMENT-SIGNATURE`` / ``PAYMENT-REQUIRED``, CAIP-2 networks.

Payload for SVM exact is a base64 partially-signed tx in
``payload.transaction`` (v2) or ``payload.serializedTransaction`` (v1).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping

from chains.solana.constants import (
    CAIP2_DEVNET,
    CAIP2_MAINNET,
    USDC_MINT_DEVNET,
    USDC_MINT_MAINNET,
    V1_NETWORK_DEVNET,
    V1_NETWORK_MAINNET,
)


class X402ParseError(ValueError):
    pass


@dataclass(frozen=True)
class X402PaymentPayload:
    version: int
    scheme: str
    network: str
    transaction_b64: str


def cluster_caip2(cluster: str) -> str:
    return CAIP2_DEVNET if cluster == "devnet" else CAIP2_MAINNET


def cluster_usdc_mint(cluster: str) -> str:
    return USDC_MINT_DEVNET if cluster == "devnet" else USDC_MINT_MAINNET


def network_matches_cluster(network: str, cluster: str) -> bool:
    n = (network or "").strip()
    if cluster == "devnet":
        return n in V1_NETWORK_DEVNET
    return n in V1_NETWORK_MAINNET


def _b64json_decode(raw: str) -> Any:
    text = (raw or "").strip()
    if not text:
        raise X402ParseError("empty x402 payload")
    # Clients send base64(JSON). Also accept raw JSON for tests / CTLX proof.
    if text[:1] in "{[":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise X402ParseError("invalid x402 json") from exc
    pad = "=" * (-len(text) % 4)
    try:
        decoded = base64.b64decode(text + pad, validate=False)
        return json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise X402ParseError("invalid x402 base64 payload") from exc


def parse_payment_payload(raw: str) -> X402PaymentPayload:
    """Decode an ``X-PAYMENT`` / ``PAYMENT-SIGNATURE`` value."""
    data = _b64json_decode(raw)
    if not isinstance(data, dict):
        raise X402ParseError("x402 payload must be an object")
    version = int(data.get("x402Version") or data.get("x402_version") or 1)
    scheme = str(data.get("scheme") or (data.get("accepted") or {}).get("scheme") or "")
    network = str(
        data.get("network")
        or (data.get("accepted") or {}).get("network")
        or ""
    )
    payload = data.get("payload") or {}
    if not isinstance(payload, dict):
        raise X402ParseError("x402 payload.payload must be an object")
    tx_b64 = str(
        payload.get("transaction")
        or payload.get("serializedTransaction")
        or ""
    ).strip()
    if not tx_b64:
        raise X402ParseError("x402 payload missing transaction")
    if scheme and scheme != "exact":
        raise X402ParseError(f"unsupported x402 scheme {scheme!r}")
    return X402PaymentPayload(
        version=version,
        scheme=scheme or "exact",
        network=network,
        transaction_b64=tx_b64,
    )


def decode_transaction_bytes(tx_b64: str) -> bytes:
    pad = "=" * (-len(tx_b64) % 4)
    try:
        raw = base64.b64decode(tx_b64 + pad, validate=False)
    except Exception as exc:
        raise X402ParseError("transaction is not base64") from exc
    if not raw:
        raise X402ParseError("empty transaction")
    if len(raw) > 1232:
        raise X402ParseError("transaction exceeds 1232 bytes")
    return raw


def exact_requirements(
    *,
    amount: int,
    pay_to: str,
    asset: str,
    fee_payer: str,
    cluster: str,
    resource_url: str,
    description: str = "",
    mime_type: str = "application/json",
    max_timeout_seconds: int = 60,
) -> dict[str, Any]:
    """One ``accepts[]`` entry for SVM exact USDC."""
    return {
        "scheme": "exact",
        "network": cluster_caip2(cluster),
        "amount": str(amount),
        "maxAmountRequired": str(amount),  # v1 alias
        "asset": asset,
        "payTo": pay_to,
        "maxTimeoutSeconds": max_timeout_seconds,
        "resource": resource_url,
        "description": description,
        "mimeType": mime_type,
        "extra": {"feePayer": fee_payer},
    }


def payment_required_body(
    accepts: list[dict[str, Any]],
    *,
    resource_url: str,
    description: str = "",
    mime_type: str = "application/json",
) -> dict[str, Any]:
    return {
        "x402Version": 2,
        "error": "PAYMENT_REQUIRED",
        "resource": {
            "url": resource_url,
            "description": description,
            "mimeType": mime_type,
        },
        "accepts": accepts,
    }


def encode_payment_required_header(body: Mapping[str, Any]) -> str:
    return base64.b64encode(
        json.dumps(body, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def extract_header_proof(headers: Mapping[str, str]) -> str | None:
    """Read v2 then v1 payment proof headers. Header names are case-insensitive
    on aiohttp; this helper also works with a plain dict in tests."""
    if not headers:
        return None
    lower = {str(k).lower(): v for k, v in headers.items()}
    for name in ("payment-signature", "x-payment"):
        val = (lower.get(name) or "").strip()
        if val:
            return val
    return None
