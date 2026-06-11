from __future__ import annotations

from pathlib import Path
from typing import Any


def validate_body(
    payload: dict[str, Any],
    args_schema: dict[str, Any],
    has_tx: bool = False,
    uploaded_files: dict[str, Path] | None = None,
) -> list[str]:
    body = payload.get("body")
    if not isinstance(body, dict):
        body = {}

    missing: list[str] = []
    for field, spec in args_schema.items():
        required = spec.get("required")
        field_type = spec.get("type")

        if field_type == "file":
            if not required:
                continue
            # Skip file validation on preflight (no tx) — file not sent yet
            if not has_tx:
                continue
            if uploaded_files and field in uploaded_files:
                continue
            missing.append(field)
        elif field_type == "select":
            if required and field not in body:
                missing.append(field)
                continue
            if field in body:
                options = spec.get("options")
                if isinstance(options, list) and options:
                    allowed = {
                        (o["value"] if isinstance(o, dict) else o)
                        for o in options
                    }
                    if body[field] not in allowed:
                        missing.append(field)
        else:
            if required and field not in body:
                missing.append(field)
    return missing


def validate_result_structure(raw: dict[str, Any]) -> None:
    """Ensure agent result has the required {type, data} structure."""
    result = raw.get("result")
    if not isinstance(result, dict):
        raise ValueError("Agent result must be a JSON object with 'type' and 'data' keys")
    if "type" not in result or "data" not in result:
        raise ValueError("Agent result must contain 'type' and 'data' keys")
