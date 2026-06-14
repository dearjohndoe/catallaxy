from __future__ import annotations

import argparse
from pathlib import Path

from ..template import _CAPABILITIES
from ..wallet import _generate_wallet_keypair


def handle_init(args: argparse.Namespace, _prefill: dict[str, str] | None = None) -> int:
    output = getattr(args, "output", ".env")
    prefill = _prefill or {}
    directory = Path(getattr(args, "directory", "."))

    print("=== sidecar init ===\n")

    agent_name = input("Agent name (AGENT_NAME): ").strip()
    if not agent_name:
        print("Name is required")
        return 1

    print("Description (AGENT_DESCRIPTION, Ctrl+D when done):")
    desc_lines: list[str] = []
    try:
        while True:
            desc_lines.append(input(""))
    except EOFError:
        pass
    agent_description = "\n".join(desc_lines).strip()

    if "capability" in prefill:
        capability = prefill["capability"]
        print(f"Capability: {capability}")
    else:
        print("\nCapability (category slug; several allowed, comma-separated, first = primary):")
        for i, c in enumerate(_CAPABILITIES, 1):
            print(f"  {i}) {c}")
        cap_raw = input("Numbers or custom slugs (e.g. '1,4' or 'games.topup,games'): ").strip()
        parts = []
        for token in cap_raw.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                parts.append(_CAPABILITIES[int(token) - 1])
            except (ValueError, IndexError):
                parts.append(token)
        capability = ",".join(dict.fromkeys(parts))
    if not capability:
        print("Capability is required")
        return 1

    price_str = input("\nPrice in TON (e.g. 0.01, leave blank to skip): ").strip()
    price_nanoton: int | None = None
    if price_str:
        try:
            price_nanoton = int(float(price_str) * 1_000_000_000)
            if price_nanoton <= 0:
                raise ValueError
        except ValueError:
            print(f"Invalid price: {price_str!r}")
            return 1

    usd_prompt = "Price in USDT (e.g. 1.0, leave blank to skip): " if price_nanoton is not None \
        else "Price in USDT (e.g. 1.0, required — TON price was skipped): "
    usd_str = input(usd_prompt).strip()
    price_usdt: int | None = None
    if usd_str:
        try:
            price_usdt = int(float(usd_str) * 1_000_000)
            if price_usdt <= 0:
                raise ValueError
        except ValueError:
            print(f"Invalid USDT price: {usd_str!r}")
            return 1
    elif price_nanoton is None:
        print("At least one price rail is required. Enter TON or USDT price.")
        return 1

    endpoint = input("Endpoint URL (e.g. https://my-agent.com): ").strip()
    if not endpoint:
        print("Endpoint is required")
        return 1

    agent_command = input(f"Agent command [$SIDECAR_PYTHON {directory}/agent.py]: ").strip() or f"$SIDECAR_PYTHON {directory}/agent.py"

    print("\nWallet private key:")
    print("  1) Enter existing hex key")
    print("  2) Generate new keypair")
    pk_choice = input("Choice [1/2]: ").strip()

    wallet_pk = ""
    wallet_seed = ""
    wallet_address = ""

    if pk_choice == "2":
        wallet_pk, wallet_seed, wallet_address = _generate_wallet_keypair()
        print(f"\nNew wallet generated!")
        print(f"  Address: {wallet_address}")
        if wallet_seed:
            print(f"  Seed:    {wallet_seed}")
            print("  SAVE THE SEED PHRASE in a safe place!\n")
        else:
            print("  (pytoniq_core not installed — no seed phrase; save the private key!)\n")
    else:
        wallet_pk = input("AGENT_WALLET_PK (hex): ").strip()
        if not wallet_pk:
            print("Private key is required")
            return 1
        try:
            from settings import _derive_wallet_address
            wallet_address = _derive_wallet_address(wallet_pk, False)
            print(f"  Wallet address: {wallet_address}")
        except Exception as exc:
            print(f"  Warning: could not derive wallet address: {exc}")

    if "\n" in agent_description:
        _desc_escaped = agent_description.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        _desc_line = f'AGENT_DESCRIPTION="{_desc_escaped}"'
    else:
        _desc_line = f"AGENT_DESCRIPTION={agent_description}"

    lines = [
        f"AGENT_NAME={agent_name}",
        _desc_line,
        f"AGENT_CAPABILITY={capability}",
        f"AGENT_ENDPOINT={endpoint}",
        f"AGENT_COMMAND={agent_command}",
        f"AGENT_WALLET_PK={wallet_pk}",
        f"AGENT_WALLET_SEED={wallet_seed}",
        "",
        "PORT=8080",
        "TESTNET=false",
    ]
    insert_at = 4
    if price_nanoton is not None:
        lines.insert(insert_at, f"AGENT_PRICE={price_nanoton}")
        insert_at += 1
    if price_usdt is not None:
        lines.insert(insert_at, f"AGENT_PRICE_USD={price_usdt}")

    out_path = Path(output)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out_path.chmod(0o600)

    print(f"\n.env written to {out_path}")
    if wallet_address:
        print(f"Wallet address: {wallet_address}")
        print("Fund the wallet with TON before starting the agent!")

    return 0
