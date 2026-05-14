from __future__ import annotations

import argparse
from pathlib import Path

from ..template import _AGENT_TEMPLATE
from .init_cmd import handle_init


def handle_scaffold(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    capability: str = getattr(args, "capability", "") or ""

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Cannot create directory {directory}: {exc}")
        return 1

    agent_py = directory / "agent.py"
    if agent_py.exists():
        print(f"{agent_py} already exists — skipping")
    else:
        agent_py.write_text(_AGENT_TEMPLATE, encoding="utf-8")
        print(f"Created {agent_py}")

    reqs = directory / "requirements.txt"
    if reqs.exists():
        print(f"{reqs} already exists — skipping")
    else:
        reqs.write_text("python-dotenv\n# Add your dependencies below\n", encoding="utf-8")
        print(f"Created {reqs}")

    env_file = directory / ".env"
    if env_file.exists():
        print(f"{env_file} already exists — skipping")
    else:
        print(f"\nCreating {env_file} ...")
        init_args = argparse.Namespace(output=str(env_file))
        prefill = {"capability": capability} if capability else {}
        rc = handle_init(init_args, _prefill=prefill)
        if rc != 0:
            return rc
        # Pin state_path explicitly so renaming AGENT_NAME later doesn't
        # orphan the agent's on-chain identity record.
        slug = directory.name
        with env_file.open("a", encoding="utf-8") as f:
            f.write(f"\nSIDECAR_STATE_PATH=.sidecar_state.{slug}.json\n")

    print(f"\nScaffold complete! Next steps:")
    print(f"  1. Edit {agent_py}")
    print(f"  2. sudo sidecar service install --name <agent-name> --env-file {env_file}")
    return 0
