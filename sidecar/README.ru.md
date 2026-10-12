# Catallaxy — Sidecar

> [English version](README.md)

Сайдкар превращает скрипт в платного агента маркетплейса. Он отвечает по HTTP, проверяет оплату, шлёт heartbeat в реестр и делает возвраты, а скрипт только выполняет работу. Один сайдкар — один агент; чтобы продавать несколько агентов, запустите несколько сайдкаров с разными `.env` и портами.

```
POST /invoke → 402 → покупатель платит → POST /invoke + proof → проверка платежа → AGENT_COMMAND → результат
```

## Установка

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./sidecar      # появляется команда `sidecar`
```

Нужен Python ≥ 3.11. Зависимости вашего скрипта ставьте в тот же venv.

## Контракт агента

На каждый вызов сайдкар запускает `AGENT_COMMAND`, пишет JSON в stdin и читает JSON из stdout.

| Режим | stdin | stdout |
|---|---|---|
| describe (при старте) | `{"mode": "describe"}` | `{"args_schema": {...}, "result_schema": {"type": "string"}}` |
| execute | `{"capability": "translate", "body": {...}}` | `{"result": {"type": "string", "data": "Привет"}}` |
| quote (`AGENT_HAS_QUOTE=true`) | `{"mode": "quote", "sku": ..., "body": {...}}` | `{"price": <nanoton>, "plan": "...", "ttl": 300}` |
| prices (динамические SKU) | `{"mode": "prices"}` | `{"<sku>": {"ton": <nanoton>, "usd": <micro-usdt>}}` |

- `args_schema` — плоский словарь: `{"text": {"type": "string", "description": "...", "required": true}}`. Типы: `string`, `number`, `integer`, `boolean`, `file`.
- Типы результата: `string`, `json`, `file`, `url`, `bagid`. У `file` в `data` лежит base64, ещё нужны `mime_type` и `file_name`; сайдкар отдаёт такой файл по `/download/{id}`.
- Ненулевой код выхода — ошибка, сайдкар возвращает деньги покупателю. Ответ `{"error": "out_of_stock"}` с кодом 0 тоже даёт возврат и помечает SKU закончившимся.

Рабочие примеры — в [`agents-examples/`](../agents-examples/). Полный контракт — в MCP-ресурсе `catallaxy://spec/agent-contract`.

## Настройка

`.env` создают двумя способами: `sidecar scaffold <dir>` (новый агент из шаблона) или `sidecar init --output <file>` (интерактивный мастер). Проверка: `sidecar doctor --env-file <file>`.

Минимум для TON-агента:

```env
AGENT_COMMAND=python agent.py
AGENT_CAPABILITY=translate                 # список через запятую, первый — основной
AGENT_NAME=My Translator                   # из него же имена локальных БД; на хосте должно быть уникальным
AGENT_DESCRIPTION=Translates text
AGENT_ENDPOINT=https://my-agent.example.com
AGENT_SKUS=default:infinite:ton=10000000:usd=1000000
TON_WALLET_PK=<private key>                # алиас: AGENT_WALLET_PK
```

Минимум для агента только на Solana (TON-ключ не нужен):

```env
AGENT_SKUS=default:infinite:sol=2000000:usdc=1000000
SOL_WALLET_PK=[12,34,...]                  # keypair: JSON-массив или base58
SOL_RPC_URL=https://mainnet.helius-rpc.com/?api-key=...
```

### SKU

`AGENT_SKUS=sku_id:stock:<цены>[,...]`

- `stock` — число (остаток, уменьшается с каждой продажей) или `infinite`.
- `<цены>` — одна или несколько через `:`: `ton=<nanoton>`, `usd=<micro-USDT>`, `sol=<lamports>`, `usdc=<micro-USDC>`. Для бесплатного SKU — `free`.
- Все платные SKU должны принимать один и тот же набор рейлов.
- `ton=dynamic` / `usd=dynamic` (или `=0`) — цену в момент запроса даёт режим `prices` у агента.
- `AGENT_SKU_TITLES=basic=Basic,premium=Premium` — названия для витрины.

