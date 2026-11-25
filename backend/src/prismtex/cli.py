"""The `prismtex` command line."""

from __future__ import annotations

import argparse
import sys
from typing import Any

import uvicorn

from prismtex import __version__
from prismtex.settings import Settings, is_loopback_host


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prismtex", description="PrismTeX server.")
    parser.add_argument("--version", action="version", version=f"prismtex {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    serve = commands.add_parser("serve", help="run the API server")
    serve.add_argument("--host", help="interface to bind (default: PRISMTEX_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port to bind (default: PRISMTEX_PORT or 8000)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return serve(host=args.host, port=args.port)
    return 2  # argparse rejects unknown commands before this


def serve(*, host: str | None = None, port: int | None = None) -> int:
    overrides: dict[str, Any] = {}
    if host is not None:
        overrides["host"] = host
    if port is not None:
        overrides["port"] = port
    settings = Settings(**overrides)
    if not is_loopback_host(settings.host) and settings.token is None:
        print(
            f"Refusing to bind {settings.host} without PRISMTEX_ACCESS_TOKEN. "
            "Set a token, or bind 127.0.0.1.",
            file=sys.stderr,
        )
        return 2

    _harden_process()

    # Imported here so `prismtex --help` stays fast and import errors in the
    # app surface only when serving.
    from prismtex.main import create_app

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        proxy_headers=False,
        server_header=False,
    )
    return 0


def _harden_process() -> None:
    """On Linux, make the server's /proc/<pid> unreadable to same-uid processes.

    A compile's TeX engine runs under the same uid as the server. prctl
    PR_SET_DUMPABLE=0 clears the process's dumpable flag, which makes
    /proc/<pid>/environ, /maps and /mem unreadable to a same-uid reader, so an
    engine that slips its argv/env filter still cannot read the server's secrets
    through /proc (this also defeats `\\pdfobj file {/proc/<pid>/environ}`).

    Best effort only: it is a no-op off Linux and never blocks startup. A server
    running as root is not covered, because root bypasses the dumpable check.
    """
    if sys.platform != "linux":
        return
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_DUMPABLE = 4
        libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError, ValueError):
        pass
