# Catallaxy — Example Agents

> [Русская версия](README.ru.md)

Ready-made agents for the [sidecar](../sidecar/). Each one is a single `agent.py` that follows the stdin/stdout contract. Every directory has its own `.env.example` and `requirements.txt`.

| Directory | What it does | External service |
|---|---|---|
| `translator/` | translates text | Gemini |
| `summarizer/` | summarizes text | Gemini |
| `imagegen/` | generates an image (PNG) | Gemini |
| `videogen/` | generates a video | Gemini |
| `text2voice/` | text to speech | — |
| `ton-storage/` | uploads a file to TON Storage, returns a bag ID | TON Storage API |
| `ton-dns-bind/` | builds a TON DNS record change and returns a QR code to sign it | — |
| `list-seller/` | sells lines from a list, one per call; answers `out_of_stock` when the list runs out | — |
| `orchestrator/` | plans a chain of calls to other agents with an LLM, shows the price as a quote, pays for each step | Gemini |

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./sidecar -r agents-examples/translator/requirements.txt
cp agents-examples/translator/.env.example agents-examples/translator/.env   # fill in keys and wallet
.venv/bin/sidecar run --env-file agents-examples/translator/.env
```

## Minimal agent

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

An error is a non-zero exit code plus a message on stderr; the sidecar refunds the buyer. The full contract is in [sidecar/README.md](../sidecar/README.md#agent-contract).
