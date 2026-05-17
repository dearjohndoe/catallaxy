"""MCP resource: the catallaxy-agent build/deploy playbook.

Serves the vendored skill (skills/catallaxy-agent/SKILL.md at the repo root)
so any LLM connected to the MCP can pull the full playbook — project layout,
agent.py contract, pricing, deploy pipeline, gotchas — without a browser.

The file is read at request time (not embedded) so there is a single source
of truth and no drift. If the MCP server runs outside the repo checkout, a
short pointer is returned instead of crashing.
"""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

_SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "catallaxy-agent" / "SKILL.md"

_FALLBACK = (
    "# catallaxy-agent skill\n\n"
    "Playbook not found next to this MCP server. It lives in the repo at "
    "`skills/catallaxy-agent/SKILL.md` — open it there.\n"
)


def register_agent_skill(mcp: FastMCP) -> None:
    @mcp.resource("catallaxy://guide/agent-skill")
    def agent_skill() -> str:
        """Полный плейбук: собрать, задеплоить и проверить агента на проде (skills/catallaxy-agent)."""
        try:
            return _SKILL_PATH.read_text(encoding="utf-8")
        except OSError:
            return _FALLBACK
