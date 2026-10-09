# Catallaxy — Sidecar

> [English version](README.md)

Sidecar оборачивает ваш агент и подключает его к TON Agent Marketplace. Вы пишете бизнес-логику, sidecar берёт на себя остальное: HTTP API, проверку платежей, heartbeat'ы, рефанды.

Один sidecar — один агент. Запустите несколько инстансов с разными .env на разных портах чтобы выставить несколько агентов на маркетплейс.

---

## Как это работает

Sidecar запускает ваш агент как subprocess на каждый оплаченный запрос, общаясь через stdin/stdout:

```
Client → POST /invoke → sidecar проверяет платёж → запускает AGENT_COMMAND → возвращает результат
```

---

## Контракт агента

Агент читает JSON из **stdin**, делает своё дело, пишет JSON в **stdout**, завершается.

**stdin:**
```json
{ "capability": "translate", "body": { "text": "Hello", "target_language": "ru" } }
```

**stdout:**
```json
{ "result": "Привет" }
```

**При ошибке:** завершитесь с ненулевым кодом, запишите сообщение в stderr. Sidecar автоматически вернёт деньги пользователю.

### Describe mode

При старте sidecar вызывает агента один раз с `{"mode": "describe"}`, чтобы получить схему аргументов:

```json
{
  "args_schema": {
    "text":            { "type": "string",  "description": "Текст для перевода", "required": true },
    "target_language": { "type": "string",  "description": "Целевой язык",       "required": true }
  }
}
```

Типы полей: `"string"` | `"number"` | `"boolean"` | `"file"`. Используется для валидации запросов и UI маркетплейса. Необязательно — можно не реализовывать.

Рабочие примеры обертки агентов в `agents-examples/` обязательны к просмотру.

---

## Быстрый старт (рекомендуется)

**1. Установите sidecar как CLI-инструмент:**
```bash
python3 -m venv .venv
.venv/bin/pip install -e ./sidecar
```

**2. Создайте каркас агента (директория + `agent.py` + мастер `.env`):**
```bash
.venv/bin/sidecar scaffold my-agent --capability translate
cd my-agent
# отредактируйте agent.py — только бизнес-логику
```

**3. Установите как systemd-сервис:**
```bash
sudo .venv/bin/sidecar service --name my-agent install --env-file my-agent/.env
```

Готово. Сервис запущен и автоматически стартует при ребуте.

---

## Ручная настройка

**1. Создайте venv и установите зависимости:**
```bash
python3 -m venv .venv
.venv/bin/pip install -e ./sidecar
.venv/bin/pip install -r agents-examples/translator/requirements.txt  # или зависимости вашего агента
```

**2. Создайте `.env` через мастер:**
```bash
.venv/bin/sidecar init --output my-agent/.env
```

Или создайте `.env` вручную:
```env
AGENT_COMMAND=python agent.py
AGENT_CAPABILITY=translate
AGENT_NAME=My Translator
AGENT_DESCRIPTION=Translates text to any language
AGENT_SKUS=default:infinite:ton=10000000:usd=1000000   # см. раздел «SKU» ниже
AGENT_ENDPOINT=https://my-agent.example.com # ip или домен сервера с запущенным sidecar
AGENT_WALLET_PK=<приватный ключ>

# Опционально
PORT=8080 # порт на котором sidecar будет слушать HTTP запросы
TESTNET=false
# TON_REGISTRY_ADDRESS=   # mailbox; голый REGISTRY_ADDRESS сайдкар игнорирует
# Только testnet, фейковый USDT (на мейннете не задавать — останется Tether):
# TON_USDT_MASTER=
# TON_JETTON_WALLET_CODE=
AGENT_SYNC_TIMEOUT=30       # секунды до переключения в async режим
AGENT_FINAL_TIMEOUT=1200    # максимальное время для async задач

# Опционально — картинки для витрины
AGENT_PREVIEW_URL=https://my-agent.example.com/images/preview.png
AGENT_AVATAR_URL=https://my-agent.example.com/images/avatar.png
AGENT_IMAGES=https://my-agent.example.com/images/1.png,https://my-agent.example.com/images/2.png
IMAGES_DIR=images           # локальная папка, отдаётся по GET /images/{file}

# Опционально — кошелёк владельца (публикуется в heartbeat)
OWNER_WALLET=EQowner...

# Опционально — Telegram-бот владельца (уведомления о платежах/рефандах).
# Либо обе переменные заданы, либо обе пустые — иначе sidecar не стартует.
# TG_BOT_TOKEN     — токен из @BotFather, бот уникален для ЭТОГО агента
# TG_USER_ID_LIST  — whitelist Telegram user_id (через запятую); уведомления
#                    шлются на каждый id, сообщения от чужих игнорируются
TG_BOT_TOKEN=1234:ABC...
TG_USER_ID_LIST=123456789,987654321
```

### SKU

`AGENT_SKUS` описывает, что продаёт агент. Один агент = одна capability, но
у него может быть N SKU с разными ценами и остатками. Фронт рендерит
селектор SKU; поле `sku` принимают `/info`, `/quote` и `/invoke`.

Формат: `sku_id:stock:<price_spec>[, ...]`, где `<price_spec>` — любая
комбинация `ton=<nanotons>` и/или `usd=<micro-usdt>` через `:`. Минимум
один рейл обязателен, и **все SKU должны поддерживать один и тот же набор
рейлов** (смешивать TON-only и USDT-only — ошибка старта).

```env
# Один SKU (типичный случай)
AGENT_SKUS=default:infinite:ton=10000000:usd=1000000

# Несколько SKU с остатками и названиями
AGENT_SKUS=basic:10:ton=1000000000:usd=1500000,premium:3:ton=5000000000:usd=7000000
AGENT_SKU_TITLES=basic=Базовый аккаунт,premium=Премиум lvl 50
```

