from __future__ import annotations

from dataclasses import dataclass

from pytoniq_core import Transaction


class PaymentVerificationError(Exception):
    pass


@dataclass
class VerifiedPayment:
    tx_hash: str
    sender: str
    recipient: str
    amount: int
    comment: str


UNDERPAID_MESSAGE = "Transaction amount is lower than required price"


class UnderpaidPayment(PaymentVerificationError):
    """A payment for this nonce is on-chain but below the price.

    Unlike other rejections the money is real and the sender known, so the
    caller refunds it instead of just answering 402.
    """

    def __init__(self, payment: VerifiedPayment, required: int) -> None:
        super().__init__(UNDERPAID_MESSAGE)
        self.payment = payment
        self.required = required


def underpaid_error(
    *, tx_hash: str, sender: str, recipient: str, amount: int, comment: str, required: int,
) -> PaymentVerificationError:
    """``UnderpaidPayment`` when there is something to refund, else a plain rejection."""
    if amount <= 0 or not sender:
        return PaymentVerificationError(UNDERPAID_MESSAGE)
    return UnderpaidPayment(
        VerifiedPayment(tx_hash=tx_hash, sender=sender, recipient=recipient,
                        amount=amount, comment=comment),
        required,
    )


@dataclass
class NonceMeta:
    value: str


@dataclass
class JettonPaymentTx:
    tx: Transaction
    amount: int       # jetton base units (e.g. micro-USDT)
    sender: str       # original sender from transfer_notification body
    nonce: str
