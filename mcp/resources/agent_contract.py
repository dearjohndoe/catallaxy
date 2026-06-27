from mcp.server.fastmcp import FastMCP

CONTENT = """# Agent Contract — stdin/stdout

A Catallaxy agent is any executable (Python, Node, Go, Rust, bash) that:
- Reads JSON from stdin
- Writes JSON to stdout
- On error: writes to stderr and exits with exit code != 0

## Modes

### 1. describe (required)

stdin: {"mode": "describe"}
stdout:
  {
    "args_schema": <see formats below>,
    "result_schema": {"type": "string | file | json | bagid | url", "mime_type": "image/png"}
  }

Timeout: 3 s.

#### args_schema format

Use the flat format — it's what the ctlx.cc marketplace UI expects:

  {
    "field_name": {
      "type": "string | number | integer | boolean | file",
      "description": "Human-readable description",
      "required": true
    }
  }

JSON Schema (`{"type": "object", "properties": {...}, "required": [...]}`) is also
parsed by the sidecar, but it doesn't map onto the ctlx.cc form — the field simply
won't appear. `scaffold_agent` auto-converts JSON Schema to flat on input, but in
the finished `agent.py` always use the flat format.

### 2. execute (required)

stdin: {"capability": "translate", "body": {"text": "Hello", "target_language": "ru"}}
stdout (string): {"result": {"type": "string", "data": "Привет"}}
stdout (file): {"result": {"type": "file", "data": "<base64>", "mime_type": "image/png", "file_name": "output.png"}}
stdout (json): {"result": {"type": "json", "data": {"key": "value"}}}

Timeout: AGENT_FINAL_TIMEOUT (default 1200 s).

### 3. quote (optional, AGENT_HAS_QUOTE=true)

Called before payment — the agent returns the live price based on the arguments.
The client sees the price and plan before sending any TON.

stdin:  {"mode": "quote", "capability": "buy_stars", "sku": "premium", "body": {"stars_count": 100}}
stdout: {"price": 150000000, "price_usdt": 200000, "plan": "100 stars for @user — 0.15 TON", "ttl": 300}

- price: price in nanoTON (must be an integer > 0)
- price_usdt: optional, price in micro-USDT (for the USDT rail; > 0 if given)
- plan: string shown to the user (what they get for the money)
- ttl: quote lifetime in seconds (the sidecar stores it and uses it on invoke)
- sku: SKU id from the request — use it to price the specific variant

exit code != 0 in quote mode → the client gets an error and no payment is started.

### 4. prices (optional, for dynamic SKUs)

Called by the sidecar when a SKU is priced `ton=0`/`usd=0` (the dynamic-pricing
sentinel; `ton=dynamic`/`usd=dynamic` in AGENT_SKUS is the same thing). Returns the
current price per SKU without doing any work.

stdin:  {"mode": "prices"}
stdout: {"premium_3m": {"ton": 1000000000, "usd": 1500000}, "premium_6m": {"ton": 1800000000}}

Cached by the sidecar, see AGENT_SYNC_TIMEOUT.

## Errors

- stderr + exit code != 0 → the sidecar auto-refunds the client
- Causes: timeout, invalid_response, execution_failed, internal_error
- Refund amount = payment - REFUND_FEE_NANOTON (default 0.0005 TON gas)
"""

def register_agent_contract(mcp: FastMCP) -> None:
    @mcp.resource("catallaxy://spec/agent-contract")
    def agent_contract() -> str:
        """Agent stdin/stdout contract: describe / execute / quote / prices modes, response formats, error handling."""
        return CONTENT
