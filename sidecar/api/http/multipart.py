from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web


async def parse_multipart_invoke(
    request: web.Request, file_store_dir: Path
) -> tuple[str, str, str, str | None, str, str | None, dict[str, Any], dict[str, Path]]:
    """Parse multipart/form-data invoke request.

    Returns: (tx_hash, nonce, capability, quote_id, rail, sku, body_dict, uploaded_files)

    ``proof`` is the v2 field name; ``tx`` is the CTLX/1 alias. When both
    are present ``proof`` wins.
    """
    reader = await request.multipart()
    tx_hash = nonce = capability = ""
    proof = ""
    quote_id: str | None = None
    rail = "TON"
    sku: str | None = None
    body: dict[str, Any] = {}
    uploaded_files: dict[str, Path] = {}

    async for part in reader:
        name = part.name
        if name == "proof":
            proof = (await part.text()).strip()
        elif name == "tx":
            tx_hash = (await part.text()).strip()
        elif name == "nonce":
            nonce = (await part.text()).strip()
        elif name == "capability":
            capability = (await part.text()).strip()
        elif name == "quote_id":
            quote_id = (await part.text()).strip() or None
        elif name == "rail":
            rail = (await part.text()).strip().upper() or "TON"
        elif name == "sku":
            sku = (await part.text()).strip() or None
        elif name == "body_json":
            body = json.loads(await part.text())
        elif name and name.startswith("file:"):
            field_name = name[5:]  # strip "file:" prefix
            file_data = await part.read(decode=False)
            original_name = Path(part.filename or "").name or f"{uuid.uuid4().hex}.bin"
            upload_dir = file_store_dir / "uploads" / uuid.uuid4().hex
            upload_dir.mkdir(parents=True, exist_ok=True)
            file_path = upload_dir / original_name
            file_path.write_bytes(file_data)
            uploaded_files[field_name] = file_path

    return proof or tx_hash, nonce, capability, quote_id, rail, sku, body, uploaded_files
