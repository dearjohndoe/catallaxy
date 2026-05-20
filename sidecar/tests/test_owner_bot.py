"""Tests for sidecar/owner_bot — formatting, URL builders, settings parsing."""

from __future__ import annotations

import pytest

from owner_bot.bot import (
    _body_preview,
    _format_amount,
    _short,
    address_url,
    tx_url,
    OwnerBot,
)
from settings import _parse_tg_user_ids


# ── URL builders ────────────────────────────────────────────────────────

def test_tonviewer_urls_mainnet():
    assert address_url("UQAB", False) == "https://tonviewer.com/UQAB"
    assert tx_url("deadbeef", False) == "https://tonviewer.com/transaction/deadbeef"


def test_tonviewer_urls_testnet():
    assert address_url("UQAB", True) == "https://testnet.tonviewer.com/UQAB"
    assert tx_url("deadbeef", True) == "https://testnet.tonviewer.com/transaction/deadbeef"


# ── helpers ─────────────────────────────────────────────────────────────

def test_short_keeps_short_strings():
    assert _short("abc") == "abc"
    assert _short("abcdef") == "abcdef"


def test_short_truncates_long_strings():
    s = "a" * 50
    out = _short(s, head=6, tail=4)
    assert out.startswith("aaaaaa") and out.endswith("aaaa") and "…" in out


def test_format_amount_ton():
    assert _format_amount(1_000_000_000, "TON") == "1.0000 TON"
    assert _format_amount(500_000_000, "TON") == "0.5000 TON"


def test_format_amount_usdt():
    assert _format_amount(1_000_000, "USDT") == "1.00 USDT"
    assert _format_amount(1_500_000, "USDT") == "1.50 USDT"


def test_format_amount_none():
    assert _format_amount(None, "TON") == "?"


def test_body_preview_trims_long_input():
    big = {"prompt": "x" * 1000}
    out = _body_preview(big)
    assert len(out) <= 501  # 500 + ellipsis
    assert out.endswith("…")


def test_body_preview_keeps_short_input_intact():
    out = _body_preview({"k": "v"})
    assert out == '{"k": "v"}'


def test_body_preview_handles_non_serializable():
    class X:
        pass
    out = _body_preview({"x": X()})
    assert "X object" in out or "X " in out  # default=str fallback


# ── settings parsing ────────────────────────────────────────────────────

def test_parse_tg_user_ids_basic():
    assert _parse_tg_user_ids("1,2,3") == (1, 2, 3)


def test_parse_tg_user_ids_dedup_preserves_order():
    assert _parse_tg_user_ids("3, 1, 2, 1, 3") == (3, 1, 2)


def test_parse_tg_user_ids_blank():
    assert _parse_tg_user_ids("") == ()
    assert _parse_tg_user_ids("  ,  ,") == ()


def test_parse_tg_user_ids_rejects_non_int():
    with pytest.raises(RuntimeError, match="invalid id"):
        _parse_tg_user_ids("1,abc,3")


# ── message formatters ─────────────────────────────────────────────────

def _make_bot(testnet: bool = False, sidecar_id: str = "sid-abc") -> OwnerBot:
    return OwnerBot(
        token="xxx", user_ids=(42,),
        agent_name="MyAgent", agent_description="d", testnet=testnet,
        sidecar_id=sidecar_id,
    )


def test_format_success_contains_key_fields():
    bot = _make_bot()
    text = bot._format_success(
        sender="UQABCDE12345", amount=1_000_000_000, rail="TON",
        sku_id="default", tx_hash="0xdeadbeef" + "0" * 50,
        body={"prompt": "hi"},
    )
    assert "Платёж получен" in text
    assert "MyAgent" in text
    assert "default" in text
    assert "TON" in text
    assert "1.0000 TON" in text
    assert "tonviewer.com" in text
    assert "prompt" in text
    # product link + buyer-chat hint
    assert "ctlx.cc/ru/agent/sid-abc" in text
    assert "Чат с покупателем доступен на сайте." in text


def test_format_success_escapes_html_in_body():
    bot = _make_bot()
    text = bot._format_success(
        sender="UQABC", amount=1, rail="TON",
        sku_id="default", tx_hash="hash",
        body={"prompt": "<script>x</script>"},
    )
    assert "<script>x</script>" not in text
    assert "&lt;script&gt;" in text


def test_format_refund_with_refund_tx():
    bot = _make_bot(testnet=True)
    text = bot._format_refund(
        sender="UQAB", amount=2_000_000, rail="USDT", sku_id="basic",
        tx_hash="orig_hash", reason="out_of_stock",
        refund_tx="ref_hash", status="refunded",
    )
    assert "Возврат отправлен" in text
    assert "out_of_stock" in text
    assert "USDT" in text
    assert "2.00 USDT" in text
    assert "testnet.tonviewer.com" in text
    assert "ref_hash" in text and "orig_hash" in text
    assert "ctlx.cc/ru/agent/sid-abc" in text
    # buyer-chat hint is success-only
    assert "Чат с покупателем" not in text


def test_reply_text_describes_bot():
    bot = _make_bot()
    txt = bot._reply_text()
    assert "MyAgent" in txt
    assert "бот-уведомитель владельца" in txt
    assert "mainnet" in txt
    assert "ctlx.cc/ru/agent/sid-abc" in txt


# ── inbound handling ───────────────────────────────────────────────────

class _StubBot(OwnerBot):
    def __init__(self, allow_id: int):
        super().__init__(
            token="t", user_ids=(allow_id,),
            agent_name="A", agent_description="d", testnet=False,
            sidecar_id="sid-stub",
        )
        self.sent: list[dict] = []

    async def _call(self, method, payload, *, ignore_400=False):
        self.sent.append({"method": method, "payload": payload})


@pytest.mark.asyncio
async def test_handle_update_replies_to_whitelisted_user():
    bot = _StubBot(allow_id=42)
    await bot._handle_update({
        "update_id": 1,
        "message": {"from": {"id": 42}, "chat": {"id": 99}, "text": "hi"},
    })
    assert len(bot.sent) == 1
    assert bot.sent[0]["method"] == "sendMessage"
    assert bot.sent[0]["payload"]["chat_id"] == 99
    assert "бот-уведомитель владельца" in bot.sent[0]["payload"]["text"]


@pytest.mark.asyncio
async def test_handle_update_ignores_unknown_user():
    bot = _StubBot(allow_id=42)
    await bot._handle_update({
        "update_id": 1,
        "message": {"from": {"id": 7}, "chat": {"id": 99}, "text": "hi"},
    })
    assert bot.sent == []


@pytest.mark.asyncio
async def test_handle_update_ignores_non_message():
    bot = _StubBot(allow_id=42)
    await bot._handle_update({"update_id": 1, "callback_query": {}})
    assert bot.sent == []


def test_format_refund_delayed_flag_marks_message():
    bot = _make_bot()
    text = bot._format_refund(
        sender="UQAB", amount=1_000_000_000, rail="TON", sku_id="default",
        tx_hash="t", reason="retry", refund_tx="r", status="refunded",
        delayed=True,
    )
    assert "отложенно" in text and "refund worker" in text


def test_format_refund_pending_without_refund_tx():
    bot = _make_bot()
    text = bot._format_refund(
        sender=None, amount=None, rail="TON", sku_id=None,
        tx_hash="orig", reason="verifier_error",
        refund_tx=None, status="refund_pending",
    )
    assert "Возврат ожидает" in text
    assert "verifier_error" in text
    # No 'получатель:' line when sender is None
    assert "получатель:" not in text
    # No refund tx line
    assert "транзакция возврата:" not in text
