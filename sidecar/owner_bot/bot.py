from __future__ import annotations

import asyncio
import html
import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger("sidecar")

_TELEGRAM_API = "https://api.telegram.org"

# Telegram limits: description ≤512, short_description ≤120, message ≤4096.
# We pre-trim ours well below those to leave room for HTML escaping growth.
_DESCRIPTION_LIMIT = 480
_SHORT_DESCRIPTION_LIMIT = 110
_BODY_PREVIEW_LIMIT = 500

# Long-poll timeout for getUpdates. Telegram holds the connection until an
# update arrives or this elapses. Session timeout must exceed this.
_LONG_POLL_SECONDS = 25
_SESSION_TIMEOUT_SECONDS = _LONG_POLL_SECONDS + 10

_PRODUCT_URL_BASE = "https://ctlx.cc/ru/agent"


def _tonviewer_base(testnet: bool) -> str:
    return "https://testnet.tonviewer.com" if testnet else "https://tonviewer.com"


def address_url(address: str, testnet: bool) -> str:
    return f"{_tonviewer_base(testnet)}/{address}"


def tx_url(tx_hash: str, testnet: bool) -> str:
    return f"{_tonviewer_base(testnet)}/transaction/{tx_hash}"


def _short(s: str, head: int = 6, tail: int = 4) -> str:
    if len(s) <= head + tail + 1:
        return s
    return f"{s[:head]}…{s[-tail:]}"


def _format_amount(amount: int | None, rail: str) -> str:
    if amount is None:
        return "?"
    if rail == "TON":
        return f"{amount / 1_000_000_000:.4f} TON"
    if rail == "USDT":
        return f"{amount / 1_000_000:.2f} USDT"
    return f"{amount} {rail}"


def _body_preview(body: Any) -> str:
    """JSON-serialize body, trim to ~500 chars, append ellipsis if cut."""
    try:
        s = json.dumps(body, ensure_ascii=False, default=str)
    except Exception:
        s = repr(body)
    if len(s) > _BODY_PREVIEW_LIMIT:
        s = s[:_BODY_PREVIEW_LIMIT] + "…"
    return s


def _link(text: str, href: str) -> str:
    return f'<a href="{html.escape(href, quote=True)}">{html.escape(text)}</a>'


