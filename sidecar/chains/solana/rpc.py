"""Minimal Solana JSON-RPC over aiohttp. Injectable in tests."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("sidecar")


class SolanaRpcError(RuntimeError):
    pass


class SolanaRpc:
    def __init__(self, url: str, *, session: Any | None = None, timeout: float = 20.0) -> None:
        self.url = url
        self._session = session
        self._own_session = False
        self._timeout = timeout
        self.last_ok_at: float = 0.0

    async def close(self) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None
            self._own_session = False

    async def _session_ready(self) -> Any:
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        session = await self._session_ready()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        try:
            async with session.post(
                self.url, json=payload, timeout=self._timeout,
            ) as resp:
                body = await resp.json(content_type=None)
        except Exception as exc:
            raise SolanaRpcError(f"{method} transport failed: {exc}") from exc
        if not isinstance(body, dict):
            raise SolanaRpcError(f"{method} returned non-object")
        if body.get("error"):
            raise SolanaRpcError(f"{method} rpc error: {body['error']}")
        self.last_ok_at = time.time()
        return body.get("result")

    async def get_health(self) -> bool:
        try:
            result = await self.call("getHealth")
            return result == "ok"
        except Exception:
            logger.warning("solana getHealth failed", exc_info=True)
            return False

    async def send_transaction(self, tx_b64: str) -> str:
        result = await self.call(
            "sendTransaction",
            [tx_b64, {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}],
        )
        if not isinstance(result, str) or not result:
            raise SolanaRpcError("sendTransaction returned no signature")
        return result

    async def signature_status(self, signature: str) -> dict[str, Any] | None:
        result = await self.call(
            "getSignatureStatuses",
            [[signature], {"searchTransactionHistory": True}],
        )
        if not isinstance(result, dict):
            return None
        value = result.get("value") or [None]
        row = value[0] if value else None
        return row if isinstance(row, dict) else None

    async def get_latest_blockhash(self) -> tuple[str, int]:
        result = await self.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        value = (result or {}).get("value") or result or {}
        blockhash = value.get("blockhash")
        last = int(value.get("lastValidBlockHeight") or 0)
        if not blockhash:
            raise SolanaRpcError("getLatestBlockhash missing blockhash")
        return str(blockhash), last

    async def get_account_info(self, pubkey: str) -> dict[str, Any] | None:
        result = await self.call(
            "getAccountInfo",
            [pubkey, {"encoding": "base64", "commitment": "confirmed"}],
        )
        if not isinstance(result, dict):
            return None
        return result.get("value")
