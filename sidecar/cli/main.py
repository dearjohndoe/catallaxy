from __future__ import annotations

import asyncio
import logging
import os
import sys

from settings import load_settings

from .args import parse_cli_args
from .commands.doctor import handle_doctor
from .commands.init_cmd import handle_init
from .commands.scaffold import handle_scaffold
from .commands.service import handle_service_command
from .commands.stock import handle_stock_command
from .server import run_server

logger = logging.getLogger("sidecar")


async def async_main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

    parser, parser_map, args = parse_cli_args()

    if args.command in {None, "run"}:
        env_file = getattr(args, "env_file", ".env")
        settings = load_settings(env_file)
        if getattr(args, "force_heartbeat", False):
            from storage import StateStore
            store = StateStore(settings.state_path)
            state = store.load()
            state.last_heartbeat = None
            state.last_sol_heartbeat = None
            store.save(state)
            logger.info("Cleared last_heartbeat — fresh heartbeat will be sent on startup")
        return await run_server(settings)

    if args.command == "service":
        return handle_service_command(args)

    if args.command == "doctor":
        return handle_doctor(args)

    if args.command == "stock":
        return await handle_stock_command(args)

    if args.command == "init":
        return handle_init(args)

    if args.command == "scaffold":
        return handle_scaffold(args)

    if args.command == "help":
        if args.topic:
            parser_map[args.topic].print_help()
        else:
            parser.print_help()
        return 0

    parser.print_help()
    return 1


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
