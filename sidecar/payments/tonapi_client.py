"""TonAPI HTTP fallback for tx polling (plan C from LITESERVER_RESILIENCE_TASK.md).

Used by WalletMonitor / JettonWalletMonitor when the primary ADNL path
(`tonutils.LiteBalancer`) fails. We expose a tiny duck-typed Transaction
shaped just like what verifier code reads: `tx.lt`, `tx.now`, `tx.cell.hash`,
`tx.in_msg.info.src.to_str(...)`, `tx.in_msg.info.value.grams`, `tx.in_msg.body`
(real pytoniq `Cell`, so `_parse_payment_nonce` / `parse_transfer_notification`
work unchanged).

Without `TONAPI_KEY` the public limit is ~1 RPS per IP — only safe as a
short-lived failure mode, not a steady-state source. Set the env var on every
sidecar unit for the 13K-club agents.
"""
from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace
from typing import Any

import aiohttp
from pytoniq_core import Address, Cell

logger = logging.getLogger(__name__)


DEFAULT_BASE = "https://tonapi.io"
DEFAULT_TIMEOUT = 8.0


class TonAPIError(Exception):
    pass


class TonAPIRateLimitError(TonAPIError):
    pass


class _Src:
    __slots__ = ("_raw",)

    def __init__(self, raw: str) -> None:
        self._raw = raw

    def to_str(self, is_user_friendly: bool = True, is_bounceable: bool = False) -> str:
        try:
            return Address(self._raw).to_str(
                is_user_friendly=is_user_friendly, is_bounceable=is_bounceable,
            )
        except Exception:
            return self._raw


class TonAPIClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base = (base_url or os.environ.get("TONAPI_BASE", DEFAULT_BASE)).rstrip("/")
        self._key = api_key if api_key is not None else (os.environ.get("TONAPI_KEY") or None)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    @property
    def has_api_key(self) -> bool:
        return bool(self._key)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                headers = {"Accept": "application/json"}
                if self._key:
                    headers["Authorization"] = f"Bearer {self._key}"
                self._session = aiohttp.ClientSession(headers=headers, timeout=self._timeout)
            return self._session

    async def close(self) -> None:
        sess = self._session
        self._session = None
        if sess is not None and not sess.closed:
            try:
                await sess.close()
            except Exception:
                logger.exception("TonAPIClient.close failed")

    async def get_account_transactions(
        self,
        address: str,
        limit: int = 50,
    ) -> list[Any]:
        """Fetch newest `limit` transactions for `address`.

        Returns duck-typed wrappers (see module docstring). Filtering by lt
        is left to the caller — TonAPI's pagination semantics differ from
        ADNL and we don't need them here (50 txs covers >> our poll window).
        """
        session = await self._ensure_session()
        url = f"{self._base}/v2/blockchain/accounts/{address}/transactions"
        params = {"limit": str(limit)}
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 429:
                    raise TonAPIRateLimitError("429 rate limited")
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    raise TonAPIError(f"HTTP {resp.status}: {body}")
                data = await resp.json()
        except aiohttp.ClientError as e:
            raise TonAPIError(f"network error: {e}") from e

        result: list[Any] = []
        for raw in data.get("transactions", []):
            tx = _wrap_tx(raw)
            if tx is not None:
                result.append(tx)
        return result


def _wrap_tx(raw: dict[str, Any]) -> Any | None:
    try:
        lt = int(raw["lt"])
        utime = int(raw.get("utime", 0))
        tx_hash = bytes.fromhex(raw["hash"])

        in_msg_raw = raw.get("in_msg")
        in_msg: Any | None = None
        if in_msg_raw is not None:
            src_addr = (in_msg_raw.get("source") or {}).get("address", "") or ""
            try:
                value = int(in_msg_raw.get("value", 0) or 0)
            except (TypeError, ValueError):
                value = 0

            body_hex = in_msg_raw.get("raw_body") or ""
            body_cell: Cell | None = None
            if body_hex:
                try:
                    body_cell = Cell.one_from_boc(bytes.fromhex(body_hex))
                except Exception:
                    body_cell = None

            is_external = in_msg_raw.get("msg_type") == "ext_in_msg"
            info = SimpleNamespace(
                src=_Src(src_addr),
                value=SimpleNamespace(grams=value),
            )
            in_msg = SimpleNamespace(info=info, body=body_cell, is_external=is_external)

        return SimpleNamespace(
            lt=lt,
            now=utime,
            in_msg=in_msg,
            cell=SimpleNamespace(hash=tx_hash),
        )
    except Exception:
        logger.exception("TonAPI _wrap_tx failed (raw keys=%s)", list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__)
        return None
