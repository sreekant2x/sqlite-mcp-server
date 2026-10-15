"""Layer 3: the MCP protocol.

On top of JSON-RPC, MCP defines:

* a lifecycle: the client sends ``initialize``, the server answers with its protocol
  version, capabilities and identity, then the client sends ``notifications/initialized``.
  Only then is the session "operational".
* three server primitives, each with a list/get pair of methods:

    tools      model-controlled actions      tools/list, tools/call
    resources  application-controlled data   resources/list, resources/templates/list, resources/read
    prompts    user-controlled templates     prompts/list, prompts/get

This module is the generic part: a registry of tools, resources and prompts and a
dispatcher from method names to handlers. It knows nothing about SQLite; see app.py
for the actual tools.
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from . import jsonrpc
from .jsonrpc import INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, JsonRpcError

log = logging.getLogger(__name__)

# Newest first. The server answers with the client's version when it supports it,
# otherwise with its own latest, and the client decides whether to continue.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# MCP-specific error code for resources/read on an unknown URI.
RESOURCE_NOT_FOUND = -32002


class ToolError(Exception):
    """A failure the model should see and can recover from, such as bad SQL.

    These become a normal tools/call result with ``isError: true``, not a JSON-RPC error,
    because the spec wants the model to read the message and try again.
    """


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]
    read_only: bool = True

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"readOnlyHint": self.read_only, "openWorldHint": False},
        }


@dataclass
class Resource:
    uri: str
    name: str
    description: str
    mime_type: str
    reader: Callable[[], str]

    def describe(self) -> dict[str, Any]:
        return {"uri": self.uri, "name": self.name, "description": self.description, "mimeType": self.mime_type}


@dataclass
class ResourceTemplate:
    """A family of resources such as ``sqlite://table/{name}``."""

    uri_template: str
    name: str
    description: str
    mime_type: str
    reader: Callable[..., str]
    _pattern: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Supports the simple {var} form of RFC 6570, which is all this server needs.
        regex = re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(self.uri_template))
        self._pattern = re.compile(f"^{regex}$")

    def match(self, uri: str) -> dict[str, str] | None:
        m = self._pattern.match(uri)
        return m.groupdict() if m else None

    def describe(self) -> dict[str, Any]:
        return {
            "uriTemplate": self.uri_template,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
        }


@dataclass
class PromptArgument:
    name: str
    description: str
    required: bool = False


@dataclass
class Prompt:
    name: str
    description: str
    arguments: list[PromptArgument]
    render: Callable[..., str]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "arguments": [
                {"name": a.name, "description": a.description, "required": a.required} for a in self.arguments
            ],
        }


