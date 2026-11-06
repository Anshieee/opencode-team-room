"""Small standalone command entry point for the OpenCode team room plugin."""
from __future__ import annotations

import argparse
import sys

from .room import configure_room_parser, dispatch_room


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opencode-team-room")
    commands = parser.add_subparsers(dest="room_action")
    configure_room_parser(commands, include_open=False)
    args = parser.parse_args(argv)
    if not args.room_action:
        parser.print_help()
        return 0
    try:
        return dispatch_room(args)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"Team room failed: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
