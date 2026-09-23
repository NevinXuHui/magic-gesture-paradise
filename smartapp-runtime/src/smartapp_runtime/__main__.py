import argparse
import asyncio
import sys
from typing import Optional, Sequence

from smartapp_runtime.bootstrap import run_runtime
from smartapp_runtime.config import load_config
from smartapp_runtime.domain.errors import sanitize_message


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smartapp-runtime")
    parser.add_argument("--config", required=True, help="path to runtime TOML configuration")
    parser.add_argument(
        "--check-config", action="store_true", help="validate configuration and exit"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        config = load_config(arguments.config)
        if arguments.check_config:
            print("configuration is valid")
            return 0
        asyncio.run(run_runtime(config))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        message = sanitize_message(str(error)) or "runtime failed"
        print("smartapp-runtime: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
