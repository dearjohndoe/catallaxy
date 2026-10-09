"""Tests for jetton.py — transfer_notification parsing & jetton transfer body building."""

from __future__ import annotations

import pytest
from pytoniq_core import Address, Cell, begin_cell

from chains.ton.jetton import (
    JETTON_TRANSFER_OPCODE,
    TRANSFER_NOTIFICATION_OPCODE,
    JettonNotification,
    jetton_transfer_body,
    parse_transfer_notification,
)
from chains.ton.transfer import PAYMENT_OPCODE


def _build_notification(
    query_id: int = 0,
    amount: int = 1_000_000,
    sender: str = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
    forward_payload: Cell | None = None,
    inline_payload: bool = False,
) -> Cell:
    """Build a valid transfer_notification cell."""
    b = (
        begin_cell()
        .store_uint(TRANSFER_NOTIFICATION_OPCODE, 32)
        .store_uint(query_id, 64)
        .store_coins(amount)
        .store_address(sender)
    )
    if forward_payload is not None:
        if inline_payload:
            # Either bit = 0 → inline
            b.store_bit(0)
            b.store_slice(forward_payload.begin_parse())
        else:
            # Either bit = 1 → reference
            b.store_bit(1)
            b.store_ref(forward_payload)
    else:
        b.store_bit(0)  # empty inline
    return b.end_cell()


def _nonce_payload(nonce: str) -> Cell:
    """Build a forward_payload with PAYMENT_OPCODE + nonce (same as TON rail)."""
    return (
        begin_cell()
        .store_uint(PAYMENT_OPCODE, 32)
        .store_snake_string(nonce)
        .end_cell()
    )


# ── parse_transfer_notification ──────────────────────────────────────


class TestParseTransferNotification:
    def test_valid_notification_ref_payload(self):
        nonce = "abc123:sid-test"
        fwd = _nonce_payload(nonce)
        cell = _build_notification(query_id=42, amount=5_000_000, forward_payload=fwd)
        result = parse_transfer_notification(cell)
        assert result is not None
        assert result.query_id == 42
        assert result.amount == 5_000_000
        assert result.forward_payload is not None

    def test_valid_notification_inline_payload(self):
        fwd = _nonce_payload("inline:sid-test")
        cell = _build_notification(forward_payload=fwd, inline_payload=True)
        result = parse_transfer_notification(cell)
        assert result is not None
        assert result.amount == 1_000_000
        assert result.forward_payload is not None

    def test_no_forward_payload(self):
        cell = _build_notification(forward_payload=None)
        result = parse_transfer_notification(cell)
        assert result is not None
        assert result.forward_payload is None

    def test_wrong_opcode_returns_none(self):
        cell = (
            begin_cell()
            .store_uint(0xDEADBEEF, 32)
            .store_uint(0, 64)
            .store_coins(100)
            .store_address("EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs")
            .store_bit(0)
            .end_cell()
        )
        assert parse_transfer_notification(cell) is None

    def test_truncated_body_returns_none(self):
        cell = begin_cell().store_uint(TRANSFER_NOTIFICATION_OPCODE, 32).end_cell()
        assert parse_transfer_notification(cell) is None

    def test_none_body_returns_none(self):
        assert parse_transfer_notification(None) is None

    def test_empty_cell_returns_none(self):
        assert parse_transfer_notification(begin_cell().end_cell()) is None

    def test_sender_extracted_correctly(self):
        addr = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
        cell = _build_notification(sender=addr)
        result = parse_transfer_notification(cell)
        assert result is not None
        assert result.sender  # non-empty


# ── jetton_transfer_body ─────────────────────────────────────────────


class TestJettonTransferBody:
    def test_roundtrip_opcode(self):
        cell = jetton_transfer_body(
            destination="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
            amount=1_000_000,
            response_destination="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        )
        s = cell.begin_parse()
        assert s.load_uint(32) == JETTON_TRANSFER_OPCODE

    def test_amount_stored(self):
        cell = jetton_transfer_body(
            destination="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
            amount=2_500_000,
            response_destination="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        )
        s = cell.begin_parse()
        s.load_uint(32)   # opcode
        s.load_uint(64)   # query_id
        amount = s.load_coins()
        assert amount == 2_500_000

    def test_with_forward_payload(self):
        fwd = _nonce_payload("refund:sid-test")
        cell = jetton_transfer_body(
            destination="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
            amount=1_000,
            response_destination="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
            forward_payload=fwd,
            forward_ton_amount=50_000_000,
        )
        s = cell.begin_parse()
        s.load_uint(32)    # opcode
        s.load_uint(64)    # query_id
        s.load_coins()     # amount
        s.load_address()   # destination
        s.load_address()   # response_destination
        s.load_bit()       # custom_payload (0)
        fwd_ton = s.load_coins()
        assert fwd_ton == 50_000_000
        assert s.load_bit() == 1  # forward_payload as ref
        ref = s.load_ref()
        ref_s = ref.begin_parse()
        assert ref_s.load_uint(32) == PAYMENT_OPCODE

    def test_without_forward_payload(self):
        cell = jetton_transfer_body(
            destination="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
            amount=1_000,
            response_destination="EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs",
        )
        s = cell.begin_parse()
        s.load_uint(32)    # opcode
        s.load_uint(64)    # query_id
        s.load_coins()     # amount
        s.load_address()   # destination
        s.load_address()   # response_destination
        s.load_bit()       # custom_payload (0)
        s.load_coins()     # forward_ton_amount
        assert s.load_bit() == 0  # empty inline


def test_derive_agent_jetton_wallet_custom_code_differs_from_tether():
    from tonutils.contracts.jetton.wallet import JettonWalletStandard
    from chains.ton.jetton import derive_agent_jetton_wallet, USDT_MASTER_MAINNET

    owner = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
    tether = derive_agent_jetton_wallet(owner, USDT_MASTER_MAINNET, None)
    std_code = JettonWalletStandard.get_default_code().to_boc().hex()
    custom = derive_agent_jetton_wallet(owner, USDT_MASTER_MAINNET, std_code)
    assert tether.startswith("UQ") or tether.startswith("EQ")
    assert custom != tether
