"""x402 HTTP envelope: v1/v2 payload parse and 402 header encode."""

from __future__ import annotations

import base64
import json

import pytest

from chains.solana.x402 import (
    X402ParseError,
    encode_payment_required_header,
    exact_requirements,
    extract_header_proof,
    parse_payment_payload,
    payment_required_body,
)


def _b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def test_parse_v2_payload_transaction_field():
    raw = _b64({
        "x402Version": 2,
        "scheme": "exact",
        "network": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
        "payload": {"transaction": "AAAA"},
    })
    p = parse_payment_payload(raw)
    assert p.version == 2
    assert p.scheme == "exact"
    assert p.transaction_b64 == "AAAA"


def test_parse_v1_serialized_transaction_alias():
    raw = _b64({
        "x402Version": 1,
        "scheme": "exact",
        "network": "solana-devnet",
        "payload": {"serializedTransaction": "BBBB"},
    })
    p = parse_payment_payload(raw)
    assert p.transaction_b64 == "BBBB"
    assert p.network == "solana-devnet"


def test_parse_raw_json_without_base64():
    p = parse_payment_payload(json.dumps({
        "x402Version": 2, "scheme": "exact",
        "payload": {"transaction": "CCCC"},
    }))
    assert p.transaction_b64 == "CCCC"


def test_parse_rejects_wrong_scheme():
    with pytest.raises(X402ParseError, match="scheme"):
        parse_payment_payload(_b64({
            "x402Version": 2, "scheme": "upto",
            "payload": {"transaction": "AA"},
        }))


def test_parse_rejects_missing_tx():
    with pytest.raises(X402ParseError, match="missing transaction"):
        parse_payment_payload(_b64({"x402Version": 2, "payload": {}}))


def test_extract_header_prefers_v2():
    assert extract_header_proof({
        "PAYMENT-SIGNATURE": "aaa",
        "X-PAYMENT": "bbb",
    }) == "aaa"
    assert extract_header_proof({"X-Payment": "bbb"}) == "bbb"
    assert extract_header_proof({}) is None


def test_payment_required_header_roundtrip():
    req = exact_requirements(
        amount=10_000, pay_to="PayTo111", asset="Mint111",
        fee_payer="Fee111", cluster="devnet",
        resource_url="https://agent.test/invoke",
    )
    body = payment_required_body([req], resource_url="https://agent.test/invoke")
    header = encode_payment_required_header(body)
    decoded = json.loads(base64.b64decode(header))
    assert decoded["x402Version"] == 2
    assert decoded["accepts"][0]["scheme"] == "exact"
    assert decoded["accepts"][0]["extra"]["feePayer"] == "Fee111"
    assert decoded["accepts"][0]["amount"] == "10000"
