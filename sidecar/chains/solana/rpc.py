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
        self._rent_cache: dict[int, tuple[int, float]] = {}

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
        except Exception as exc:
            logger.warning("solana getHealth failed: %s", exc)
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

    async def min_rent(self, data_len: int, *, fallback: int, ttl: float = 3600.0) -> int:
        """Rent-exempt minimum for ``data_len`` bytes, cached for ``ttl``.

        Live value from the cluster (rent is a governed parameter and has
        changed); ``fallback`` only when the RPC fails.
        """
        hit = self._rent_cache.get(data_len)
        if hit and time.time() - hit[1] < ttl:
            return hit[0]
        try:
            value = await self.call("getMinimumBalanceForRentExemption", [data_len])
            if not isinstance(value, int) or value <= 0:
                raise SolanaRpcError("bad rent value")
        except Exception:
            logger.warning("getMinimumBalanceForRentExemption(%s) failed; using %s", data_len, fallback)
            return fallback
        self._rent_cache[data_len] = (value, time.time())
        return value

    async def get_balance(self, pubkey: str, commitment: str = "confirmed") -> int:
        result = await self.call("getBalance", [pubkey, {"commitment": commitment}])
        value = result.get("value") if isinstance(result, dict) else result
        if not isinstance(value, int):
            raise SolanaRpcError("getBalance returned no value")
        return value

    async def get_signatures_for_address(
        self,
        address: str,
        *,
        limit: int = 100,
        before: str | None = None,
        until: str | None = None,
        commitment: str = "confirmed",
    ) -> list[dict[str, Any]]:
        """Newest-first signature rows (``signature``/``err``/``memo``/``blockTime``)."""
        opts: dict[str, Any] = {"limit": limit, "commitment": commitment}
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        result = await self.call("getSignaturesForAddress", [address, opts])
        if not isinstance(result, list):
            raise SolanaRpcError("getSignaturesForAddress returned non-list")
        return [r for r in result if isinstance(r, dict) and r.get("signature")]

    async def get_transaction(
        self, signature: str, commitment: str = "confirmed",
    ) -> dict[str, Any] | None:
        """jsonParsed transaction, or None while it is not visible at ``commitment``."""
        result = await self.call(
            "getTransaction",
            [signature, {
                "encoding": "jsonParsed",
                "commitment": commitment,
                "maxSupportedTransactionVersion": 0,
            }],
        )
        return result if isinstance(result, dict) else None

    async def get_token_account_balance(self, token_account: str) -> int | None:
        """Raw base units, or None when the token account does not exist."""
        try:
            result = await self.call(
                "getTokenAccountBalance", [token_account, {"commitment": "confirmed"}],
            )
        except SolanaRpcError as exc:
            if "could not find account" in str(exc).lower() or "-32602" in str(exc):
                return None
            raise
        value = (result or {}).get("value") if isinstance(result, dict) else None
        if not isinstance(value, dict) or value.get("amount") is None:
            return None
        return int(value["amount"])
