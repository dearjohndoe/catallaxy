from mcp.server.fastmcp import FastMCP

CONTENT = """# Result Types

## string
{"result": {"type": "string", "data": "text output"}}
Rendered as Markdown.

## file
{"result": {"type": "file", "data": "<base64>", "mime_type": "image/png", "file_name": "output.png"}}
The sidecar replaces data with a url: {"type": "file", "url": "/download/uuid", "expires_in": 900}
Supported MIME types: png, jpeg, gif, webp, wav, mp3, ogg, mp4, webm, pdf.

## json
{"result": {"type": "json", "data": {"any": "structure"}}}

## bagid
{"result": {"type": "bagid", "data": "bag_hash_string"}}
For TON Storage.

## url
{"result": {"type": "url", "data": "https://example.com/resource"}}
"""

def register_result_types(mcp: FastMCP) -> None:
    @mcp.resource("catallaxy://spec/result-types")
    def result_types() -> str:
        """Agent response formats: string, file (base64→url), json, bagid, url."""
        return CONTENT
