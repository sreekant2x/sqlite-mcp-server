"""Layer 2: the stdio transport.

The client (a desktop MCP app, an IDE, the MCP Inspector) launches this server as a child
process and talks to it through pipes:

    client --> server's stdin    one JSON-RPC message per line, UTF-8
    server's stdout --> client   one JSON-RPC message per line, UTF-8
    server's stderr              free-form logs; clients may show or ignore them

Two rules make or break a stdio server:

1. Messages are newline-delimited and must not contain raw newlines. json.dumps never
   emits them unless you ask for indentation, so never pretty-print on stdout.
2. Nothing but protocol messages may go to stdout. A stray print() corrupts the stream
   and the client drops the connection. Log to stderr.

The transport does not understand MCP. It moves lines and hands each one to a callback.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Optional, TextIO

log = logging.getLogger(__name__)

# Takes one raw line, returns one line to send back (or None for notifications).
LineHandler = Callable[[str], Optional[str]]


def serve(handle_line: LineHandler, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Read lines until EOF, which is how a stdio client says the session is over."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        log.debug("<-- %s", line)
        reply = handle_line(line)
        if reply is not None:
            log.debug("--> %s", reply)
            stdout.write(reply + "\n")
            # Flush every message: stdout to a pipe is block-buffered, and a buffered
            # reply looks to the client like a server that hung.
            stdout.flush()
    log.info("stdin closed, shutting down")


def configure_stdio() -> None:
    """Force UTF-8 line I/O regardless of the platform default (Windows uses cp1252)."""
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", newline="\n")
