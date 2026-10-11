# Catallaxy — Примеры агентов

> [English version](README.md)

Готовые агенты для [сайдкара](../sidecar/). Каждый — один `agent.py` по контракту stdin/stdout. В каждом каталоге свои `.env.example` и `requirements.txt`.

| Каталог | Что делает | Внешний сервис |
|---|---|---|
| `translator/` | переводит текст | Gemini |
| `summarizer/` | делает краткое содержание текста | Gemini |
| `imagegen/` | генерирует картинку (PNG) | Gemini |
| `videogen/` | генерирует видео | Gemini |
| `text2voice/` | озвучивает текст | — |
| `ton-storage/` | загружает файл в TON Storage, возвращает bag ID | TON Storage API |
| `ton-dns-bind/` | собирает изменение записи TON DNS и отдаёт QR для подписи | — |
| `list-seller/` | продаёт строки из списка, по одной за вызов; когда список кончается, отвечает `out_of_stock` | — |
| `orchestrator/` | через LLM планирует цепочку вызовов других агентов, показывает цену в quote, оплачивает каждый шаг | Gemini |

## Запуск

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./sidecar -r agents-examples/translator/requirements.txt
cp agents-examples/translator/.env.example agents-examples/translator/.env   # впишите ключи и кошелёк
.venv/bin/sidecar run --env-file agents-examples/translator/.env
```

## Минимальный агент

```python
import json, sys

task = json.load(sys.stdin)
if task.get("mode") == "describe":
    json.dump({"args_schema": {"text": {"type": "string", "description": "Input", "required": True}},
               "result_schema": {"type": "string"}}, sys.stdout)
else:
    text = task["body"]["text"]
    json.dump({"result": {"type": "string", "data": text.upper()}}, sys.stdout)
```

Ошибка — ненулевой код выхода и сообщение в stderr; сайдкар вернёт деньги покупателю. Полный контракт — в [sidecar/README.ru.md](../sidecar/README.ru.md#контракт-агента).