class OwnerBot:
    """Telegram notifier for the agent owner.

    Sends one-way payment alerts to a fixed allowlist of chat IDs. Failures
    are logged and swallowed — the bot must never block or break the sidecar.
    Designed to grow into a control panel; today it only notifies.
    """

    def __init__(
        self,
        *,
        token: str,
        user_ids: tuple[int, ...],
        agent_name: str,
        agent_description: str,
        testnet: bool,
        sidecar_id: str,
    ) -> None:
        self._token = token
        self._user_ids = user_ids
        self._agent_name = agent_name
        self._agent_description = agent_description
        self._testnet = testnet
        self._sidecar_id = sidecar_id
        self._session: aiohttp.ClientSession | None = None
        self._base = f"{_TELEGRAM_API}/bot{token}"
        # Long-poll cursor: update_ids strictly less than this are already acked.
        self._next_offset: int = 0

    async def setup(self) -> None:
        """Open HTTP session, register bot description with Telegram.

        Description registration is best-effort — Telegram rejects no-op
        updates with 400, which we treat as success.
        """
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_SESSION_TIMEOUT_SECONDS)
        )

        net = "testnet" if self._testnet else "mainnet"
        long_desc = (
            f"Бот-уведомитель владельца агента «{self._agent_name}» в Catallaxy.\n"
            f"Присылает оповещения о входящих платежах и возвратах. "
            f"Сеть: {net}.\n\n"
            f"{self._agent_description}"
        )[:_DESCRIPTION_LIMIT]
        short_desc = f"Оповещения о платежах для «{self._agent_name}» ({net})"[:_SHORT_DESCRIPTION_LIMIT]

        await self._call("setMyDescription", {"description": long_desc}, ignore_400=True)
        await self._call("setMyShortDescription", {"short_description": short_desc}, ignore_400=True)

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                logger.exception("owner_bot: session close failed")
            self._session = None

    def notify_success(
        self,
        *,
        sender: str,
        amount: int,
        rail: str,
        sku_id: str,
        tx_hash: str,
        body: Any,
    ) -> None:
        """Fire-and-forget success notification. Returns immediately."""
        text = self._format_success(
            sender=sender, amount=amount, rail=rail,
            sku_id=sku_id, tx_hash=tx_hash, body=body,
        )
        self._fire(text)

    def notify_refund(
        self,
        *,
        sender: str | None,
        amount: int | None,
        rail: str,
        sku_id: str | None,
        tx_hash: str,
        reason: str,
        refund_tx: str | None = None,
        status: str = "refunded",
        delayed: bool = False,
    ) -> None:
        """Fire-and-forget refund/error notification. Returns immediately.

        ``delayed=True`` marks the alert as coming from the background refund
        worker (a retry after the original /invoke already returned).
        """
        text = self._format_refund(
            sender=sender, amount=amount, rail=rail, sku_id=sku_id,
            tx_hash=tx_hash, reason=reason, refund_tx=refund_tx, status=status,
            delayed=delayed,
        )
        self._fire(text)

    # ── formatting ─────────────────────────────────────────────────────────

    def _product_url(self) -> str:
        return f"{_PRODUCT_URL_BASE}/{self._sidecar_id}"

    def _product_line(self) -> str:
        return f"товар: {_link('открыть на ctlx.cc', self._product_url())}"

    def _format_success(
        self, *, sender: str, amount: int, rail: str,
        sku_id: str, tx_hash: str, body: Any,
    ) -> str:
        lines = [
            f"✅ <b>Платёж получен</b> — <i>{html.escape(self._agent_name)}</i>",
            f"sku: <code>{html.escape(sku_id)}</code> · канал: {html.escape(rail)} · "
            f"сумма: <b>{_format_amount(amount, rail)}</b>",
            f"отправитель: {_link(_short(sender), address_url(sender, self._testnet))}",
            f"транзакция: {_link(_short(tx_hash, 8, 6), tx_url(tx_hash, self._testnet))}",
            f"параметры: <code>{html.escape(_body_preview(body))}</code>",
        ]
        lines.append(self._product_line())
        lines.append("Чат с покупателем доступен на сайте.")
        return "\n".join(lines)

    def _format_refund(
        self, *, sender: str | None, amount: int | None, rail: str,
        sku_id: str | None, tx_hash: str, reason: str,
        refund_tx: str | None, status: str, delayed: bool = False,
    ) -> str:
        icon = "❌" if status == "refunded" else "⏳"
        title = "Возврат отправлен" if status == "refunded" else "Возврат ожидает"
        if delayed:
            title = f"{title} (отложенно · refund worker)"
        lines = [
            f"{icon} <b>{title}</b> — <i>{html.escape(self._agent_name)}</i>",
            f"причина: <code>{html.escape(reason)}</code>",
        ]
        sku_part = f"sku: <code>{html.escape(sku_id)}</code> · " if sku_id else ""
        lines.append(
            f"{sku_part}канал: {html.escape(rail)} · "
            f"сумма: <b>{_format_amount(amount, rail)}</b>"
        )
        if sender:
            lines.append(f"получатель: {_link(_short(sender), address_url(sender, self._testnet))}")
        lines.append(
            f"исходная транзакция: {_link(_short(tx_hash, 8, 6), tx_url(tx_hash, self._testnet))}"
        )
        if refund_tx:
            lines.append(
                f"транзакция возврата: {_link(_short(refund_tx, 8, 6), tx_url(refund_tx, self._testnet))}"
            )
        lines.append(self._product_line())
        return "\n".join(lines)

    # ── inbound polling ────────────────────────────────────────────────────

    def _reply_text(self) -> str:
        net = "testnet" if self._testnet else "mainnet"
        lines = [
            f"🤖 <b>{html.escape(self._agent_name)}</b> — бот-уведомитель владельца",
            "Этот бот принадлежит продавцу агента в Catallaxy и присылает "
            "владельцу оповещения о входящих платежах и возвратах.",
            "Он не обрабатывает заказы и не общается с покупателями. "
            "Чтобы воспользоваться самим агентом, обратитесь к его API.",
            f"Сеть: {net}.",
            self._product_line(),
        ]
        return "\n".join(lines)

    async def poll_loop(self, stop_event: asyncio.Event) -> None:
        """Long-poll getUpdates and reply to whitelisted users.

        Drains the backlog on first iteration so users don't get spammed with
        replies for messages sent while the sidecar was offline.
        """
        await self._drain_pending()
        while not stop_event.is_set():
            try:
                updates = await self._get_updates(
                    offset=self._next_offset, timeout=_LONG_POLL_SECONDS,
                )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("owner_bot: getUpdates failed")
                # Back off a bit to avoid hammering Telegram on persistent failure.
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=5)
                    return
                except asyncio.TimeoutError:
                    continue
            for update in updates:
                uid = int(update.get("update_id", 0))
                if uid >= self._next_offset:
                    self._next_offset = uid + 1
                try:
                    await self._handle_update(update)
                except Exception:
                    logger.exception("owner_bot: handle_update failed")

    async def _drain_pending(self) -> None:
        """Ack and discard any updates queued by Telegram while we were down."""
        try:
            updates = await self._get_updates(offset=-1, timeout=0)
        except Exception:
            logger.exception("owner_bot: initial drain failed")
            return
        for update in updates:
            uid = int(update.get("update_id", 0))
            if uid >= self._next_offset:
                self._next_offset = uid + 1

    async def _handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not isinstance(msg, dict):
            return
        from_user = msg.get("from") or {}
        user_id = from_user.get("id")
        if user_id not in self._user_ids:
            return  # silently ignore non-whitelisted senders
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        await self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": self._reply_text(),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    async def _get_updates(self, *, offset: int, timeout: int) -> list[dict[str, Any]]:
        """Wrapper around getUpdates that returns the result array or []."""
        if self._session is None:
            return []
        url = f"{self._base}/getUpdates"
        payload = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        # Per-request timeout: long-poll holds the connection for `timeout`
        # seconds, plus network slack. Override the session default just in case.
        async with self._session.post(
            url, json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout + 10),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    "owner_bot: getUpdates status=%s body=%s", resp.status, body[:200],
                )
                return []
            data = await resp.json()
            result = data.get("result")
            return result if isinstance(result, list) else []

    # ── transport ──────────────────────────────────────────────────────────

    def _fire(self, text: str) -> None:
        """Schedule sends without awaiting them. Safe to call from sync code
        inside an event-loop context (i.e. from async handlers)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("owner_bot: notify called outside event loop, dropping")
            return
        for chat_id in self._user_ids:
            loop.create_task(self._send_to_chat(chat_id, text))

    async def _send_to_chat(self, chat_id: int, text: str) -> None:
        try:
            await self._call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        except Exception:
            logger.exception("owner_bot: sendMessage failed chat_id=%s", chat_id)

    async def _call(
        self, method: str, payload: dict[str, Any], *, ignore_400: bool = False,
    ) -> None:
        if self._session is None:
            logger.warning("owner_bot: session not initialised, dropping %s", method)
            return
        url = f"{self._base}/{method}"
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return
                body = await resp.text()
                if ignore_400 and resp.status == 400:
                    logger.info(
                        "owner_bot: %s returned 400 (ignored): %s", method, body[:200],
                    )
                    return
                logger.warning(
                    "owner_bot: %s failed status=%s body=%s", method, resp.status, body[:200],
                )
        except Exception:
            logger.exception("owner_bot: HTTP call %s failed", method)
