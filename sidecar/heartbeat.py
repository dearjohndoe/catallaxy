from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from pytoniq_core import Cell

from storage import SidecarState, StateStore
from transfer import TransferFn, heartbeat_body

logger = logging.getLogger(__name__)

MAX_IMAGES = 5
MAX_URL_LEN = 512
MAX_PAYLOAD_BYTES = 2048
_ALLOWED_SCHEMES = {"http", "https"}


def _valid_image_url(url: Any) -> bool:
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False
    if not parsed.netloc:
        return False
    path = parsed.path.lower()
    if path.endswith(".svg") or path.endswith(".svgz"):
        return False
    return True


@dataclass
class HeartbeatConfig:
    registry_address: str
    endpoint: str
    price: int
    capabilities: tuple[str, ...]
    name: str
    description: str
    args_schema: dict[str, Any]
    has_quote: bool = False
    price_usdt: int | None = None
    sidecar_id: str | None = None
    result_schema: dict[str, Any] | None = None
    preview_url: str | None = None
    avatar_url: str | None = None
    images: tuple[str, ...] = field(default_factory=tuple)
    owner_wallet: str | None = None


class HeartbeatManager:
    def __init__(
        self,
        config: HeartbeatConfig,
        state_store: StateStore,
        transfer_sender: TransferFn,
        heartbeat_interval_days: int = 7,
        immediate_threshold_days: int = 6,
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._transfer_sender = transfer_sender
        self._interval = timedelta(days=heartbeat_interval_days)
        self._immediate_threshold = timedelta(days=immediate_threshold_days)

    def _build_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self._config.name,
            "description": self._config.description,
            "capabilities": list(self._config.capabilities),
            "price": self._config.price,
            "endpoint": self._config.endpoint,
            "args_schema": self._config.args_schema,
        }
        if self._config.has_quote:
            payload["has_quote"] = True
        if self._config.price_usdt is not None:
            payload["price_usdt"] = self._config.price_usdt
        if self._config.sidecar_id:
            payload["sidecar_id"] = self._config.sidecar_id
        if self._config.result_schema:
            payload["result_schema"] = self._config.result_schema
        if self._config.owner_wallet:
            payload["owner_wallet"] = self._config.owner_wallet

        if self._config.preview_url:
            if _valid_image_url(self._config.preview_url):
                payload["preview_url"] = self._config.preview_url
            else:
                logger.warning("Dropping invalid preview_url: %s", self._config.preview_url)

        if self._config.avatar_url:
            if _valid_image_url(self._config.avatar_url):
                payload["avatar_url"] = self._config.avatar_url
            else:
                logger.warning("Dropping invalid avatar_url: %s", self._config.avatar_url)

        if self._config.images:
            valid = [u for u in self._config.images if _valid_image_url(u)]
            dropped = len(self._config.images) - len(valid)
            if dropped:
                logger.warning("Dropped %d invalid image URL(s) from heartbeat", dropped)
            if len(valid) > MAX_IMAGES:
                logger.warning("Truncating images to %d (had %d)", MAX_IMAGES, len(valid))
                valid = valid[:MAX_IMAGES]
            if valid:
                payload["images"] = valid

        encoded_len = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if encoded_len > MAX_PAYLOAD_BYTES:
            logger.warning(
                "Heartbeat payload too large (%d bytes > %d); dropping media fields",
                encoded_len, MAX_PAYLOAD_BYTES,
            )
            for k in ("preview_url", "avatar_url", "images"):
                payload.pop(k, None)

        return payload

    def _should_send_now(self, state: SidecarState) -> bool:
        if not state.last_heartbeat:
            return True
        try:
            last = datetime.fromisoformat(state.last_heartbeat.replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(timezone.utc) - last >= self._immediate_threshold

    async def send_if_needed(self, force: bool = False) -> bool:
        state = self._state_store.load()
        if not force and not self._should_send_now(state):
            return False

        payload = self._build_payload()
        payload_json = json.dumps(payload, ensure_ascii=False)
        body = heartbeat_body(payload_json)
        await self._transfer_sender(self._config.registry_address, 10_000_000, body)

        state.last_heartbeat = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._state_store.save(state)
        logger.info("Heartbeat sent")
        return True

    async def loop(self, stop_event: asyncio.Event) -> None:
        # Wake at least once per hour so we can react quickly to shutdown,
        # but honor self._interval when it's shorter than that (tests and
        # aggressive intervals would otherwise be ignored entirely).
        poll_timeout = min(self._interval.total_seconds(), 3600)
        while not stop_event.is_set():
            failed = False
            try:
                await self.send_if_needed(force=False)
            except Exception:
                logger.exception("Heartbeat failed")
                failed = True
            sleep_for = 300 if failed else poll_timeout
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                continue
