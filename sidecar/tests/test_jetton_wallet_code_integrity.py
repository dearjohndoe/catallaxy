"""Integrity test for `USDT_JETTON_WALLET_CODE_HEX`.

Re-fetches the live wallet code from USDT mainnet master via TonAPI HTTP
and compares to our hardcoded constant. If Tether ever changes it, this
test fails — and we update the constant + redeploy before the prod
sidecars start computing wrong jetton-wallet addresses.

Marked as `network` so CI can opt out when offline.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from jetton import USDT_JETTON_WALLET_CODE_HEX, USDT_MASTER_MAINNET
from tonutils.contracts.jetton.master import JettonMasterStablecoin


# A known agent wallet whose live jetton wallet address is recorded below.
# Pinned to ensure offline derivation is byte-identical to the chain.
_PINNED_AGENT_WALLET = "UQDtBt_JMwcrLDkRP04dcEeMHIQY8srlbHgs3UcQKELYdq8h"
_PINNED_JETTON_WALLET = "UQDVVDfSx5gdXWzlabsVCudoJW8cy9TVHE8cxsKpOviYWsmy"


@pytest.mark.network
def test_constant_matches_live_chain():
    """Live USDT master.get_jetton_data().jetton_wallet_code == our constant."""
    url = f"https://tonapi.io/v2/blockchain/accounts/{USDT_MASTER_MAINNET}/methods/get_jetton_data"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError) as e:
        pytest.skip(f"TonAPI unreachable: {e}")

    # stack[4] is jetton_wallet_code per TEP-74 get_jetton_data signature
    live_code = data["stack"][4]["cell"]
    assert live_code == USDT_JETTON_WALLET_CODE_HEX, (
        "USDT_JETTON_WALLET_CODE_HEX is stale. "
        "Live wallet code from chain:\n  " + live_code +
        "\nUpdate the constant in sidecar/jetton.py and redeploy. "
        "Without this, JettonPaymentVerifier will compute wrong jetton "
        "wallet addresses for all USDT-rail agents."
    )


def test_offline_derivation_matches_pinned_address():
    """`calculate_user_jetton_wallet_address` with our constant produces the
    correct address for a known agent — purely offline assertion, no network."""
    addr = JettonMasterStablecoin.calculate_user_jetton_wallet_address(
        owner_address=_PINNED_AGENT_WALLET,
        jetton_master_address=USDT_MASTER_MAINNET,
        jetton_wallet_code=USDT_JETTON_WALLET_CODE_HEX,
    )
    got = addr.to_str(is_user_friendly=True, is_bounceable=False)
    assert got == _PINNED_JETTON_WALLET, (
        f"offline derivation regressed: got {got}, expected {_PINNED_JETTON_WALLET}"
    )
