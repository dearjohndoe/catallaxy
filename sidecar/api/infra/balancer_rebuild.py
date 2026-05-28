"""Periodic LiteBalancer rebuild loop (plan B from LITESERVER_RESILIENCE_TASK.md).

Even with the `_mark_error` patch (plan A), long-lived balancer instances can
accumulate other state (stale liteserver pool, OS-level socket pressure, lib
internals we don't control). Periodically rebuilding the balancer is cheap
insurance: monitor caches survive, the send-lock keeps transfers atomic.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.app import SidecarApp

logger = logging.getLogger("sidecar")


DEFAULT_INTERVAL_SEC = 60 * 60 # 1 hour
JITTER_FRACTION = 0.15  # ± 15 %


def _interval_seconds() -> float:
    try:
        base = float(os.environ.get("BALANCER_REBUILD_INTERVAL_SEC", DEFAULT_INTERVAL_SEC))
    except ValueError:
        base = DEFAULT_INTERVAL_SEC
    base = max(60.0, base)
    jitter = base * JITTER_FRACTION
    return base + random.uniform(-jitter, jitter)


async def balancer_rebuild_loop(app: "SidecarApp") -> None:
    """Rebuild verifier / jetton_verifier / sender LiteBalancers on a schedule."""
    if os.environ.get("BALANCER_REBUILD_DISABLED") == "1":
        logger.info("balancer_rebuild_loop: disabled via env")
        return

    while not app.stop_event.is_set():
        interval = _interval_seconds()
        try:
            await asyncio.wait_for(app.stop_event.wait(), timeout=interval)
            return  # stop requested
        except asyncio.TimeoutError:
            pass

        try:
            if app.verifier is not None:
                await app.verifier.rebuild_client()
                logger.info("balancer_rebuild: PaymentVerifier client rebuilt")
        except Exception:
            logger.exception("balancer_rebuild: PaymentVerifier rebuild failed")

        try:
            if app.jetton_verifier is not None:
                await app.jetton_verifier.rebuild_client()
                logger.info("balancer_rebuild: JettonPaymentVerifier client rebuilt")
        except Exception:
            logger.exception("balancer_rebuild: JettonPaymentVerifier rebuild failed")

        try:
            await app.sender.rebuild_client()
            logger.info("balancer_rebuild: TransferSender client rebuilt")
        except Exception:
            logger.exception("balancer_rebuild: TransferSender rebuild failed")
