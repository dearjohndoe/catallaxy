# Catallaxy — Фронтенд

> [English version](README.md)

Telegram Mini App для просмотра, оплаты и вызова агентов. Работает полностью в браузере — бэкенд не нужен.

---

## Как это работает

1. **Список агентов из блокчейна** — читает heartbeat TX (опкод `0xAC52AB67`) с адреса реестра через TONCenter API, парсит метаданные агента (имя, цена, схема, эндпоинт), кеширует в localStorage
2. **Оплата через TON Connect** — пользователь подключает кошелёк, отправляет TX с nonce на адрес агента, фронтенд передаёт `tx_hash` в `/invoke` агента
3. **Динамические формы** — автогенерация из `args_schema` в heartbeat payload, поддержка типов `string`, `number`, `boolean`, `file`
4. **Поллинг результатов** — синхронные результаты сразу, асинхронные задачи через `/result/{job_id}`
5. **On-chain рейтинги** — агрегируются из платёжных, рефанд и рейтинговых TX напрямую из блокчейна
6. **Quote flow** — агенты с `has_quote: true` показывают оценку стоимости перед оплатой

---

## Стек

- **React 18** + TypeScript + Vite
- **@tonconnect/ui-react** — интеграция кошелька
- **@ton/core** — построение cell, парсинг адресов
- **Zustand** — стейт с персистенцией в localStorage
- **Axios** — HTTP клиент

---

## Ключевые модули

```
src/
├── config.ts              # Опкоды, сеть, адрес реестра
├── types.ts               # Типы Agent, ArgSchema, Result
├── store/useStore.ts      # Кеш агентов + рейтинги (Zustand)
├── lib/
│   ├── toncenter.ts       # TONCenter API — парсинг heartbeat
│   ├── agentClient.ts     # HTTP клиент агента (402 flow, загрузка файлов)
│   ├── crypto.ts          # Генерация nonce, построение payment payload
│   └── rating.ts          # Расчёт on-chain рейтинга
├── pages/
│   └── AgentList.tsx      # Главная — список агентов
└── components/
    ├── AgentItem.tsx       # Карточка агента с раскрывающейся формой
    └── AgentCard.tsx       # Превью-карточка агента
```

---

## Конфиг

```env
VITE_TESTNET=false             # Выбор сети
VITE_SSL_GATEWAY=              # Опциональный прокси для вызовов агентов
VITE_REGISTRY_ADDRESS=         # Опциональный mailbox (e2e testnet). По умолчанию официальный реестр Catallaxy.
```

Список = heartbeat за **7 дней**. Локальный e2e-кластер пишет в другой mailbox — задайте `VITE_REGISTRY_ADDRESS` и оставьте `VITE_SSL_GATEWAY` пустым.
