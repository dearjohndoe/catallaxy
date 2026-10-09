from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from settings import load_settings

from ..discovery import _resolve_service_name
from ..shell import _run_command, _systemctl_command
from ..systemd import render_systemd_unit

logger = logging.getLogger("sidecar")


def handle_service_install(args: argparse.Namespace) -> int:
    service_name = _resolve_service_name(args, for_install=True)
    if service_name is None:
        return 1

    workdir = str(Path(args.workdir).resolve())
    env_file = str(Path(args.env_file).resolve())
    python_bin = str(Path(sys.executable).absolute())
    sidecar_path = str(Path(args.sidecar_path).resolve())
    unit_path = Path(f"/etc/systemd/system/{service_name}.service")

    env_path = Path(env_file)
    if not env_path.exists():
        print(f"Env file not found: {env_file}")
        return 1

    try:
        env_path.chmod(0o600)
    except OSError as exc:
        print(f"Warning: could not set secure permissions on {env_file}: {exc}")

    unit_content = render_systemd_unit(service_name, workdir, env_file, python_bin, sidecar_path)

    try:
        unit_path.write_text(unit_content, encoding="utf-8")
    except PermissionError:
        print(f"Permission denied writing {unit_path}. Run command with sudo.")
        return 1

    if _run_command(["systemctl", "daemon-reload"]) != 0:
        return 1
    if _run_command(["systemctl", "enable", "--now", f"{service_name}.service"]) != 0:
        return 1

    print(json.dumps({"installed": True, "service": f"{service_name}.service"}, ensure_ascii=False))
    return 0


def handle_service_uninstall(args: argparse.Namespace) -> int:
    service_name = _resolve_service_name(args)
    if service_name is None:
        return 1

    unit_path = Path(f"/etc/systemd/system/{service_name}.service")

    _run_command(_systemctl_command(service_name, "stop"))
    _run_command(_systemctl_command(service_name, "disable"))

    try:
        if unit_path.exists():
            unit_path.unlink()
    except PermissionError:
        print(f"Permission denied deleting {unit_path}. Run command with sudo.")
        return 1

    if _run_command(["systemctl", "daemon-reload"]) != 0:
        return 1

    removed_files = []
    if getattr(args, "env_file", None):
        try:
            settings = load_settings(args.env_file)
            for path in [settings.state_path, settings.tx_db_path]:
                p = Path(path)
                if p.exists():
                    p.unlink()
                    removed_files.append(str(p))
        except Exception as exc:
            print(f"Warning: could not clean up state files: {exc}")

    print(json.dumps({"uninstalled": True, "service": f"{service_name}.service", "removed_files": removed_files}, ensure_ascii=False))
    return 0


def handle_service_command(args: argparse.Namespace) -> int:
    if args.service_command == "install":
        return handle_service_install(args)
    if args.service_command == "uninstall":
        return handle_service_uninstall(args)

    service_name = _resolve_service_name(args)
    if service_name is None:
        return 1

    if args.service_command == "logs":
        cmd = ["journalctl", "-u", f"{service_name}.service", "-n", str(args.lines)]
        if args.follow:
            cmd.append("-f")
        return _run_command(cmd)

    if args.service_command == "restart" and getattr(args, "force_heartbeat", False):
        try:
            settings = load_settings(args.env_file)
            from storage import StateStore
            store = StateStore(settings.state_path)
            state = store.load()
            state.last_heartbeat = None
            state.last_sol_heartbeat = None
            store.save(state)
            logger.info("Cleared last_heartbeat — fresh heartbeat will be sent after restart")
        except Exception as exc:
            print(f"Warning: could not clear heartbeat state: {exc}")

    mapping = {"start": "start", "stop": "stop", "restart": "restart", "status": "status"}
    action = mapping.get(args.service_command)
    if action is None:
        print("Unknown service command")
        return 1
    return _run_command(_systemctl_command(service_name, action))
