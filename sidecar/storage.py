from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SidecarState:
    last_heartbeat: str | None = None  # TON registry
    sidecar_id: str | None = None
    last_sol_heartbeat: str | None = None  # Solana registry


class StateStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def load(self) -> SidecarState:
        # File not existing is a legitimate first-run state — return defaults.
        if not self._path.exists():
            return SidecarState()
        # Any other failure mode (unreadable, corrupt, wrong shape) is fatal:
        # losing sidecar_id silently would re-register the agent under a fresh
        # identity on the registry, stranding in-flight payments. Crash loudly
        # so the operator notices and restores the state file from backup.
        raw = self._path.read_text(encoding="utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"State file {self._path} is corrupt (invalid JSON): {exc}. "
                "Refusing to start with a blank identity — restore the file "
                "from backup or delete it to re-register as a new agent."
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"State file {self._path} is malformed: expected a JSON object, "
                f"got {type(payload).__name__}. Refusing to start with a blank "
                "identity — restore the file from backup or delete it to "
                "re-register as a new agent."
            )
        missing = {"last_heartbeat", "sidecar_id"} - payload.keys()
        if missing:
            raise RuntimeError(
                f"State file {self._path} is missing required keys: "
                f"{sorted(missing)}. Refusing to start with a partial identity."
            )
        return SidecarState(
            last_heartbeat=payload["last_heartbeat"],
            sidecar_id=payload["sidecar_id"],
            # Optional: state files written before the Solana registry lack it.
            last_sol_heartbeat=payload.get("last_sol_heartbeat"),
        )

    def save(self, state: SidecarState) -> None:
        payload = {
            "last_heartbeat": state.last_heartbeat,
            "sidecar_id": state.sidecar_id,
            "last_sol_heartbeat": state.last_sol_heartbeat,
        }
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
