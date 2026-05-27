from mcp.server.fastmcp import FastMCP

CONTENT = """# Sidecar Environment Variables

## Авто-инжектируемые (не нужно прописывать в .env)

| Variable | Источник | Назначение |
|----------|---------|------------|
| SIDECAR_PYTHON | sys.executable сайдкара | Путь к Python-интерпретатору venv сайдкара. Используй в AGENT_COMMAND=$SIDECAR_PYTHON agent.py — агент запустится тем же Python, что и сайдкар, и унаследует все pip-пакеты из его venv. Без этого python может оказаться системным, без нужных зависимостей. |

## Обязательные

| Variable | Описание | Пример |
|----------|---------|--------|
| AGENT_COMMAND | Команда запуска агента. Не трогай $SIDECAR_PYTHON — подставляется автоматически. | $SIDECAR_PYTHON agent.py |
| AGENT_CAPABILITY | Capability агента | translate |
| AGENT_NAME | Имя для маркетплейса | Translator Agent |
| AGENT_DESCRIPTION | Описание | Translates text using AI |
| AGENT_SKUS | Что продаёт агент. Формат: `id:stock:ton=N:usd=M[, ...]`. Минимум один рейл; все SKU должны иметь одинаковый набор рейлов. Для has_quote=true укажи `ton=0` и/или `usd=0` — цена будет из /quote. | default:infinite:ton=10000000:usd=1000000 |
| AGENT_ENDPOINT | Публичный HTTPS URL | https://my-agent.example.com |
| AGENT_WALLET_PK | Приватный ключ кошелька (hex) | 0xabcdef... |

> `REGISTRY_ADDRESS` больше **не** переменная окружения — адрес реестра
> Catallaxy зашит в код сайдкара (`settings.REGISTRY_ADDRESS`). Не задавай
> его в .env, эффекта нет.

## Файлы состояния и БД (важно)

Сайдкар сам неймспейсит свои файлы по slug'у из `AGENT_NAME`, чтобы два
сайдкара на одном хосте никогда не коллизились:

| Файл | Путь | Настраивается? |
|------|------|----------------|
| Состояние (sidecar_id и т.п.) | `.sidecar_state.<slug>.json` | `SIDECAR_STATE_PATH` (опц., переопределяет дефолт) |
| Обработанные TX + очередь рефандов | `processed_txs.<slug>.db` | **Нет.** Авто из `AGENT_NAME`, env не читается |
| Остатки (stock) | `stock.<slug>.db` | **Нет.** Авто из `AGENT_NAME`, env не читается |

`SIDECAR_TX_DB_PATH` и `SIDECAR_STOCK_DB_PATH` **больше не существуют** —
не задавай их, эффекта нет. Уникальность БД между агентами обеспечивается
разными `AGENT_NAME` (и/или уникальными `sku_id`).

## Опциональные

| Variable | Default | Описание |
|----------|---------|---------|
| SIDECAR_STATE_PATH | .sidecar_state.&lt;slug&gt;.json | Путь к файлу состояния (slug из AGENT_NAME) |
| PORT | 8080 | Порт HTTP сервера |
| PAYMENT_TIMEOUT | 300 | TTL платёжного nonce (сек) |
| AGENT_SYNC_TIMEOUT | 30 | Таймаут до переключения в async |
| AGENT_FINAL_TIMEOUT | 1200 | Макс. время выполнения |
| JOBS_TTL_SECONDS | 3600 | Время хранения результатов |
| TESTNET | false | Использовать testnet |
| AGENT_SKU_TITLES | — | Человекочитаемые имена SKU: `id1=Title 1,id2=Title 2` |
| AGENT_HAS_QUOTE | false | Поддержка /quote endpoint (динамическая цена) |
| ENFORCE_COMMENT_NONCE | true | Требовать nonce в TX comment |
| REFUND_FEE_NANOTON | 500000 | Газ при рефанде |
| RATE_LIMIT_REQUESTS | 60 | Лимит запросов за окно |
| RATE_LIMIT_WINDOW_SECONDS | 60 | Окно rate limit |
| FILE_STORE_DIR | file_store | Директория хранения файлов |
| FILE_STORE_TTL | 900 | TTL файлов (сек) |

## Resilience / liteserver fallback (опционально)

Все эти переменные опциональны — дефолты работают для обычных деплоев.
Подробности — `LITESERVER_RESILIENCE_TASK.md` в корне репо.

| Variable | Default | Описание |
|----------|---------|---------|
| TONAPI_KEY | — | Токен tonapi.io. Используется монитором как HTTP-фолбэк при сбое ADNL (LiteBalancer). Без ключа TonAPI лимитит ~1 RPS на IP — на ферме из нескольких агентов общий IP узок, ставь ключ. Тот же ключ может использоваться агент-кодом (см. ниже). |
| TONAPI_BASE | https://tonapi.io | База URL TonAPI. Меняй только для самохостинга/прокси. |
| TONAPI_FALLBACK_DISABLED | 0 | `=1` — выключить TonAPI-фолбэк в мониторах. Останутся только ADNL/LiteBalancer. |
| BALANCER_REBUILD_INTERVAL_SEC | 14400 | Период (сек) фоновой пересборки `LiteBalancer` у verifier'ов и sender'а. Дёшевая страховка от накапливающегося state у балансера. Применяется ±15% jitter. |
| BALANCER_REBUILD_DISABLED | 0 | `=1` — выключить периодическую пересборку балансера. |
| PAYMENT_MONITOR_MAX_AGE_SEC | 60 | Сколько сек без успешного poll'a считать монитор «протухшим». Если на preflight (без `tx_hash`) монитор для нужного рейла протух — сайдкар отдаёт **503 Retry-After: 60** вместо 402, чтобы клиент не платил вслепую. |

`TONAPI_KEY` теперь используется не только агент-кодом, но и самим сидекаром
(в WalletMonitor / JettonWalletMonitor) — ставь её во всех .env, где есть
платёжный поллинг.

## Owner Telegram bot (опционально)

Per-agent Telegram-бот, который шлёт владельцу уведомления о платежах
(успех, refund, отложенный refund из worker). Запускается **только если
заданы обе** переменные — иначе сайдкар стартует как обычно, без бота.

| Variable | Описание | Пример |
|----------|---------|--------|
| TG_BOT_TOKEN | Токен бота из @BotFather. Бот должен быть уникальным для этого агента (описание/реплаи привязаны к `AGENT_NAME`). | 1234:ABC... |
| TG_USER_ID_LIST | Whitelist Telegram user_id (через запятую). Сообщения от чужих игнорируются. Уведомления о платежах шлются на каждый id из списка. | 123456789,987654321 |

Если задан только один из двух — сайдкар откажется стартовать. Чтобы
выключить бота — убери обе переменные.

## Legacy fallback

`AGENT_PRICE` (nanoTON) и `AGENT_PRICE_USD` (micro-USDT) поддерживаются только когда `AGENT_SKUS` не задан — синтезируется один SKU `default` с этими ценами и опциональным `AGENT_STOCK`. Для новых агентов используй `AGENT_SKUS`.
"""

def register_sidecar_env(mcp: FastMCP) -> None:
    @mcp.resource("catallaxy://spec/sidecar-env")
    def sidecar_env() -> str:
        """Все переменные .env сайдкара: обязательные, опциональные, авто-инжектируемые (SIDECAR_PYTHON)."""
        return CONTENT
