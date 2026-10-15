"""Layer 1: JSON-RPC 2.0.

MCP is JSON-RPC 2.0 underneath. Every message on the wire is one of three shapes:

    request       {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {...}}
    notification  {"jsonrpc": "2.0", "method": "notifications/initialized"}       (no id, no reply)
    response      {"jsonrpc": "2.0", "id": 1, "result": {...}}
                  {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "..."}}

This module knows nothing about MCP. It only parses raw text into one of those shapes
and builds replies. Spec: https://www.jsonrpc.org/specification
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Union

# Standard JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

RequestId = Union[str, int]


class JsonRpcError(Exception):
    """Raised by a handler to send a JSON-RPC error response instead of a result."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


@dataclass
class Request:
    id: RequestId
    method: str
    params: dict[str, Any]


@dataclass
class Notification:
    method: str
    params: dict[str, Any]


@dataclass
class Response:
    """A reply from the other side. This server never sends requests, so it only logs these."""

    id: RequestId | None
    result: Any = None
    error: dict[str, Any] | None = None


Message = Union[Request, Notification, Response]


def parse(raw: str) -> Message:
    """Turn one line of text into a Request, Notification or Response.

    Raises JsonRpcError with PARSE_ERROR or INVALID_REQUEST. When the message had a
    usable id, it is attached as ``error.request_id`` so the caller can still reply to it.
    """
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JsonRpcError(PARSE_ERROR, f"Parse error: {exc.msg}") from exc

    if isinstance(obj, list):
        # JSON-RPC allows batches, but MCP removed batching in protocol version 2025-06-18.
        raise JsonRpcError(INVALID_REQUEST, "Batch requests are not supported")
    if not isinstance(obj, dict):
        raise JsonRpcError(INVALID_REQUEST, "Message must be a JSON object")

    msg_id = obj.get("id")
    id_ok = isinstance(msg_id, (str, int)) and not isinstance(msg_id, bool)

    def invalid(reason: str) -> JsonRpcError:
        err = JsonRpcError(INVALID_REQUEST, reason)
        err.request_id = msg_id if id_ok else None  # type: ignore[attr-defined]
        return err

    if obj.get("jsonrpc") != "2.0":
        raise invalid('"jsonrpc" must be exactly "2.0"')

    if "method" not in obj:
        if "result" in obj or "error" in obj:
            return Response(id=msg_id if id_ok else None, result=obj.get("result"), error=obj.get("error"))
        raise invalid('Message has no "method", "result" or "error"')

    method = obj["method"]
    if not isinstance(method, str):
        raise invalid('"method" must be a string')

    params = obj.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        # JSON-RPC allows positional (array) params; MCP always uses named params.
        raise invalid('"params" must be an object')

    if "id" not in obj:
        return Notification(method=method, params=params)
    if not id_ok:
        # MCP is stricter than JSON-RPC here: the id must be a string or integer, never null.
        raise invalid('"id" must be a string or an integer')
    return Request(id=msg_id, method=method, params=params)


def success(request_id: RequestId, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def failure(request_id: RequestId | None, error: JsonRpcError) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": error.to_dict()}


def notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params:
        msg["params"] = params
    return msg


def encode(message: dict[str, Any]) -> str:
    """Serialise to a single line. The stdio transport depends on there being no raw newlines."""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)
