"""HTTP broadcast of signed external messages: toncenter first, then TonAPI.

Why not the liteserver: public liteservers answer `sendMessage` with "ok" right
after emulation, but the actual rebroadcast into the public shard overlay is
asynchronous and best-effort — its failure is only visible in the node's own
log. Since early Sep 2026 almost nothing sent that way reaches a block, while
the very same BoC posted to toncenter lands in seconds. Also, a byte-identical
`sendMessage` is answered from the liteserver's cache as "ok" without being
rebroadcast, so retrying the same BoC through it is a no-op.

Env:
  TONCENTER_API_KEY — optional; without it toncenter allows ~1 RPS per IP.
  TONAPI_KEY / TONAPI_BASE — shared with payments/tonapi_client.py.
"""
from __future__ import annotations

import base64
import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0

TONCENTER_BASE_MAINNET = "https://toncenter.com"
TONCENTER_BASE_TESTNET = "https://testnet.toncenter.com"
TONAPI_BASE_MAINNET = "https://tonapi.io"
TONAPI_BASE_TESTNET = "https://testnet.tonapi.io"


class BroadcastError(Exception):
    pass


class HttpBroadcaster:
    def __init__(self, testnet: bool = False, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._toncenter_base = TONCENTER_BASE_TESTNET if testnet else TONCENTER_BASE_MAINNET
        self._toncenter_key = os.environ.get("TONCENTER_API_KEY") or None
        default_tonapi = TONAPI_BASE_TESTNET if testnet else TONAPI_BASE_MAINNET
        self._tonapi_base = (os.environ.get("TONAPI_BASE") or default_tonapi).rstrip("/")
        self._tonapi_key = os.environ.get("TONAPI_KEY") or None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def send_boc(self, boc: bytes) -> str:
        """Post the BoC to toncenter, falling back to TonAPI.

        Returns the name of the provider that accepted it. Raises
        BroadcastError if both rejected it.
        """
        boc_b64 = base64.b64encode(boc).decode()
        errors: list[str] = []
        for name, post in (("toncenter", self._toncenter), ("tonapi", self._tonapi)):
            try:
                await post(boc_b64)
                return name
            except Exception as exc:
                logger.warning("Broadcast via %s failed: %s", name, exc)
                errors.append(f"{name}: {exc}")
        raise BroadcastError("; ".join(errors))

    async def _toncenter(self, boc_b64: str) -> None:
        headers = {"X-API-Key": self._toncenter_key} if self._toncenter_key else {}
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(
                f"{self._toncenter_base}/api/v2/sendBoc",
                json={"boc": boc_b64},
                headers=headers,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise BroadcastError(f"HTTP {resp.status}: {text[:200]}")
                try:
                    ok = (await resp.json(content_type=None)).get("ok")
                except Exception:
                    ok = None
                if ok is not True:
                    raise BroadcastError(f"not ok: {text[:200]}")

    async def _tonapi(self, boc_b64: str) -> None:
        headers = {"Authorization": f"Bearer {self._tonapi_key}"} if self._tonapi_key else {}
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(
                f"{self._tonapi_base}/v2/blockchain/message",
                json={"boc": boc_b64},
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise BroadcastError(f"HTTP {resp.status}: {text[:200]}")