class Server:
    def __init__(self, name: str, version: str, instructions: str | None = None):
        self.name = name
        self.version = version
        self.instructions = instructions
        self.tools: dict[str, Tool] = {}
        self.resources: dict[str, Resource] = {}
        self.resource_templates: list[ResourceTemplate] = []
        self.prompts: dict[str, Prompt] = {}

        self.protocol_version: str | None = None
        self.client_info: dict[str, Any] | None = None
        self.initialized = False  # set by notifications/initialized

        self._methods: dict[str, Callable[[dict[str, Any]], Any]] = {
            "initialize": self._initialize,
            "ping": lambda params: {},
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "resources/list": self._resources_list,
            "resources/templates/list": self._resource_templates_list,
            "resources/read": self._resources_read,
            "prompts/list": self._prompts_list,
            "prompts/get": self._prompts_get,
        }

    # -- registration -----------------------------------------------------------------

    def add_tool(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def add_resource(self, resource: Resource) -> None:
        self.resources[resource.uri] = resource

    def add_resource_template(self, template: ResourceTemplate) -> None:
        self.resource_templates.append(template)

    def add_prompt(self, prompt: Prompt) -> None:
        self.prompts[prompt.name] = prompt

    # -- entry points -----------------------------------------------------------------

    def handle_line(self, raw: str) -> str | None:
        """Transport callback: one line in, at most one line out."""
        reply = self.handle_raw(raw)
        return jsonrpc.encode(reply) if reply is not None else None

    def handle_raw(self, raw: str) -> dict[str, Any] | None:
        try:
            message = jsonrpc.parse(raw)
        except JsonRpcError as err:
            return jsonrpc.failure(getattr(err, "request_id", None), err)

        if isinstance(message, jsonrpc.Notification):
            self._on_notification(message)
            return None
        if isinstance(message, jsonrpc.Response):
            log.warning("Ignoring unexpected response with id %r", message.id)
            return None
        return self._on_request(message)

    # -- dispatch ---------------------------------------------------------------------

    def _on_notification(self, note: jsonrpc.Notification) -> None:
        if note.method == "notifications/initialized":
            self.initialized = True
            log.info("Session initialised (protocol %s)", self.protocol_version)
        elif note.method == "notifications/cancelled":
            # Requests are handled one at a time, so by the time this arrives the
            # request it cancels has already been answered. Nothing to do.
            log.info("Client cancelled request %s", note.params.get("requestId"))
        else:
            # Notifications never get a reply, not even an error.
            log.debug("Ignoring notification %s", note.method)

    def _on_request(self, req: jsonrpc.Request) -> dict[str, Any]:
        handler = self._methods.get(req.method)
        if handler is None:
            return jsonrpc.failure(req.id, JsonRpcError(METHOD_NOT_FOUND, f"Method not found: {req.method}"))
        if self.protocol_version is None and req.method not in ("initialize", "ping"):
            return jsonrpc.failure(
                req.id, JsonRpcError(INVALID_REQUEST, "Server not initialized: send 'initialize' first")
            )
        try:
            return jsonrpc.success(req.id, handler(req.params))
        except JsonRpcError as err:
            return jsonrpc.failure(req.id, err)
        except Exception:  # never let one bad request kill the session
            log.error("Unhandled error in %s:\n%s", req.method, traceback.format_exc())
            return jsonrpc.failure(req.id, JsonRpcError(jsonrpc.INTERNAL_ERROR, "Internal error"))

    # -- lifecycle --------------------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.protocol_version is not None:
            raise JsonRpcError(INVALID_REQUEST, "Session is already initialized")
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise JsonRpcError(INVALID_PARAMS, "initialize requires a string 'protocolVersion'")

        self.protocol_version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        self.client_info = params.get("clientInfo")
        log.info("initialize from %s, client wants %s, using %s", self.client_info, requested, self.protocol_version)

        # Capabilities tell the client which method families exist. listChanged would
        # promise notifications when the lists change; ours are fixed at startup.
        result: dict[str, Any] = {
            "protocolVersion": self.protocol_version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    # -- tools ------------------------------------------------------------------------

    def _tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        # Lists support cursor pagination; ours are short enough to return in one page.
        return {"tools": [t.describe() for t in self.tools.values()]}

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise JsonRpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS, "'arguments' must be an object")

        problem = validate_arguments(arguments, tool.input_schema)
        if problem:
            return _tool_error(f"Invalid arguments for {tool.name}: {problem}")
        try:
            output = tool.handler(**arguments)
        except ToolError as err:
            return _tool_error(str(err))

        # Text content is what every client can show the model. structuredContent is
        # the same data as JSON for clients that want to parse it.
        result: dict[str, Any] = {
            "content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False, default=str)}],
            "isError": False,
        }
        if isinstance(output, dict):
            result["structuredContent"] = output
        return result

    # -- resources --------------------------------------------------------------------

    def _resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"resources": [r.describe() for r in self.resources.values()]}

    def _resource_templates_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"resourceTemplates": [t.describe() for t in self.resource_templates]}

    def _resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise JsonRpcError(INVALID_PARAMS, "resources/read requires a string 'uri'")

        if uri in self.resources:
            res = self.resources[uri]
            return {"contents": [{"uri": uri, "mimeType": res.mime_type, "text": res.reader()}]}
        for template in self.resource_templates:
            variables = template.match(uri)
            if variables is not None:
                try:
                    text = template.reader(**variables)
                except LookupError:
                    break
                return {"contents": [{"uri": uri, "mimeType": template.mime_type, "text": text}]}
        raise JsonRpcError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})

    # -- prompts ----------------------------------------------------------------------

    def _prompts_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"prompts": [p.describe() for p in self.prompts.values()]}

    def _prompts_get(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        prompt = self.prompts.get(name) if isinstance(name, str) else None
        if prompt is None:
            raise JsonRpcError(INVALID_PARAMS, f"Unknown prompt: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict) or not all(isinstance(v, str) for v in arguments.values()):
            raise JsonRpcError(INVALID_PARAMS, "Prompt arguments must be an object of strings")

        known = {a.name for a in prompt.arguments}
        missing = [a.name for a in prompt.arguments if a.required and a.name not in arguments]
        unknown = sorted(set(arguments) - known)
        if missing or unknown:
            raise JsonRpcError(INVALID_PARAMS, f"Bad prompt arguments: missing={missing} unknown={unknown}")

        text = prompt.render(**arguments)
        return {
            "description": prompt.description,
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


_JSON_TYPES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def validate_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> str | None:
    """Check arguments against the small subset of JSON Schema this server's tools use.

    Returns a human-readable problem, or None when the arguments are valid. A real
    project would use the jsonschema package; this stays dependency-free on purpose.
    """
    properties: dict[str, Any] = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in arguments:
            return f"missing required argument '{key}'"
    if schema.get("additionalProperties") is False:
        extra = sorted(set(arguments) - set(properties))
        if extra:
            return f"unexpected argument(s) {extra}"
    for key, value in arguments.items():
        spec = properties.get(key)
        if spec is None:
            continue
        expected = spec.get("type")
        if expected and not _JSON_TYPES[expected](value):
            return f"'{key}' must be of type {expected}"
        if "minimum" in spec and value < spec["minimum"]:
            return f"'{key}' must be >= {spec['minimum']}"
        if "maximum" in spec and value > spec["maximum"]:
            return f"'{key}' must be <= {spec['maximum']}"
        if "minLength" in spec and len(value) < spec["minLength"]:
            return f"'{key}' must not be empty"
    return None
