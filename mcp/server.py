"""Catallaxy MCP Server — gives LLM full autonomy over Catallaxy marketplace."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from mcp.server.fastmcp import FastMCP
from tools.discovery import register_discovery_tools
from tools.invocation import register_invocation_tools
from tools.development import register_development_tools
from resources.agent_contract import register_agent_contract
from resources.sidecar_env import register_sidecar_env
from resources.payment_protocol import register_payment_protocol
from resources.result_types import register_result_types
from resources.create_guide import register_create_guide
from resources.gotchas import register_gotchas
from resources.agent_skill import register_agent_skill

from config import REGISTRY_ADDRESS  # noqa: F401 — re-exported for convenience

mcp = FastMCP("Catallaxy")

register_discovery_tools(mcp)
register_invocation_tools(mcp)
register_development_tools(mcp)
register_agent_contract(mcp)
register_sidecar_env(mcp)
register_payment_protocol(mcp)
register_result_types(mcp)
register_create_guide(mcp)
register_gotchas(mcp)
register_agent_skill(mcp)


@mcp.prompt()
def catallaxy_quickstart() -> str:
    """How to work with the Catallaxy MCP — read at the start of every session."""
    return """# Catallaxy MCP — getting started

Before using the tools, read the resource that fits your task:

| Task | Resource |
|------|----------|
| Create an agent from scratch | catallaxy://guide/create-agent |
| Full playbook: build + deploy to prod | catallaxy://guide/agent-skill |
| Understand the stdin/stdout contract (describe/execute/quote modes) | catallaxy://spec/agent-contract |
| Sort out the .env variables | catallaxy://spec/sidecar-env |
| Debug test/validate errors | catallaxy://guide/gotchas |
| Understand the payment flow (402, TX, quote) | catallaxy://spec/payment-protocol |
| Pick an agent result type | catallaxy://spec/result-types |

## Key rules

- `AGENT_COMMAND=$SIDECAR_PYTHON agent.py` — don't change it; the sidecar substitutes the right Python itself
- `has_quote=true` → the agent must implement mode=quote and return `{"price": int_nanoton, "plan": "...", "ttl": 300}`
- exit code != 0 in any mode → the sidecar auto-refunds the client
- Write agent logs to stderr or a file; stdout is reserved for the protocol
"""


if __name__ == "__main__":
    mcp.run()
