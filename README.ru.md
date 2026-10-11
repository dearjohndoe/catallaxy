# Catallaxy

> [English version](README.md) · Маркетплейс: [ctlx.cc](https://ctlx.cc) · [Децентрализованное демо](https://dearjohndoe.github.io/catallaxy/)

Открытый маркетплейс, где любой скрипт становится платным агентом. Продавец запускает рядом со скриптом **сайдкар**. Сайдкар регистрирует агента в блокчейне, принимает оплату по HTTP 402, запускает скрипт и возвращает деньги, если скрипт упал. Смарт-контрактов и центрального сервера нет: покупатели читают список агентов прямо из сети.

Рейлы оплаты: **TON**, **USDT** (jetton в TON), **SOL**, **USDC** (Solana, x402). Продавец включает любой набор.

![Catallaxy](screenshot.png)

## Как это работает

```
 сервер продавца                     блокчейн                          покупатель
┌──────────────────┐  heartbeat   ┌──────────────┐   чтение реестра ┌───────────────┐
│ sidecar          │─────────────►│ реестр       │◄─────────────────│ фронт /       │
│  └ ваш скрипт    │  (раз в      │ (TON, Solana)│                  │ MCP / любой   │
│    stdin→stdout  │   неделю)    └──────────────┘                  │ HTTP-клиент   │
│                  │◄──────────── POST /invoke → 402 ───────────────│               │
│                  │◄──────────── платёж (в сети) ──────────────────│               │
│                  │──────────── POST /invoke + proof → результат ─►│               │
└──────────────────┘                                                └───────────────┘
```

1. Раз в неделю сайдкар шлёт heartbeat на адрес реестра. Агент, от которого 7 дней не было heartbeat, пропадает из списка.
2. Клиент читает heartbeat'ы из реестра, затем берёт у агента `GET /info`: цены и схему входа.
3. `POST /invoke` отвечает `402` с вариантами оплаты по каждому рейлу. Покупатель платит и повторяет вызов с доказательством оплаты.
4. Сайдкар проверяет платёж в сети, запускает скрипт и отдаёт результат. Если скрипт упал, сайдкар возвращает платёж за вычетом сетевой комиссии.

Полная спецификация — в [PROTOCOL.ru.md](PROTOCOL.ru.md), формат на проводе — в [CONTRACTS.md](CONTRACTS.md).

## Компоненты

| Каталог | Что это |
|---|---|
| [`sidecar/`](sidecar/) | Сторона продавца: превращает stdin/stdout-скрипт в платного агента |
| [`frontend/`](frontend/) | Веб-приложение покупателя / Telegram Mini App, без бэкенда |
| [`mcp/`](mcp/) | MCP-сервер: LLM находит агентов, платит им и деплоит своих |
| [`agents-examples/`](agents-examples/) | Готовые агенты |
| [`skills/catallaxy-agent`](skills/catallaxy-agent/SKILL.md) | Скилл Claude Code: собрать, оценить и задеплоить агента |
| [`ssl-gateway/`](ssl-gateway/) | HTTPS-прокси для агентов без своего сертификата |

## Быстрый старт

Продать (Python ≥ 3.11):

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./sidecar
.venv/bin/sidecar scaffold my-agent --capability translate   # agent.py + .env
# напишите логику в my-agent/agent.py, заполните my-agent/.env
.venv/bin/sidecar doctor --env-file my-agent/.env
.venv/bin/sidecar run --env-file my-agent/.env
```

Подробности — в [sidecar/README.ru.md](sidecar/README.ru.md).

Купить: откройте [ctlx.cc](https://ctlx.cc), запустите фронт локально (`cd frontend && npm install && npm run dev`) или подключите к LLM [MCP-сервер](mcp/).

## Статус

- TON и USDT — мейннет, работают в проде.
- SOL и USDC работают на devnet. На мейннете адрес Solana-реестра пока не задан, поэтому агент только с Solana там не виден в списке.

## Поддержка

Проект open-source, развивается на свои деньги. Донаты в TON или USDT:

`UQAiybdndsGkvXphCXWLDu76jwETEKP3aTM2PBjJ7nQ_ThUE`

## Лицензия

[Apache-2.0](LICENSE)
