import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

ARGS_SCHEMA = {
    "text": {
        "type": "string",
        "description": "Text to translate",
        "required": True,
    },
    "target_language": {
        "type": "string",
        "description": "Target language, e.g. 'Russian', 'Spanish', 'fr', 'de'",
        "required": True,
    },
}


def main():
    task = json.load(sys.stdin)

    if task.get("mode") == "describe":
        print(json.dumps({"args_schema": ARGS_SCHEMA, "result_schema": {"type": "string"}}))
        return

    body = task.get("body") or {}

    text = body.get("text", "").strip()
    if not text:
        raise ValueError("body.text must be a non-empty string")

    target_language = body.get("target_language", "").strip()
    if not target_language:
        raise ValueError("body.target_language must be a non-empty string")

    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    prompt = (
        f"Translate the following text to {target_language}. "
        "Return only the translated text, no explanations or extra content.\n\n"
        f"{text}"
    )
    # gpt-5.x rejects max_tokens; max_completion_tokens is the supported param
    response = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=16384,
    )

    print(json.dumps({"result": {"type": "string", "data": response.choices[0].message.content.strip()}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
