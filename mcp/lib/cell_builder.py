"""Reuses payment_body() from sidecar/chains/ton/transfer.py."""
import base64
import os
import sys

# Add sidecar to path so we can import the TON rail modules. They live under
# chains/ton/ since the multichain step-0 refactor (sidecar/chains/ton/).
_SIDECAR = os.path.join(os.path.dirname(__file__), "..", "..", "sidecar")
if _SIDECAR not in sys.path:
    sys.path.insert(0, _SIDECAR)

from chains.ton.transfer import payment_body  # noqa: E402
from chains.ton.jetton import jetton_transfer_body  # noqa: E402


def build_payment_cell(nonce: str) -> tuple[str, str]:
    """Build TON payment Cell with nonce. Returns (base64_boc, hex_boc)."""
    boc = payment_body(nonce).to_boc()
    return base64.b64encode(boc).decode(), boc.hex()


def build_jetton_transfer_cell(
    agent_address: str,
    usdt_amount: int,
    nonce: str,
    response_destination: str,
    forward_ton_amount: int = 1,
) -> tuple[str, str]:
    """Build jetton transfer Cell for USDT payment.

    Send this Cell + attached_ton (≥0.06 TON) to your own USDT jetton wallet.
    Returns (base64_boc, hex_boc).
    """
    forward_payload = payment_body(nonce)
    cell = jetton_transfer_body(
        destination=agent_address,
        amount=usdt_amount,
        response_destination=response_destination,
        forward_payload=forward_payload,
        forward_ton_amount=forward_ton_amount,
    )
    boc = cell.to_boc()
    return base64.b64encode(boc).decode(), boc.hex()
