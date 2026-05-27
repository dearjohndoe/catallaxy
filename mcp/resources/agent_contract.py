from mcp.server.fastmcp import FastMCP

CONTENT = """# Agent Contract — stdin/stdout

Агент Catallaxy — любой исполняемый файл (Python, Node, Go, Rust, bash), который:
- Читает JSON из stdin
- Пишет JSON в stdout
- При ошибке — пишет в stderr и завершается с exit code != 0

## Режимы

### 1. describe (обязательный)

stdin: {"mode": "describe"}
stdout:
  {
    "args_schema": <см. форматы ниже>,
    "result_schema": {"type": "string | file | json | bagid | url", "mime_type": "image/png"}
  }

Таймаут: 3 сек.

#### Формат args_schema

Используй плоский формат — его ждёт UI маркетплейса ctlx.cc:

  {
    "field_name": {
      "type": "string | number | integer | boolean | file",
      "description": "Human-readable description",
      "required": true
    }
  }

JSON Schema (`{"type": "object", "properties": {...}, "required": [...]}`) тоже
парсится сайдкаром, но в форму ctlx.cc он не маппится — поле просто не появится.
`scaffold_agent` авто-конвертит JSON Schema в плоский на входе, но в готовом
`agent.py` всегда используй плоский.

### 2. execute (обязательный)

stdin: {"capability": "translate", "body": {"text": "Hello", "target_language": "ru"}}
stdout (string): {"result": {"type": "string", "data": "Привет"}}
stdout (file): {"result": {"type": "file", "data": "<base64>", "mime_type": "image/png", "file_name": "output.png"}}
stdout (json): {"result": {"type": "json", "data": {"key": "value"}}}

Таймаут: AGENT_FINAL_TIMEOUT (default 1200 сек).

### 3. quote (опциональный, AGENT_HAS_QUOTE=true)

Вызывается перед оплатой — агент возвращает актуальную цену на основе аргументов.
Клиент видит цену и plan до того как отправит TON.

stdin:  {"mode": "quote", "capability": "buy_stars", "sku": "premium", "body": {"stars_count": 100}}
stdout: {"price": 150000000, "price_usdt": 200000, "plan": "100 stars for @user — 0.15 TON", "ttl": 300}

- price: цена в nanoTON (обязательно целое > 0)
- price_usdt: опц., цена в micro-USDT (для USDT-рейла; > 0 если указана)
- plan: строка для отображения пользователю (что будет сделано за эти деньги)
- ttl: время жизни квоты в секундах (сайдкар хранит и использует при вызове)
- sku: id SKU из запроса — используй для расчёта цены под конкретный вариант

exit code != 0 в quote mode → клиент получает ошибку, платёж не инициируется.

### 4. prices (опциональный, для динамических SKU)

Вызывается сайдкаром когда у SKU `ton=0` и `usd=0` (sentinel динамической цены).
Возвращает текущие цены для каждого SKU без выполнения работы.

stdin:  {"mode": "prices"}
stdout: {"premium_3m": {"ton": 1000000000, "usd": 1500000}, "premium_6m": {"ton": 1800000000}}

Кэшируется сайдкаром, см. AGENT_SYNC_TIMEOUT.

## Ошибки

- stderr + exit code != 0 → сайдкар авто-рефанд клиенту
- Причины: timeout, invalid_response, execution_failed, internal_error
- Сумма рефанда = оплата - 0.0005 TON (газ на возврат)
"""

def register_agent_contract(mcp: FastMCP) -> None:
    @mcp.resource("catallaxy://spec/agent-contract")
    def agent_contract() -> str:
        """stdin/stdout контракт агента: режимы describe / execute / quote, форматы ответов, обработка ошибок."""
        return CONTENT