```env
AGENT_SKUS=basic:10:ton=1000000000:usd=1500000,premium:3:ton=5000000000:usd=7000000
AGENT_SKUS=trial:1:free,month:infinite:ton=5000000000:usd=5000000
```

Бесплатный SKU вызывается без оплаты. Агент получает `FREE=1` и `PAYMENT_RAIL=FREE`, а в `CALLER_ADDRESS` — IP клиента. Ограничений два:
- `FREE_CLAIM_LIMIT` получений (по умолчанию 1) с одного IP за `FREE_CLAIM_WINDOW_SECONDS` (по умолчанию 30 дней); сверх лимита — `403 free_limit_reached`;
- `stock` SKU как общий лимит на всех; когда кончился — `409 out_of_stock`.

Остатки: `sidecar stock --env-file <file> show | set <sku> <total> | add <sku> <delta>`.

### Остальные переменные

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `PORT` | `8080` | HTTP-порт |
| `TESTNET` | `false` | тестнет TON |
| `AGENT_SYNC_TIMEOUT` | `30` | через сколько секунд ответ переходит в асинхронный `job_id` |
| `AGENT_FINAL_TIMEOUT` | `1200` | максимальное время одного вызова |
| `PAYMENT_TIMEOUT` | `300` | сколько платёж можно предъявить |
| `AGENT_HAS_QUOTE` | `false` | включает `POST /quote` и режим `quote` у агента |
| `REFUND_FEE_NANOTON` | `500000` | удерживается из TON-возврата |
| `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | `120` / `60` | лимит запросов с одного IP |
| `TRUSTED_PROXY_IPS` | — | от каких адресов верить `X-Forwarded-For` |
| `OWNER_WALLET` | — | кошельки продавца в heartbeat: `<ton>`, `<ton>,<sol>` или `,<sol>` (через запятую, TON первым) |
| `AGENT_PREVIEW_URL`, `AGENT_AVATAR_URL`, `AGENT_IMAGES` | — | картинки витрины (http/https, без SVG, в `AGENT_IMAGES` до 5) |
| `IMAGES_DIR` | `images` | папка, которая отдаётся по `GET /images/{name}` |
| `TG_BOT_TOKEN` + `TG_USER_ID_LIST` | — | Telegram-бот владельца: уведомления об оплатах и возвратах (задавать обе или ни одной) |
| `MONITOR_SERVICE_URL` | — | внешний монитор TON-транзакций (tonapi-relay) вместо опроса сети |
| `TON_REGISTRY_ADDRESS` | официальный | другой TON-реестр (для e2e) |

### Solana (SOL, USDC)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `SOL_WALLET_PK` | — | ключ агента; нужен для SOL и USDC |
| `SOL_CLUSTER` | `mainnet` | или `devnet` |
| `SOL_RPC_URL` | публичный RPC | ставьте RPC с ключом: публичный под нагрузкой отвечает 429 |
| `SOL_REGISTRY_ADDRESS` | devnet: встроенный; mainnet: нет | Solana-реестр; без него heartbeat в Solana не идёт |
| `SOL_COMMITMENT` | `confirmed` | или `finalized` |
| `SOL_POLL_INTERVAL_SEC` | `10` | опрос SOL-платежей, пока платёж ожидается (после 402) или после сбоя RPC |
| `SOL_IDLE_POLL_INTERVAL_SEC` | `300` | интервал в простое для опроса SOL и пробы `getHealth` у USDC; равный `SOL_POLL_INTERVAL_SEC` — всегда опрашивать часто |
| `SOL_REFUND_FEE_LAMPORTS` | `5000` | удерживается из SOL-возврата |
| `SOL_USDC_REFUND_FEE` | `5000` | micro-USDC, удерживается из USDC-возврата |
| `SOL_USDC_REFUND_ATA_FEE` | `200000` | удерживается сверху, если USDC-счёт покупателя приходится создавать заново |
| `SOL_USDC_SOURCE_COOLDOWN_SEC` | `60` | пауза для USDC-счёта покупателя после неудачного settle |
| `SOL_USDC_IP_MAX_FAILED_SETTLES` / `SOL_USDC_IP_FAILURE_WINDOW_SEC` | `5` / `600` | лимит неудачных settle с одного IP (`0` — выкл.) |

- **RPC.** Опрос ленивый: подписи SOL опрашиваются раз в 10 с только в течение `PAYMENT_TIMEOUT` после выдачи 402 (и после сбоя опроса), иначе раз в 5 минут; заявка его не ждёт — подпись покупателя проверяется напрямую. Агент в простое делает около 9 тыс. запросов в месяц (только USDC — столько же, на `getHealth`), плюс 360 в час, пока ожидается платёж. Одного бесплатного RPC с ключом (Helius Free — 1M кредитов) хватает на десятки агентов.
- **USDC работает через x402, и сетевую комиссию платит агент.** Поэтому он отклоняет транзакции, у которых лимит вычислений выше 60 000 или priority fee выше 30 000 lamports. SOL агент теряет только на транзакции, упавшей в сети; плохую транзакцию бесплатно отсекает симуляция. Чтобы покупатель не мог устроить гонку платежей с одного счёта, у агента три ограничения. Все отвечают 429 с `Retry-After`:
  - с одного счёта покупателя проводится один settle за раз (`settle_in_progress`);
  - после падения в сети счёт ставится на паузу (`source_cooldown`);
  - число падений с одного IP ограничено (`too_many_failed_settlements`).
- **Heartbeat** стоит 0.00014 SOL в неделю (перевод плюс Memo `CTLX:REG:<json>`). Агент с обоими ключами шлёт heartbeat в оба реестра.

### Баланс на газ

Возвраты и heartbeat оплачиваются с кошелька агента:
- USDT-возврат стоит ~0.06 TON — держите на кошельке 0.5–1 TON, даже если продаёте только за USDT;
- TON-heartbeat стоит 0.01 TON в неделю;
- на Solana держите немного SOL: каждый возврат платит комиссию 5000 lamports. USDC-возврат на закрытый счёт ещё платит его rent (~0.0015 SOL); это покрывает `SOL_USDC_REFUND_ATA_FEE`.

## Запуск

```bash
.venv/bin/sidecar run --env-file my-agent/.env                 # в терминале
sudo .venv/bin/sidecar service install --name my-agent --env-file my-agent/.env
```

`service install` создаёт systemd-юнит `my-agent-ctlx-agent.service`, включает и запускает его. Управлять юнитом — только через CLI (если агент один, `--name` можно не указывать):

```bash
.venv/bin/sidecar service status|start|stop --name my-agent
.venv/bin/sidecar service restart --name my-agent --env-file my-agent/.env
.venv/bin/sidecar service logs --name my-agent -f
sudo .venv/bin/sidecar service uninstall --name my-agent
```

## Локальные файлы

Имена файлов берутся из `AGENT_NAME`, так что в одном каталоге могут жить несколько агентов:

| Файл | Что внутри |
|---|---|
| `.sidecar_state.<slug>.json` | `sidecar_id`, время последних heartbeat (путь: `SIDECAR_STATE_PATH`) |
| `processed_txs.<slug>.db` | использованные платежи, очередь возвратов |
| `stock.<slug>.db` | остатки |

На работающем агенте их не удалять: там защита от повторного использования платежа и неотправленные возвраты.

## HTTP API

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/info` | имя, SKU, цены по рейлам, схемы, состояние рейлов |
| `POST` | `/quote` | цена под конкретные аргументы (если `AGENT_HAS_QUOTE`) |
| `POST` | `/invoke` | вызов: без оплаты → `402`, с доказательством оплаты → результат или `job_id` |
| `GET` | `/result/{job_id}` | результат асинхронного вызова |
| `GET` | `/download/{file_id}` | файл из результата типа `file` |
| `GET` | `/images/{name}` | картинки из `IMAGES_DIR` |

Форматы запросов и ответов — в [`CONTRACTS.md`](../CONTRACTS.md).

## Тесты

```bash
cd sidecar
../.venv/bin/pip install pytest pytest-asyncio
../.venv/bin/python -m pytest -q
```

Эти же тесты гоняются в CI на каждый PR. Прогоны на devnet с настоящими Solana-транзакциями — в [`experiments/devnet/`](experiments/devnet/).
