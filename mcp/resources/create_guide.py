from mcp.server.fastmcp import FastMCP

CONTENT = """# Creating a Catallaxy agent — step-by-step guide

## 1. Scaffold

Use the `scaffold_agent` tool with these params:
- name: kebab-case name (my-translator)
- capability: one word (translate). AGENT_CAPABILITY also accepts a comma-separated list; the first entry is primary.
- description: marketplace description
- price: price in nanoTON (10000000 = 0.01 TON). For dynamic pricing (has_quote=true) pass 0.
- price_usd: optional, price in micro-USDT (1000000 = 1 USDT). If set, a USDT rail is added.
- args_schema: flat dict `{field: {type, description, required: true}}`. The ctlx.cc UI expects exactly this. JSON Schema (`{type: "object", properties: ...}`) is understood by the sidecar but breaks the marketplace form — scaffold_agent auto-converts it, but write the flat format directly in `agent.py`.
- result_type: string | file | json | bagid | url
- result_mime_type: optional, required for result_type=file (e.g. image/png)
- has_quote: true if the price depends on the arguments (see quote mode in agent-contract)
- directory: where to write the files (default agents-examples/{name})

Scaffold creates `.env.example` with a single SKU `default` and infinite stock. For multiple SKUs or finite inventory, edit `AGENT_SKUS` in `.env` after scaffold (format — `catallaxy://spec/sidecar-env`).

## 2. Implement the logic

Open `{directory}/agent.py` (the path is returned by scaffold_agent) and fill in the YOUR LOGIC HERE section.

If has_quote=true, also implement the quote mode section (a stub is already in the file).

## 3. Create .env

Copy .env.example to .env and fill in:
- TON_WALLET_PK (legacy: AGENT_WALLET_PK) — wallet private key (hex)
- AGENT_ENDPOINT — public URL where the sidecar will be reachable

AGENT_COMMAND=$SIDECAR_PYTHON — don't touch it; the sidecar substitutes the right Python automatically.

## 4. Validate

Use the `validate_agent` tool — it checks all required params and runs describe mode.

## 5. Test

Use the `test_agent` tool:
- agent_dir: path to the agent directory
- test_body: test arguments

## 6. Deploy

Use the `deploy_agent` tool — it installs and starts the systemd service.

IMPORTANT: the `--name` flag must come BEFORE the subcommand:
```bash
# Correct:
sidecar.py service --name my-agent install

# Wrong (error):
sidecar.py service install --name my-agent
```

## 7. Monitor

- `agent_status` — service status
- `agent_logs` — logs
- `stop_agent` — stop it

## Agent architecture (stdin/stdout)

The agent reads JSON from stdin and writes JSON to stdout.
On error it writes to stderr and exits with exit code != 0.
The sidecar auto-refunds the client when the agent errors.

Full contract: catallaxy://spec/agent-contract
"""

def register_create_guide(mcp: FastMCP) -> None:
    @mcp.resource("catallaxy://guide/create-agent")
    def create_guide() -> str:
        """Step-by-step guide: scaffold → implement → .env → validate → test → deploy."""
        return CONTENT
