from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from api.infra.files import cleanup_expired_files
from api.infra.rate_limit import cleanup_rate_limits

if TYPE_CHECKING:
    from api.app import SidecarApp

logger = logging.getLogger("sidecar")


async def cleanup_loop(app: "SidecarApp") -> None:
    while not app.stop_event.is_set():
        await app.jobs.cleanup()
        cleanup_expired_files(app._file_store)
        cleanup_rate_limits(app.rate_limits, app.settings.rate_limit_window)
        try:
            await app.stock.sweep_expired()
        except Exception:
            logger.exception("Stock sweep failed")
        try:
            await app.free_claims.cleanup(app.settings.free_claim_window_seconds)
        except Exception:
            logger.exception("Free-claims cleanup failed")
        try:
            await app.claim_secrets.sweep_expired()
        except Exception:
            logger.exception("Claim-secrets sweep failed")
        try:
            await asyncio.wait_for(app.stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
