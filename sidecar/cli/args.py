from __future__ import annotations

import argparse
from pathlib import Path


def parse_cli_args() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser], argparse.Namespace]:
    parser = argparse.ArgumentParser(description="TON Agent Marketplace sidecar CLI")
    subparsers = parser.add_subparsers(dest="command")
    parser_map: dict[str, argparse.ArgumentParser] = {}

    run_parser = subparsers.add_parser("run", help="Run sidecar HTTP server")
    run_parser.add_argument("--env-file", default=".env", help="Path to .env file")
    run_parser.add_argument("--force-heartbeat", action="store_true",
                            help="Clear last_heartbeat so a fresh heartbeat is sent on startup")
    parser_map["run"] = run_parser

    service_parser = subparsers.add_parser("service", help="Manage systemd service")
    service_parser.add_argument("--name", default=None,
                                help="Service name (without .service suffix); -ctlx-agent appended automatically. "
                                     "If omitted, auto-detected from installed services.")
    service_sub = service_parser.add_subparsers(dest="service_command", required=True)

    install_parser = service_sub.add_parser("install", help="Install + enable + start systemd service")
    install_parser.add_argument("--workdir", default=str(Path.cwd()), help="Working directory for service")
    install_parser.add_argument("--env-file", default=".env", help="Path to .env file")
    install_parser.add_argument("--sidecar-path",
                                default=str(Path(__file__).resolve().parent.parent / "sidecar.py"),
                                help="Path to sidecar.py")

    uninstall_parser = service_sub.add_parser("uninstall", help="Disable and remove systemd service")
    uninstall_parser.add_argument("--env-file", default=None, help="Path to .env file to clean up state files")
    service_sub.add_parser("start", help="Start service")
    service_sub.add_parser("stop", help="Stop service")
    restart_parser = service_sub.add_parser("restart", help="Restart service")
    restart_parser.add_argument("--force-heartbeat", action="store_true",
                                help="Clear last_heartbeat after restart")
    restart_parser.add_argument("--env-file", default=".env", help="Path to .env file (needed for --force-heartbeat)")
    service_sub.add_parser("status", help="Show service status")
    logs_parser = service_sub.add_parser("logs", help="Show service logs")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow logs")
    logs_parser.add_argument("--lines", type=int, default=200, help="Number of lines")
    parser_map["service"] = service_parser

    doctor_parser = subparsers.add_parser("doctor", help="Validate local sidecar setup")
    doctor_parser.add_argument("--env-file", default=".env", help="Path to .env file")
    parser_map["doctor"] = doctor_parser

    stock_parser = subparsers.add_parser("stock", help="Manage SKU stock")
    stock_parser.add_argument("--env-file", default=".env", help="Path to .env file")
    stock_sub = stock_parser.add_subparsers(dest="stock_command", required=True)
    stock_sub.add_parser("show", help="Show stock state for all SKUs")
    stock_set = stock_sub.add_parser("set", help="Set absolute total for a SKU")
    stock_set.add_argument("sku", help="SKU id")
    stock_set.add_argument("total", help="New total (int, or 'none'/'infinite' to disable tracking)")
    stock_add = stock_sub.add_parser("add", help="Increment/decrement SKU total by delta")
    stock_add.add_argument("sku", help="SKU id")
    stock_add.add_argument("delta", help="Integer delta (can be negative)")
    parser_map["stock"] = stock_parser

    init_parser = subparsers.add_parser("init", help="Interactive wizard to create a .env file")
    init_parser.add_argument("--output", default=".env", help="Output file path (default: .env)")
    parser_map["init"] = init_parser

    scaffold_parser = subparsers.add_parser("scaffold", help="Create a new agent directory with starter files")
    scaffold_parser.add_argument("directory", help="Directory to create")
    scaffold_parser.add_argument("--capability", default="", help="Pre-fill capability slug(s), comma-separated, first = primary (e.g. games.topup,games)")
    parser_map["scaffold"] = scaffold_parser

    help_parser = subparsers.add_parser("help", help="Show help")
    help_parser.add_argument("topic", nargs="?",
                             choices=["run", "service", "doctor", "stock", "init", "scaffold"],
                             help="Help topic")
    parser_map["help"] = help_parser

    args = parser.parse_args()
    return parser, parser_map, args
