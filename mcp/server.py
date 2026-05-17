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
    """Инструкция по работе с Catallaxy MCP — читай в начале каждой сессии."""
    return """# Catallaxy MCP — с чего начать

Перед тем как использовать инструменты, прочти нужные ресурсы:

| Задача | Ресурс |
|--------|--------|
| Создать агента с нуля | catallaxy://guide/create-agent |
| Полный плейбук: собрать + задеплоить на прод | catallaxy://guide/agent-skill |
| Понять контракт stdin/stdout (режимы describe/execute/quote) | catallaxy://spec/agent-contract |
| Разобраться с переменными .env | catallaxy://spec/sidecar-env |
| Отладить ошибки test/validate | catallaxy://guide/gotchas |
| Понять платёжный flow (402, TX, quote) | catallaxy://spec/payment-protocol |
| Выбрать тип результата агента | catallaxy://spec/result-types |

## Ключевые правила

- `AGENT_COMMAND=$SIDECAR_PYTHON agent.py` — не меняй, сайдкар подставит нужный Python сам
- `has_quote=true` → агент должен реализовать mode=quote и возвращать `{"price": int_nanoton, "plan": "...", "ttl": 300}`
- exit code != 0 в любом режиме → сайдкар делает авто-рефанд клиенту
- Логи агента пиши в stderr или файл; stdout зарезервирован для протокола
"""


if __name__ == "__main__":
    mcp.run()
