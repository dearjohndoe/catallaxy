from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pytoniq_core import Address, Cell, begin_cell

TRANSFER_NOTIFICATION_OPCODE = 0x7362D09C
JETTON_TRANSFER_OPCODE = 0x0F8A7EA5

USDT_MASTER_MAINNET = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
USDT_MASTER_TESTNET = "kQD0GKBM8ZbryVk2aESmzfU6b9b_8era_IkvBSELujFZPsyy"

USDT_JETTON_WALLET_CODE_HEX = (
    "b5ee9c72010101010023000842028f452d7a4dfd74066b682365177259ed057344"
    "35be76b5fd4bd5d8af2b7c3d68"
)

# Refund fee in micro-USDT (6 decimals). Jetton refund gas is paid in TONs, 
# but we need to reserve some USDT to cover the refund fee when refunding jettons.
USDT_REFUND_FEE = 100_000


@dataclass
class JettonNotification:
    query_id: int
    amount: int        # jetton base units (micro-USDT for USDT)
    sender: str        # original sender address from notification body
    forward_payload: Cell | None


def parse_transfer_notification(body: Any) -> JettonNotification | None:
    """Parse a transfer_notification message body (opcode 0x7362d09c).

    Returns None if the body is not a valid transfer_notification.
    The forward_payload follows TEP-74 Either Cell ^Cell encoding.
    """
    if body is None:
        return None
    try:
        s = body.begin_parse()
        if s.remaining_bits < 32:
            return None
        opcode = s.load_uint(32)
        if opcode != TRANSFER_NOTIFICATION_OPCODE:
            return None

        query_id = s.load_uint(64)
        amount = s.load_coins()
        sender = s.load_address().to_str(is_user_friendly=True, is_bounceable=False)

        # forward_payload: Either Cell ^Cell
        # bit 0 = inline (remaining bits in current cell)
        # bit 1 = reference (next ref)
        forward_payload: Cell | None = None
        if s.remaining_bits > 0:
            if s.load_bit():
                # Referenced payload
                if s.remaining_refs > 0:
                    forward_payload = s.load_ref()
            else:
                # Inline payload — collect remaining bits as a cell
                if s.remaining_bits > 0 or s.remaining_refs > 0:
                    b = begin_cell()
                    b.store_slice(s)
                    forward_payload = b.end_cell()

        return JettonNotification(
            query_id=query_id,
            amount=amount,
            sender=sender,
            forward_payload=forward_payload,
        )
    except Exception:
        return None


def jetton_transfer_body(
    destination: str,
    amount: int,
    response_destination: str,
    forward_payload: Cell | None = None,
    forward_ton_amount: int = 1,
) -> Cell:
    """Build a jetton transfer message body (opcode 0x0f8a7ea5).

    Sent to agent's own jetton wallet to transfer jettons to `destination`.
    """
    b = (
        begin_cell()
        .store_uint(JETTON_TRANSFER_OPCODE, 32)
        .store_uint(0, 64)                          # query_id
        .store_coins(amount)
        .store_address(destination)
        .store_address(response_destination)         # excess TON returns here
        .store_bit(0)                                # no custom_payload
        .store_coins(forward_ton_amount)
    )
    if forward_payload is not None:
        b.store_bit(1)                               # forward_payload as reference
        b.store_ref(forward_payload)
    else:
        b.store_bit(0)                               # empty inline payload
    return b.end_cell()
