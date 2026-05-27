from mcp.server.fastmcp import FastMCP

CONTENT = """# Создание агента для Catallaxy — пошаговое руководство

## 1. Scaffold

Используй tool `scaffold_agent` с параметрами:
- name: kebab-case имя (my-translator)
- capability: одно слово (translate)
- description: описание для маркетплейса
- price: цена в nanoTON (10000000 = 0.01 TON). Для динамической цены (has_quote=true) укажи 0.
- price_usd: опц., цена в micro-USDT (1000000 = 1 USDT). Если задана — добавляется USDT-рейл.
- args_schema: плоский dict `{field: {type, description, required: true}}`. UI ctlx.cc ждёт именно его. JSON Schema (`{type: "object", properties: ...}`) сайдкар поймёт, но форма на маркетплейсе сломается — scaffold_agent авто-конвертит, но в `agent.py` пиши сразу плоский.
- result_type: string | file | json | bagid | url
- has_quote: true если цена зависит от аргументов (см. режим quote в agent-contract)
- directory: путь куда положить файлы (по умолчанию agents-examples/{name})

Scaffold создаст `.env.example` с одним SKU `default` и infinite stock. Для нескольких SKU или конечного inventory отредактируй `AGENT_SKUS` в `.env` после scaffold (формат — `catallaxy://spec/sidecar-env`).

## 2. Реализуй логику

Открой `{directory}/agent.py` (путь возвращается в ответе scaffold_agent) и заполни секцию YOUR LOGIC HERE.

Если has_quote=true — реализуй также секцию quote mode (заглушка уже есть в файле).

## 3. Создай .env

Скопируй .env.example в .env и заполни:
- AGENT_WALLET_PK — приватный ключ кошелька (hex)
- AGENT_ENDPOINT — публичный URL где будет доступен сайдкар

AGENT_COMMAND=$SIDECAR_PYTHON — не трогай, сайдкар подставит нужный Python автоматически.

## 4. Валидируй

Используй tool `validate_agent` — проверит все обязательные параметры и запустит describe mode.

## 5. Протестируй

Используй tool `test_agent`:
- agent_dir: путь к директории агента
- test_body: тестовые аргументы

## 6. Деплой

Используй tool `deploy_agent` — установит и запустит systemd сервис.

ВАЖНО: флаг `--name` должен стоять ДО subcommand:
```bash
# Правильно:
sidecar.py service --name my-agent install

# Неправильно (ошибка):
sidecar.py service install --name my-agent
```

## 7. Мониторинг

- `agent_status` — статус сервиса
- `agent_logs` — логи
- `stop_agent` — остановить

## Архитектура агента (stdin/stdout)

Агент читает JSON из stdin и пишет JSON в stdout.
При ошибке — пишет в stderr и завершается с exit code != 0.
Сайдкар автоматически делает refund при ошибке агента.

Полный контракт: catallaxy://spec/agent-contract
"""

def register_create_guide(mcp: FastMCP) -> None:
    @mcp.resource("catallaxy://guide/create-agent")
    def create_guide() -> str:
        """Пошаговое руководство: scaffold → реализация → .env → validate → test → deploy."""
        return CONTENT
