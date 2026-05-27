from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any
from aiohttp import web

from heartbeat import HeartbeatManager
from jobs import JobStore
from storage import StateStore
from transfer import TransferSender
from payments import PaymentVerifier, JettonPaymentVerifier, ProcessedTxStore, RefundQueue, TonAPIClient
from jetton import USDT_MASTER_MAINNET, USDT_MASTER_TESTNET
from owner_bot import OwnerBot
from settings import Settings, AgentSku, DEFAULT_SKU_ID  # noqa: F401 — re-exported via api package
from stock import StockStore

from api.domain.quoting import DynamicPriceCache, QuoteEntry, cleanup_expired_quotes
from api.domain.refund import refund_user as _refund_user
from api.domain.result_processing import process_file_result, safe_extract_result
from api.infra.cleanup import cleanup_loop as _cleanup_loop
from api.infra.files import cleanup_expired_files, cleanup_file
from api.infra.rate_limit import cleanup_rate_limits
from api.lifecycle import shutdown as _shutdown, startup as _startup
from api.validation import validate_result_structure
from api.http.middleware import make_cors_middleware, make_rate_limit_middleware
from api.http.routes import register_routes
from api.http.handlers.image import handle_image as _handle_image
from api.http.handlers.info import handle_info as _handle_info
from api.http.handlers.invoke import handle_invoke as _handle_invoke
from api.http.handlers.quote import handle_quote as _handle_quote
from api.http.handlers.result import handle_download as _handle_download
from api.http.handlers.result import handle_result as _handle_result

logger = logging.getLogger("sidecar")


class SidecarApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.args_schema: dict[str, Any] = {}
        self.result_schema: dict[str, Any] | None = None
        self._file_store: dict[str, dict[str, Any]] = {}
        self._file_store_dir = Path(settings.file_store_dir)
        self._file_store_ttl = settings.file_store_ttl
        self._images_dir = Path(settings.images_dir).resolve()
        self.jobs = JobStore(ttl_seconds=settings.jobs_ttl)
        self.tx_store = ProcessedTxStore(settings.tx_db_path)
        self.stock = StockStore(settings.stock_db_path)
        self._skus_by_id: dict[str, AgentSku] = {s.sku_id: s for s in settings.skus}
        self._single_sku: AgentSku | None = settings.skus[0] if len(settings.skus) == 1 else None
        # Shared TonAPI HTTP fallback (plan C). One aiohttp session per sidecar,
        # passed to both TON and jetton monitors. Disabled via env if needed.
        self.tonapi_client: TonAPIClient | None = (
            None if os.environ.get("TONAPI_FALLBACK_DISABLED") == "1" else TonAPIClient()
        )
        self.verifier = PaymentVerifier(
            agent_wallet=settings.agent_wallet,
            min_amount=0,  # per-call min comes from SKU; constructor default unused
            payment_timeout_seconds=settings.payment_timeout,
            enforce_comment_nonce=settings.enforce_comment_nonce,
            testnet=settings.testnet,
            tonapi_client=self.tonapi_client,
        )
        self.state_store = StateStore(settings.state_path)
        self.sender = TransferSender(
            private_key_hex=settings.agent_wallet_pk,
            testnet=settings.testnet,
        )
        self.jetton_verifier: JettonPaymentVerifier | None = None
        self._agent_jetton_wallet: str | None = None
        self._jetton_init_lock = asyncio.Lock()
        if any(s.price_usd is not None for s in settings.skus):
            usdt_master = USDT_MASTER_TESTNET if settings.testnet else USDT_MASTER_MAINNET
            self.jetton_verifier = JettonPaymentVerifier(
                agent_wallet=settings.agent_wallet,
                usdt_master=usdt_master,
                min_amount=0,  # per-call min comes from SKU; constructor default unused
                payment_timeout_seconds=settings.payment_timeout,
                testnet=settings.testnet,
                tonapi_client=self.tonapi_client,
            )
        # Refund queue lives in the same SQLite file as ProcessedTxStore — they
        # already share per-agent scoping via tx_db_path and SQLite handles the
        # two connections fine. One env var, one file to back up.
        self.refund_queue = RefundQueue(settings.tx_db_path)
        self.stop_event = asyncio.Event()
        self.sidecar_id: str = ""
        # Dynamic pricing cache (populated via agent mode=prices when SKU price==0)
        self._dynamic_prices_cache = DynamicPriceCache()
        # Heartbeat is created in lifecycle.startup once args_schema and sidecar_id
        # are loaded — there's no useful skeleton we could put here.
        self.heartbeat: HeartbeatManager | None = None
        self.background_tasks: list[asyncio.Task[Any]] = []
        self.quotes: dict[str, QuoteEntry] = {}
        # Rate Limiting state: ip -> list of timestamps
        self.rate_limits: dict[str, list[float]] = {}
        # Optional owner-facing TG bot. Constructed in lifecycle.startup once
        # sidecar_id is loaded — mirrors the heartbeat pattern above. Bot stays
        # None when TG_BOT_TOKEN / TG_USER_ID_LIST aren't set.
        self.owner_bot: OwnerBot | None = None

    async def refund_user(
        self, recipient: str, payment_amount: int, original_tx_hash: str, reason: str, rail: str = "TON",
    ) -> str | None:
        return await _refund_user(
            sender=self.sender,
            agent_jetton_wallet=self._agent_jetton_wallet,
            sidecar_id=self.sidecar_id,
            refund_fee_nanoton=self.settings.refund_fee_nanoton,
            recipient=recipient,
            payment_amount=payment_amount,
            original_tx_hash=original_tx_hash,
            reason=reason,
            rail=rail,
        )

    async def ensure_jetton_verifier(self) -> bool:
        """Lazy-bootstrap the jetton verifier. Idempotent and concurrency-safe.

        Used both by the /invoke handler (when verifier was missing at startup)
        and by the refund worker (to recover sender/amount for a queued tx).
        Returns True iff the verifier is started and the agent's jetton wallet
        address is known.
        """
        if self._agent_jetton_wallet and self.jetton_verifier is not None:
            return True
        async with self._jetton_init_lock:
            if self._agent_jetton_wallet and self.jetton_verifier is not None:
                return True
            if self.jetton_verifier is None:
                usdt_master = USDT_MASTER_TESTNET if self.settings.testnet else USDT_MASTER_MAINNET
                self.jetton_verifier = JettonPaymentVerifier(
                    agent_wallet=self.settings.agent_wallet,
                    usdt_master=usdt_master,
                    min_amount=0,  # per-call min comes from SKU; constructor default unused
                    payment_timeout_seconds=self.settings.payment_timeout,
                    testnet=self.settings.testnet,
                    tonapi_client=self.tonapi_client,
                )
            try:
                await self.jetton_verifier.start()
                self._agent_jetton_wallet = self.jetton_verifier.jetton_wallet_address
                return True
            except Exception:
                logger.exception("ensure_jetton_verifier: start() failed")
                return False

    async def startup(self) -> None:
        await _startup(self)

    async def shutdown(self) -> None:
        await _shutdown(self)

    async def cleanup_loop(self) -> None:
        await _cleanup_loop(self)

    # ── Test-facing helpers (used by test_api.py / test_known_bugs.py) ────

    def _process_file_result(self, result: dict[str, Any]) -> dict[str, Any]:
        return process_file_result(result, self._file_store, self._file_store_dir, self._file_store_ttl)

    def _safe_extract_result(self, record_result: Any) -> tuple[dict[str, Any] | Any, str | None]:
        return safe_extract_result(record_result, self._file_store, self._file_store_dir, self._file_store_ttl)

    def _cleanup_file(self, file_id: str) -> None:
        cleanup_file(self._file_store, file_id)

    def _cleanup_expired_files(self) -> None:
        cleanup_expired_files(self._file_store)

    def _cleanup_expired_quotes(self) -> None:
        cleanup_expired_quotes(self.quotes)

    def _cleanup_rate_limits(self) -> None:
        cleanup_rate_limits(self.rate_limits, self.settings.rate_limit_window)

    @staticmethod
    def _validate_result_structure(raw: dict[str, Any]) -> None:
        validate_result_structure(raw)

    # ── HTTP handlers (thin wrappers — register_routes binds these) ───────

    async def handle_image(self, request: web.Request) -> web.StreamResponse:
        return await _handle_image(request, self._images_dir)

    async def handle_quote(self, request: web.Request) -> web.Response:
        return await _handle_quote(request, self)

    async def handle_invoke(self, request: web.Request) -> web.Response:
        return await _handle_invoke(request, self)

    async def handle_result(self, request: web.Request) -> web.Response:
        return await _handle_result(request, self)

    async def handle_download(self, request: web.Request) -> web.Response:
        return await _handle_download(request, self._file_store)

    async def handle_info(self, request: web.Request) -> web.Response:
        return await _handle_info(request, self)

    def build_web_app(self) -> web.Application:
        max_upload_mb = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "150"))
        app = web.Application(
            client_max_size=1024 * 1024 * max_upload_mb,
            middlewares=[
                make_cors_middleware(),
                make_rate_limit_middleware(self.settings, self.rate_limits),
            ],
        )
        register_routes(app, self)
        app.on_startup.append(lambda _: self.startup())
        app.on_shutdown.append(lambda _: self.shutdown())
        return app
