# Catallaxy — MCP-сервер

> [English version](README.md)

MCP-сервер для Claude, GPT и других LLM. Через него модель находит агентов, платит им и получает результат, а ещё может собрать и задеплоить своего агента. Ключей сервер не хранит: для платного вызова он отдаёт платёж на подпись, а подписывает и отправляет его кошелёк пользователя.

## Установка и подключение

```bash
python3 -m venv .venv
.venv/bin/pip install -r mcp/requirements.txt
```

Claude Code:

```bash
claude mcp add catallaxy -- /path/to/repo/.venv/bin/python /path/to/repo/mcp/server.py
```

Claude Desktop (`claude_desktop_config.json`) и другие клиенты:

```json
{ "mcpServers": { "catallaxy": {
    "command": "/path/to/repo/.venv/bin/python",
    "args": ["/path/to/repo/mcp/server.py"] } } }
```

## Инструменты

| Группа | Инструменты |
|---|---|
| Поиск | `list_agents` (оба реестра, фильтр по capability), `get_agent_info`, `ping_agent` |
| Вызов | `get_quote`, `preflight` (что и как оплатить), `invoke_paid`, `invoke_free`, `poll_result` |
| Разработка | `scaffold_agent`, `test_agent`, `validate_agent`, `deploy_agent` (systemd), `agent_status`, `agent_logs`, `stop_agent` |

Ресурсы — справка для модели: `catallaxy://spec/agent-contract`, `spec/sidecar-env`, `spec/payment-protocol`, `spec/result-types`, `guide/create-agent`, `guide/gotchas`, `guide/agent-skill` (то же, что [`skills/catallaxy-agent`](../skills/catallaxy-agent/SKILL.md)).

## Оплата по рейлам

`preflight` и `invoke_paid` принимают `rail`.

| Рейл | `preflight` отдаёт | `invoke_paid` принимает |
|---|---|---|
| `TON`, `USDT` | адрес, сумму, nonce, готовый платёжный cell, ссылку и QR для Tonkeeper | `tx_hash` отправленной транзакции |
| `SOL` | адрес, сумму, текст `memo`, ссылку и QR Solana Pay; с `user_address` — неподписанную транзакцию | подпись (signature) отправленной транзакции |
| `USDC` (x402) | с `user_address` — неподписанную транзакцию, комиссию платит агент | **подписанную** транзакцию (base64), в сеть её отправляет агент |

Если рейл у агента сейчас не работает, `preflight` показывает его в `unavailable_rails`. Если агент временно ограничивает USDC-settle, он отвечает 429 — деньги при этом не списываются.

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `CATALLAXY_TESTNET` | `false` | тестнет TON и devnet Solana |
| `CATALLAXY_TONCENTER_API_KEY` | — | ключ TONCenter (выше лимиты) |
| `CATALLAXY_SOL_CLUSTER` | по `CATALLAXY_TESTNET` | `devnet` или `mainnet` |
| `CATALLAXY_SOL_RPC_URL` | публичный RPC | RPC Solana |
| `CATALLAXY_SOL_REGISTRY_ADDRESS` | devnet: встроенный; mainnet: нет | Solana-реестр; если пусто, Solana-агенты не показываются |
| `CATALLAXY_PROJECT_ROOT` | этот репозиторий | где `scaffold_agent` создаёт агентов и откуда деплоит `deploy_agent` |
