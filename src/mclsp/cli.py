"""
mclsp – McCode DSL Language Server CLI entry point.

Usage
-----
    mclsp               # stdio mode (default, for use with editors)
    mclsp --stdio       # explicit stdio mode
    mclsp --tcp 2087    # listen on TCP port (useful for debugging)
"""
from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='mclsp',
        description='McCode DSL Language Server (LSP) for .instr and .comp files.',
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        '--stdio',
        action='store_true',
        default=False,
        help='Communicate over stdin/stdout (default when no flag given)',
    )
    mode.add_argument(
        '--tcp',
        metavar='PORT',
        type=int,
        default=None,
        help='Listen for connections on the given TCP port instead of stdio',
    )
    p.add_argument(
        '--version',
        action='store_true',
        default=False,
        help='Print the mclsp version and exit',
    )
    p.add_argument(
        '--log-level',
        metavar='LEVEL',
        default='WARNING',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        help='Logging level written to stderr (default: WARNING)',
    )
    return p


def mclsp() -> None:
    """Entry point for the ``mclsp`` command."""
    import logging
    parser = _build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )

    from mclsp.server import server
    from mclsp import __version__

    if args.version:
        print(f'mclsp {__version__}')
        sys.exit(0)

    if args.tcp is not None:
        server.start_tcp('127.0.0.1', args.tcp)
    else:
        # Default (and --stdio): communicate via stdin/stdout
        server.start_io()


if __name__ == '__main__':
    mclsp()
