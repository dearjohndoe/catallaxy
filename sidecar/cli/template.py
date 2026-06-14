_CTLX_SUFFIX = "-ctlx-agent"
# Marketplace category slugs. Dotted slug = subcategory (games.topup → parent games).
# An agent may declare several, comma-separated; the first one is primary.
_CAPABILITIES = [
    "games.topup", "games.keys", "games.accounts",
    "subscriptions", "gift-cards", "telegram", "topup",
    "esim", "prepaid", "software", "vpn",
    "ai.chat", "ai.image", "ai.video", "tools",
    "ton.analytics", "ton.infra", "services",
]

_AGENT_TEMPLATE = '''\
import json
import sys


def describe() -> dict:
    # Return the schema of arguments your agent accepts.
    # Used for marketplace UI and request validation.
    return {
        "args_schema": {
            "text": {"type": "string", "description": "Input text", "required": True},
        }
    }


def run(body: dict) -> dict:
    # TODO: implement your agent logic here.
    # body contains the fields declared in describe().
    text = body.get("text", "")
    return {"result": f"echo: {text}"}


if __name__ == "__main__":
    request = json.loads(sys.stdin.read())
    if request.get("mode") == "describe":
        print(json.dumps(describe()))
    else:
        print(json.dumps(run(request.get("body", {}))))
'''
