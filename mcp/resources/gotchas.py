from mcp.server.fastmcp import FastMCP

CONTENT = """# Known gotchas when building agents

## test_agent / validate_agent use AGENT_COMMAND from .env

`run_agent` reads `AGENT_COMMAND` from the agent's `.env` and substitutes
`$SIDECAR_PYTHON` → the MCP server's `sys.executable`.
Make sure `.env` exists and `AGENT_COMMAND` is set before running these tools.

Manual check without the MCP:
```bash
echo '{"mode":"describe"}' | $SIDECAR_PYTHON agent.py
echo '{"capability":"...", "body":{...}}' | $SIDECAR_PYTHON agent.py
```

---

## TonAPI: nested objects can arrive as strings

TonAPI sometimes returns nested objects (nft, collection, contract) as a bare
address string instead of an object — when the entity is unknown or not indexed.

Example from /v2/accounts/{address}/events:

```json
// What you expect:
{"nft": {"address": "EQ...", "metadata": {"name": "Cool NFT"}}}

// What you get for unknown NFTs:
{"nft": "EQ..."}
```

Guard the fields nft, collection, contract, jetton, account:

```python
# WRONG — raises AttributeError:
name = event["nft"]["metadata"]["name"]

# RIGHT:
nft = event.get("nft")
name = nft.get("metadata", {}).get("name", "NFT") if isinstance(nft, dict) else "NFT"

col_raw = item.get("collection")
col = (col_raw.get("name") if isinstance(col_raw, dict) else None) or "Unknown Collection"
```

Rule: always isinstance(x, dict) before .get() on nested TonAPI objects.

---

## Sidecar workdir on `run`

The sidecar looks for agent.py relative to its CWD. If you run from another directory:

```bash
# Wrong — looks for agent.py in the current folder:
cd /root/sidecar && sidecar.py run --env-file /root/generated-agent/.env

# Right:
cd /root/generated-agent && sidecar.py run --env-file .env
```

---

## Port already in use after a manual run

If the sidecar fails with "Address already in use":
```bash
lsof -i :<PORT> | grep LISTEN
kill -9 <PID>
```

---

## Concurrent requests inside an agent

The sidecar may run several agent instances at once for different clients.
Don't use asyncio.gather() against external APIs with rate limits — it creates a burst.
Make requests sequentially within a single agent call.
"""

def register_gotchas(mcp: FastMCP) -> None:
    @mcp.resource("catallaxy://guide/gotchas")
    def gotchas() -> str:
        """Known gotchas: AGENT_COMMAND/python path, TonAPI quirks, workdir, ports, concurrency."""
        return CONTENT