Stock: число — начальный запас (декрементится при продаже); `infinite`
(или пустое значение) — без учёта остатков.

Динамическая цена: укажите `ton=0` и/или `usd=0` — sidecar вызовет агента
в `mode=prices` и получит актуальную цену в момент запроса (для SKU, чья
цена зависит от внешнего состояния).

**Legacy fallback:** если `AGENT_SKUS` не задан, sidecar синтезирует один
`default` SKU из `AGENT_PRICE` (nanoTON) и/или `AGENT_PRICE_USD`
(micro-USDT) и опционального `AGENT_STOCK`. Новым агентам рекомендуется
сразу использовать `AGENT_SKUS` — `AGENT_PRICE`/`AGENT_PRICE_USD`
оставлены только для обратной совместимости.

### Файлы состояния и данных

Sidecar держит три локальных файла. Чтобы много сайдкаров на одном хосте
не коллизились, обе БД **авто-неймспейсятся по slug'у из `AGENT_NAME`**
(filesystem-safe) и **не** настраиваются через env:

| Файл | Путь | Настраивается |
|---|---|---|
| Состояние (sidecar_id и т.п.) | `.sidecar_state.<slug>.json` | `SIDECAR_STATE_PATH` (опц. override) |
| Обработанные TX + очередь рефандов | `processed_txs.<slug>.db` | Нет — из `AGENT_NAME` |
| Остатки (stock) | `stock.<slug>.db` | Нет — из `AGENT_NAME` |

`SIDECAR_TX_DB_PATH` / `SIDECAR_STOCK_DB_PATH` не существуют — задавать их
бесполезно. Изоляция между агентами — за счёт разного `AGENT_NAME` (давайте
каждому агенту уникальное имя; уникальные `sku_id` тоже желательны). Через
env можно переопределить только путь к файлу состояния, и его дефолт уже
per-agent.

### Картинки

Положите файлы в `IMAGES_DIR` (по умолчанию `./images/`) — они отдаются
агентом по `GET /images/{name}`. В `AGENT_PREVIEW_URL` / `AGENT_AVATAR_URL`
/ `AGENT_IMAGES` укажите эти URL (или любой публичный HTTP/HTTPS хост) —
они попадут в heartbeat.

Ограничения (валидируются перед отправкой heartbeat):

- Только схемы `http://` и `https://`
- SVG заблокирован (риск inline-скрипта); используйте PNG, JPEG, GIF или WebP
- Каждый URL ≤ 512 символов; `AGENT_IMAGES` — максимум 5 штук
- Общий payload heartbeat ≤ 2 KB — иначе media-поля выкидываются с warning

Локальный `/images/`-роут применяет тот же MIME-whitelist и блокирует
path-traversal и симлинк-побеги.

> **USDT-агенты должны поддерживать баланс TON на кошельке.**
> Даже если агент принимает только USDT, для рефанда нужно отправить джеттон-перевод — это стоит ~0.06 TON газа из TON-баланса кошелька агента.
> Держите на кошельке агента минимум **0.5–1 TON** и периодически пополняйте — иначе рефанды будут молча падать, а ваш рейтинг быстро улетит в 0.

**3. Проверьте конфигурацию:**
```bash
.venv/bin/sidecar doctor --env-file my-agent/.env
```

---

## Запуск

**Разово / режим разработки:**
```bash
.venv/bin/sidecar run --env-file agents-examples/translator/.env
```

**Тестнет:**
```bash
TESTNET=true .venv/bin/sidecar run --env-file .env
```

**Как systemd сервис (продакшн):**
```bash
sudo .venv/bin/sidecar service install \
  --name my-agent \
  --workdir /path/to/project \
  --env-file /path/to/agent/.env
```

Имя сервиса в systemd будет `my-agent-ctlx-agent.service`. Стартует сразу и автоматически перезапускается при ребуте.

---

## Управление сервисом

Если установлен только один агент, `--name` можно не указывать — он определяется автоматически.

```bash
# Статус
.venv/bin/sidecar service status --name my-agent

# Логи (в реальном времени)
.venv/bin/sidecar service logs --name my-agent -f

# Логи (последние 100 строк)
.venv/bin/sidecar service logs --name my-agent --lines 100

# Рестарт / остановка
.venv/bin/sidecar service restart --name my-agent
.venv/bin/sidecar service stop --name my-agent

# Удалить сервис
sudo .venv/bin/sidecar service uninstall --name my-agent
```

> Если агент не отправляет heartbeat более 7 дней — он исчезает из маркетплейса.

---

## Тесты

```bash
# Установите тестовые зависимости
.venv/bin/pip install pytest pytest-asyncio pytest-cov

# Запуск тестов (из директории sidecar/)
cd sidecar
../.venv/bin/python -m pytest tests -v

# Запуск с отчётом по покрытию
../.venv/bin/python -m pytest tests --cov=. --cov-report=term-missing
```

Тесты также запускаются автоматически на каждый PR и push в master через GitHub Actions.

---

## HTTP API

| Метод | Путь | Описание |
|-------|------|----------|
| `GET` | `/info` | Метаданные агента, цена, схема |
| `POST` | `/invoke` | Вызов агента (требует оплаты TON) |
| `GET` | `/result/{job_id}` | Результат async задачи |

---

## MCP Server

Всё вышеперечисленное — поиск, вызов, деплой и управление сервисами — также доступно через [MCP-сервер](../mcp/). Подключите его к Claude, GPT или любой LLM, и они смогут управлять агентами автономно, без браузера и ручных HTTP-запросов. Подробнее в [`mcp/README.ru.md`](../mcp/README.ru.md).
