"""Private process bridge to a user-managed MCP Python SDK environment.

This module deliberately has no import-time dependency on MCP.  Noruct invokes
it with an absolute Python executable supplied by the user, and that interpreter
must contain the exact audited SDK release.  MCP protocol objects never cross
the bridge; stdout contains one bounded, first-party JSON envelope.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import signal
import socket
import stat
import sys
import tempfile
import time
import webbrowser
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit, urlparse


BRIDGE_PROTOCOL = "noruct-external-read-v1"
AUDITED_MCP_VERSION = "1.28.1"
MAX_REQUEST_BYTES = 32_768
_OAUTH_STATE_KEY_RE = __import__("re").compile(r"[0-9a-f]{24}\\Z")


class BridgeRequestError(ValueError):
    pass


def _require_request(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeRequestError("request must be an object")
    if value.get("protocol") != BRIDGE_PROTOCOL:
        raise BridgeRequestError("unsupported bridge protocol")
    operation = value.get("operation")
    if operation not in {"list", "call"}:
        raise BridgeRequestError("unsupported bridge operation")
    transport = value.get("transport", "stdio")
    if transport not in {"stdio", "streamable_http"}:
        raise BridgeRequestError("unsupported MCP transport")
    if transport == "stdio":
        command = value.get("server_command")
        if not isinstance(command, str) or not Path(command).is_absolute():
            raise BridgeRequestError("server command must be absolute")
        args = value.get("server_args", [])
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise BridgeRequestError("server args must be strings")
        environment = value.get("server_environment", {})
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in environment.items()
        ):
            raise BridgeRequestError("server environment must contain string values")
    else:
        url = value.get("server_url")
        if not isinstance(url, str) or len(url.encode("utf-8")) > 2_048:
            raise BridgeRequestError("server URL is invalid")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise BridgeRequestError("server URL is invalid")
        headers = value.get("http_headers", {})
        if not isinstance(headers, dict) or len(headers) > 8 or not all(
            isinstance(key, str) and isinstance(item, str) and len(key) <= 64
            for key, item in headers.items()
        ):
            raise BridgeRequestError("HTTP headers are invalid")
        oauth = value.get("oauth", {"enabled": False})
        if not isinstance(oauth, dict) or not isinstance(oauth.get("enabled", False), bool):
            raise BridgeRequestError("OAuth request is invalid")
        if oauth.get("enabled"):
            state_directory = oauth.get("state_directory")
            state_key = oauth.get("state_key")
            if (
                not isinstance(state_directory, str)
                or not Path(state_directory).is_absolute()
                or len(state_directory.encode("utf-8")) > 1_024
                or not isinstance(state_key, str)
                or not _OAUTH_STATE_KEY_RE.fullmatch(state_key)
                or not all(isinstance(oauth.get(key), str) or oauth.get(key) is None for key in ("client_id", "client_secret", "scope"))
            ):
                raise BridgeRequestError("OAuth request is invalid")
        if not isinstance(value.get("interactive_oauth", False), bool):
            raise BridgeRequestError("OAuth interactivity is invalid")
    timeout = value.get("timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise BridgeRequestError("timeout must be positive")
    if operation == "call":
        if not isinstance(value.get("tool_name"), str):
            raise BridgeRequestError("tool name is required")
        if not isinstance(value.get("arguments"), dict):
            raise BridgeRequestError("tool arguments must be an object")
    return value


def _tool_record(tool: object) -> dict[str, Any]:
    annotations = getattr(tool, "annotations", None)
    execution = getattr(tool, "execution", None)
    return {
        "name": getattr(tool, "name", None),
        "input_schema": getattr(tool, "inputSchema", None),
        "read_only": getattr(annotations, "readOnlyHint", None),
        "destructive": getattr(annotations, "destructiveHint", None),
        "open_world": getattr(annotations, "openWorldHint", None),
        "task_support": getattr(execution, "taskSupport", None),
    }


class _OAuthLoginRequired(RuntimeError):
    pass


class _OAuthLoginFailed(RuntimeError):
    pass


def _oauth_file(root: Path, key: str, kind: str) -> Path:
    return root / f"{key}.{kind}.json"


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, stat.S_IRWXU)
    except OSError:
        pass
    descriptor, temporary_name = tempfile.mkstemp(prefix=".noruct-mcp-oauth-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_private_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _oauth_auth(request: Mapping[str, Any]):
    """Return an SDK OAuth auth handler with a Noruct-owned local token store.

    This code deliberately mirrors only the SDK ``TokenStorage`` protocol from
    the audited source.  It does not inherit upstream home-directory, gateway,
    or credential authority.
    """

    oauth = request.get("oauth")
    if not isinstance(oauth, Mapping) or oauth.get("enabled") is not True:
        return None
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

    root = Path(str(oauth["state_directory"])).resolve()
    key = str(oauth["state_key"])
    token_path = _oauth_file(root, key, "tokens")
    client_path = _oauth_file(root, key, "client")

    class TokenStore:
        async def get_tokens(self):
            payload = _read_private_json(token_path)
            if payload is None:
                return None
            expires_at = payload.pop("noruct_expires_at", None)
            if isinstance(expires_at, (int, float)):
                payload["expires_in"] = max(0, int(expires_at - time.time()))
            try:
                return OAuthToken.model_validate(payload)
            except (TypeError, ValueError):
                return None

        async def set_tokens(self, tokens):
            payload = tokens.model_dump(mode="json", exclude_none=True)
            expires_in = payload.get("expires_in")
            if isinstance(expires_in, int) and not isinstance(expires_in, bool):
                payload["noruct_expires_at"] = time.time() + expires_in
            _write_private_json(token_path, payload)

        async def get_client_info(self):
            payload = _read_private_json(client_path)
            if payload is None:
                return None
            try:
                return OAuthClientInformationFull.model_validate(payload)
            except (TypeError, ValueError):
                return None

        async def set_client_info(self, client_info):
            _write_private_json(client_path, client_info.model_dump(mode="json", exclude_none=True))

    port_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        port_socket.bind(("127.0.0.1", 0))
        port = int(port_socket.getsockname()[1])
    finally:
        port_socket.close()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    metadata: dict[str, object] = {
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "Noruct MCP",
    }
    scope = oauth.get("scope")
    if isinstance(scope, str) and scope:
        metadata["scope"] = scope
    client_secret = oauth.get("client_secret")
    if isinstance(client_secret, str) and client_secret:
        metadata["token_endpoint_auth_method"] = "client_secret_post"
    client_metadata = OAuthClientMetadata.model_validate(metadata)
    configured_client_id = oauth.get("client_id")
    if isinstance(configured_client_id, str) and configured_client_id:
        pre_registered: dict[str, object] = {
            "client_id": configured_client_id,
            "redirect_uris": [redirect_uri],
            "grant_types": metadata["grant_types"],
            "response_types": metadata["response_types"],
        }
        if isinstance(client_secret, str) and client_secret:
            pre_registered["client_secret"] = client_secret
            pre_registered["token_endpoint_auth_method"] = "client_secret_post"
        if scope:
            pre_registered["scope"] = scope
        _write_private_json(client_path, pre_registered)

    interactive = bool(request.get("interactive_oauth", False))

    async def redirect_handler(authorization_url: str) -> None:
        if not interactive:
            raise _OAuthLoginRequired("interactive OAuth login required")
        print("Noruct MCP OAuth: opening the authorization page (or copy this URL):", file=sys.stderr, flush=True)
        print(authorization_url, file=sys.stderr, flush=True)
        try:
            webbrowser.open(authorization_url, new=1, autoraise=True)
        except Exception:
            pass

    async def callback_handler() -> tuple[str, str | None]:
        if not interactive:
            raise _OAuthLoginRequired("interactive OAuth login required")
        result: dict[str, str | None] = {"code": None, "state": None, "error": None}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                values = parse_qs(urlparse(self.path).query)
                result["code"] = values.get("code", [None])[0]
                result["state"] = values.get("state", [None])[0]
                result["error"] = values.get("error", [None])[0]
                body = b"<html><body><h2>Noruct authorization complete</h2><p>You can close this tab.</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", port), CallbackHandler)
        # Poll in short bounded slices so sidecar cancellation cannot leave a
        # callback thread or listening localhost port behind for five minutes.
        server.timeout = 1.0
        deadline = time.monotonic() + 300.0
        try:
            while result["code"] is None and result["error"] is None and time.monotonic() < deadline:
                await asyncio.to_thread(server.handle_request)
        finally:
            server.server_close()
        if result["error"] or not result["code"]:
            raise _OAuthLoginFailed("authorization callback did not contain a code")
        return str(result["code"]), result["state"]

    return OAuthClientProvider(
        server_url=str(request["server_url"]),
        client_metadata=client_metadata,
        storage=TokenStore(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=300.0,
    )


async def _exchange(request: Mapping[str, Any]) -> dict[str, Any]:
    installed = importlib.metadata.version("mcp")
    if installed != AUDITED_MCP_VERSION:
        raise RuntimeError("MCP_SDK_VERSION_MISMATCH")

    from mcp import ClientSession

    timeout = float(request["timeout_seconds"])
    flow_timeout = 300.0 if request.get("interactive_oauth") is True else timeout
    async with asyncio.timeout(flow_timeout):
        client = None
        try:
            if request.get("transport", "stdio") == "stdio":
                from mcp import StdioServerParameters
                from mcp.client.stdio import stdio_client

                parameters = StdioServerParameters(
                    command=str(request["server_command"]),
                    args=list(request.get("server_args", [])),
                    env=dict(request.get("server_environment", {})),
                    cwd=tempfile.gettempdir(),
                )
                transport = stdio_client(parameters)
            else:
                # This is the modern SDK transport used by the registered source.
                # The user-managed SDK owns HTTP/TLS; protocol objects remain
                # entirely in this short-lived private child process.
                import httpx
                from mcp.client.streamable_http import streamable_http_client

                # The v1.28.1 transport accepts a preconfigured httpx client;
                # headers/auth are not transport keyword arguments.
                client = httpx.AsyncClient(
                    headers=dict(request.get("http_headers", {})),
                    auth=_oauth_auth(request),
                )
                transport = streamable_http_client(str(request["server_url"]), http_client=client)
            async with transport as streams:
                # SDK 1.28.1 streamable HTTP yields a third session-id callback;
                # stdio yields only the read/write pair.  Product logic never
                # needs that callback and intentionally discards it.
                read_stream, write_stream = streams[0], streams[1]
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    tools = [_tool_record(tool) for tool in listed.tools]
                    cursor = getattr(listed, "nextCursor", None)
                    response: dict[str, Any] = {
                        "protocol": BRIDGE_PROTOCOL,
                        "ok": True,
                        "tools": tools,
                        "has_more_tools": cursor is not None,
                    }
                    if request["operation"] == "call":
                        result = await session.call_tool(
                            str(request["tool_name"]),
                            dict(request["arguments"]),
                            read_timeout_seconds=timedelta(seconds=timeout),
                        )
                        response["result"] = result.model_dump(mode="json", by_alias=True)
                    return response
        finally:
            if client is not None:
                await client.aclose()


async def _run(request: Mapping[str, Any]) -> dict[str, Any]:
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is not None and sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, task.cancel)
            except (NotImplementedError, RuntimeError):
                pass
    return await _exchange(request)


def _error(code: str) -> dict[str, Any]:
    return {"protocol": BRIDGE_PROTOCOL, "ok": False, "error_code": code}


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        response = _error("REQUEST_TOO_LARGE")
    else:
        try:
            request = _require_request(json.loads(raw.decode("utf-8")))
            response = asyncio.run(_run(request))
        except importlib.metadata.PackageNotFoundError:
            response = _error("MCP_SDK_NOT_INSTALLED")
        except BridgeRequestError:
            response = _error("INVALID_REQUEST")
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = _error("MALFORMED_REQUEST")
        except TimeoutError:
            response = _error("SERVER_TIMEOUT")
        except _OAuthLoginRequired:
            response = _error("MCP_OAUTH_LOGIN_REQUIRED")
        except _OAuthLoginFailed:
            response = _error("MCP_OAUTH_LOGIN_FAILED")
        except RuntimeError as exc:
            response = _error(
                "MCP_SDK_VERSION_MISMATCH"
                if str(exc) == "MCP_SDK_VERSION_MISMATCH"
                else "SDK_PROTOCOL_FAILURE"
            )
        except Exception:
            response = _error("SDK_PROTOCOL_FAILURE")
    encoded = json.dumps(
        response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()
    return 0 if response.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
