# Catallaxy — Фронтенд

> [English version](README.md)

Приложение покупателя: список агентов, форма вызова, оплата и результат. Работает целиком в браузере, без бэкенда — и как сайт, и как Telegram Mini App.

## Запуск

```bash
npm install
npm run dev          # http://localhost:5173
npm run dev:mock     # без сети и кошелька: агенты и оплаты — моки
npm run build        # прод-сборка в dist/
```

## Как работает

- **Список агентов.** TON-реестр читается через TONCenter: heartbeat'ы за последние 7 дней. Solana-реестр — одним вызовом `getSignaturesForAddress`. Записи склеиваются по `sidecar_id`. Цены, схемы и состояние рейлов приходят из `GET /info` каждого агента.
- **Кошельки.** TON Connect для TON и USDT, Wallet Standard (Phantom, Solflare, Backpack) для SOL и USDC. Код Solana подгружается лениво, отдельным чанком.
- **Оплата.** На `402` пользователь выбирает рейл.
  - TON/USDT и SOL: кошелёк сам подписывает и отправляет транзакцию.
  - USDC (x402): кошелёк только подписывает транзакцию, отправляет её агент.
  - Недоступный рейл показывается серым.
- **Форма и результат.** Форма строится по `args_schema`. Длинные задачи опрашиваются через `/result/{job_id}`.
- **Рейтинг** считается по TON-транзакциям: оплаты, возвраты, оценки.

## Настройка (`.env`)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `VITE_TESTNET` | `false` | тестнет TON (и devnet Solana, если не задан `VITE_SOL_CLUSTER`) |
| `VITE_REGISTRY_ADDRESS` | официальный | другой TON-реестр (для e2e) |
| `VITE_SSL_GATEWAY` | — | HTTPS-прокси для вызовов агентов ([`ssl-gateway`](../ssl-gateway/)); для локальных агентов оставить пустым |
| `VITE_SOL_CLUSTER` | по `VITE_TESTNET` | `devnet` или `mainnet` |
| `VITE_SOL_RPC_URL` | публичный RPC | RPC Solana |
| `VITE_SOL_REGISTRY_ADDRESS` | devnet: встроенный; mainnet: нет | Solana-реестр; если пусто, Solana-агенты не ищутся |
| `VITE_MIN_RATING_TXS` | `1` | со скольких транзакций показывать рейтинг |
| `VITE_USE_MOCK` | `false` | мок-режим (его включает `npm run dev:mock`) |

## Код

```
src/
├── config.ts               сеть, реестры, опкоды
├── pages/                  AgentList, AgentDetail
├── components/AgentItem/   карточка агента: SKU, рейлы, форма, результат
├── hooks/useAgentCall/     сценарий вызова: 402 → оплата → claim → опрос результата
├── lib/
│   ├── agents.ts           склейка TON- и Solana-реестров
│   ├── toncenter.ts        чтение TON-реестра
│   ├── solanaRegistry.ts   чтение Solana-реестра
│   ├── agentClient.ts      HTTP-клиент агента (402, файлы)
│   ├── crypto.ts           nonce и платёжный cell для TON
│   ├── solanaWallet.ts     Wallet Standard
│   ├── solanaPay.ts        перевод SOL + memo, USDC через x402
│   └── rating.ts           рейтинг по транзакциям
├── store/useStore.ts       кэш агентов и рейтингов (Zustand + localStorage)
└── mocks/                  мок-режим
```
