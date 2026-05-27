AGENT_TEMPLATE = '''\
import json
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

ARGS_SCHEMA = {args_schema}

RESULT_SCHEMA = {result_schema}


def main():
    task = json.load(sys.stdin)

    if task.get("mode") == "describe":
        print(json.dumps({{
            "args_schema": ARGS_SCHEMA,
            "result_schema": RESULT_SCHEMA,
        }}))
        return

    body = task.get("body") or {{}}
{quote_block}
    # --- YOUR LOGIC HERE ---
    result = ""
    # --- END ---

    print(json.dumps({{"result": {{"type": "{result_type}", "data": result}}}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
'''

QUOTE_BLOCK = '''\
    if task.get("mode") == "quote":
        # Return dynamic price in nanoTON based on body contents.
        # {"price": int_nanoton, "plan": "human-readable plan", "ttl": seconds}
        # exit(1) here = no quote, client sees an error (no payment taken).
        raise NotImplementedError("quote mode not implemented")

'''

ENV_EXAMPLE_TEMPLATE = '''\
AGENT_COMMAND=$SIDECAR_PYTHON agent.py
AGENT_CAPABILITY={capability}
AGENT_NAME={name}
AGENT_DESCRIPTION={description_escaped}
AGENT_SKUS={skus_spec}
AGENT_ENDPOINT=https://your-server.example.com

# Wallet — generate via scripts/gen_wallet.py. Keep PK + SEED + address
# together in this file: PK signs heartbeat/refund txs, SEED lets the owner
# recover or import the wallet into Tonkeeper/MyTonWallet, the address line
# is a human-readable reference (no need to derive it from PK every time).
# Do NOT commit this file — .env stays on the deploy server only.
AGENT_WALLET_PK=0x...your_private_key_hex...
AGENT_WALLET_SEED=word1 word2 ... word24
# Wallet address (reference): UQ...

# Payouts go here. If unset, payouts go to the agent wallet above.
# OWNER_WALLET=UQ...

# State/DB files auto-namespace from AGENT_NAME (.sidecar_state.<slug>.json,
# processed_txs.<slug>.db, stock.<slug>.db). Override only the state file if
# needed: SIDECAR_STATE_PATH=...
PORT=8080
TESTNET=false
AGENT_HAS_QUOTE={has_quote}
'''

REQUIREMENTS_TEMPLATE = '''\
python-dotenv>=1.0.0
'''

VALID_ARG_TYPES = {"string", "number", "boolean", "file"}


def _extract_fields(args_schema: dict) -> dict:
    """Normalise args_schema to a flat {field: {type, description}} dict.

    Accepts both the legacy flat format and standard JSON Schema
    (type=object + properties).  Returns the flat dict for validation.
    """
    if args_schema.get("type") == "object" and "properties" in args_schema:
        return args_schema["properties"]
    return args_schema
