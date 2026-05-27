"""Monkey-patch for tonutils.LiteBalancer._mark_error.

Upstream uses exponential backoff `base * 2 ** (error_count - 1)`. Transient
errors from the public pool ("lt not in db" on stale liteserver indexes) push
`error_count` up indefinitely; cooldowns grow into minutes/hours and at
`error_count >= ~1024` the `2 ** n` term overflows to OverflowError, after
which every call fails. See LITESERVER_RESILIENCE_TASK.md (plan A).

Replacement behaviour: every error puts the client into a flat 30 s cooldown,
`error_count` is capped at 1 — clients always cycle back into rotation quickly
and the counter never grows.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

FIXED_RETRY_AFTER_SEC = 30.0

_PATCHED_ATTR = "_mark_error_patched_fixed_cooldown"


def apply_mark_error_patch() -> None:
    try:
        from tonutils.clients.adnl.balancer import LiteBalancer
    except Exception:
        logger.exception("balancer_patch: cannot import LiteBalancer; skipping patch")
        return

    if getattr(LiteBalancer, _PATCHED_ATTR, False):
        return

    def _mark_error(self, client, is_rate_limit: bool) -> None:  # type: ignore[no-untyped-def]
        now = time.monotonic()
        for state in self._states:
            if state.client is client:
                state.error_count = 1
                state.retry_after = now + FIXED_RETRY_AFTER_SEC
                break

    LiteBalancer._mark_error = _mark_error  # type: ignore[assignment]
    setattr(LiteBalancer, _PATCHED_ATTR, True)
    logger.info(
        "balancer_patch: LiteBalancer._mark_error patched (fixed %.0fs cooldown)",
        FIXED_RETRY_AFTER_SEC,
    )
