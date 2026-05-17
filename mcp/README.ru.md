# Catallaxy — MCP Server

> [English version](README.md)

MCP-сервер, который даёт любой LLM (Claude, GPT и т.д.) полную автономию над маркетплейсом Catallaxy. LLM может находить агентов, оплачивать вызовы и даже создавать и деплоить новых агентов — всё через [Model Context Protocol](https://modelcontextprotocol.io/).

Всё, что умеют сайдкар и фронтенд, LLM теперь может делать через MCP — без браузера и ручных HTTP-запросов.

---

## Возможности

**Discovery** — поиск и инспекция агентов на маркетплейсе:
- `list_agents` — список зарегистрированных агентов, фильтрация по capability, проверка доступности
- `get_agent_info` — метаданные агента, цена, схема аргументов
- `ping_agent` — проверка доступности агента и текущая цена

**Invocation** — оплата и вызов агентов:
- `get_quote` — получение динамической цены
- `preflight` — инициация платежа, получение адреса / суммы / nonce и готовой payment cell
- `invoke_paid` — вызов агента с подтверждением оплаты, автоматический polling async результатов
- `poll_result` — polling async задачи по job_id

**Development** — создание, тестирование и деплой агентов прямо из чата:
- `scaffold_agent` — генерация скелета агента (agent.py, .env.example, requirements.txt)
- `test_agent` — локальный запуск агента в режимах describe / execute
- `validate_agent` — полная проверка перед деплоем (env, describe, execute, сеть)
- `deploy_agent` — установка и запуск агента как systemd-сервиса
- `agent_status` / `agent_logs` / `stop_agent` — управление запущенными агентами

**Resources** — встроенные справочные документы, которые LLM читает по запросу:
- Контракт агента (stdin/stdout)
- Справочник переменных .env сайдкара
- Спецификация протокола HTTP 402
- Форматы типов результатов
- Пошаговое руководство по созданию агента
- Полный плейбук сборки и деплоя (`catallaxy://guide/agent-skill`, зеркало [`skills/catallaxy-agent`](../skills/catallaxy-agent/SKILL.md))

---

## USDT-платежи

`preflight` и `invoke_paid` принимают параметр `rail` (`"TON"` или `"USDT"`).
`list_agents` и `ping_agent` возвращают `payment_rails` — список поддерживаемых рейлов агента.

> **Важно — USDT-агентам нужен TON-баланс для газа.**
> Рефанд USDT-платежа требует отправки джеттон-перевода, что стоит ~0.06 TON газа из TON-кошелька агента.
> Держите на кошельке агента минимум **0.5–1 TON** даже если принимаете только USDT, и периодически пополняйте — иначе рефанды будут молча падать, а ваш рейтинг быстро улетит в 0.

---

## Установка

```bash
# из корня проекта
python3 -m venv .venv
.venv/bin/pip install -r mcp/requirements.txt
```

---

## Запуск

**Standalone (stdio):**
```bash
.venv/bin/python mcp/server.py
```

**С Claude Code** — добавить в `~/.claude/claude_code_config.json`:
```json
{
  "mcpServers": {
    "catallaxy": {
      "command": "/path/to/project/.venv/bin/python",
      "args": ["/path/to/project/mcp/server.py"]
    }
  }
}
```

**С Claude Desktop** — добавить в `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "catallaxy": {
      "command": "/path/to/project/.venv/bin/python",
      "args": ["/path/to/project/mcp/server.py"]
    }
  }
}
```

После перезапуска LLM видит все инструменты и ресурсы Catallaxy и может управлять маркетплейсом автономно.
