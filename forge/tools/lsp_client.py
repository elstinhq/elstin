"""Minimal LSP client for the Dart analysis server, used by
forge/tools/docs_tool.py's SDK-symbol lookup. The Dart binary path and
timeouts are plain module constants (PATH-resolved tool binary + timeouts
aren't credentials, matching forge/tools/language_hooks.py's existing
convention).

`expand_via_lsp` is kept for a future RAG context-discovery feature even
though nothing calls it yet in Phase 2 — cheap to keep, no extra dependency
beyond what's already here.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

DART_BIN = "dart"
LSP_TIMEOUT_SECONDS = 30
USE_LSP = True


class LspError(RuntimeError):
    pass


def uri_to_abs_path(uri: str) -> str | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return unquote(parsed.path)


def abs_path_to_uri(abs_path: str) -> str:
    return Path(abs_path).resolve().as_uri()


class DartLspSession:
    """Minimal LSP client for the Dart analysis server."""

    def __init__(self, project_path: str) -> None:
        self.project_path = project_path
        self._proc: subprocess.Popen[str] | None = None
        self._request_id = 0
        self._open_versions: dict[str, int] = {}
        self._timeout = LSP_TIMEOUT_SECONDS

    def __enter__(self) -> "DartLspSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [DART_BIN, "language-server", "--protocol=lsp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.project_path,
        )
        root_uri = abs_path_to_uri(self.project_path)
        self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": root_uri,
                "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(self.project_path)}],
                "capabilities": {
                    "textDocument": {"definition": {"linkSupport": False}, "references": {}},
                    "workspace": {"symbol": {}},
                },
                "clientInfo": {"name": "code-forge", "version": "1.0"},
            },
        )
        self._notify("initialized", {})

    def close(self) -> None:
        if not self._proc:
            return
        try:
            self._request("shutdown", {})
            self._notify("exit", {})
        except Exception:
            pass
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def open_relative(self, relative_path: str) -> None:
        abs_path = os.path.join(self.project_path, relative_path)
        if not os.path.isfile(abs_path):
            return
        uri = abs_path_to_uri(abs_path)
        if uri in self._open_versions:
            return
        with open(abs_path, encoding="utf-8") as handle:
            text = handle.read()
        version = 1
        self._open_versions[uri] = version
        self._notify(
            "textDocument/didOpen",
            {"textDocument": {"uri": uri, "languageId": "dart", "version": version, "text": text}},
        )

    def workspace_symbols(self, query: str) -> list[dict[str, Any]]:
        result = self._request("workspace/symbol", {"query": query})
        return result if isinstance(result, list) else []

    def goto_definition(self, relative_path: str, line: int, character: int) -> list[dict[str, Any]]:
        uri = abs_path_to_uri(os.path.join(self.project_path, relative_path))
        self.open_relative(relative_path)
        result = self._request(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": character}},
        )
        return _locations_from_result(result)

    def find_references(self, relative_path: str, line: int, character: int) -> list[dict[str, Any]]:
        uri = abs_path_to_uri(os.path.join(self.project_path, relative_path))
        self.open_relative(relative_path)
        result = self._request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
                "context": {"includeDeclaration": True},
            },
        )
        return _locations_from_result(result)

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not self._proc or not self._proc.stdin or not self._proc.stdout:
            raise LspError("LSP process is not running")

        self._request_id += 1
        req_id = self._request_id
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        self._send(payload)

        while True:
            message = self._read_message()
            if message.get("id") == req_id:
                if "error" in message:
                    raise LspError(str(message["error"]))
                return message.get("result")
            if message.get("method") == "window/logMessage":
                continue

    def _send(self, payload: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            raise LspError("LSP process is not running")
        body = json.dumps(payload, separators=(",", ":"))
        frame = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n{body}"
        self._proc.stdin.write(frame)
        self._proc.stdin.flush()

    def _read_message(self) -> dict[str, Any]:
        if not self._proc or not self._proc.stdout:
            raise LspError("LSP process is not running")

        headers: dict[str, str] = {}
        while True:
            line = self._read_line(self._proc.stdout)
            if line in ("", "\r\n", "\n"):
                break
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()

        length = int(headers.get("content-length", "0"))
        if length <= 0:
            raise LspError("Invalid LSP frame")

        body = self._proc.stdout.read(length)
        if not body:
            raise LspError("Unexpected end of LSP stream")
        return json.loads(body)

    def _read_line(self, stream) -> str:
        ready, _, _ = select.select([stream], [], [], self._timeout)
        if not ready:
            raise LspError(f"LSP read timed out after {self._timeout}s")
        line = stream.readline()
        if not line:
            raise LspError("Unexpected end of LSP stream")
        return line


def _path_in_project(project_path: str, uri: str | None) -> str | None:
    if not uri:
        return None
    abs_path = os.path.realpath(uri_to_abs_path(uri) or "")
    project_root = os.path.realpath(project_path)
    if not abs_path.startswith(project_root + os.sep):
        return None
    return abs_path


def _locations_from_result(result: Any) -> list[dict[str, Any]]:
    if not result:
        return []
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict) and "uri" in result:
        return [result]
    if isinstance(result, dict) and "targetUri" in result:
        return [{"uri": result["targetUri"], "range": result.get("targetRange") or result.get("range") or {}}]
    return []


def expand_via_lsp(
    *,
    project_path: str,
    grep_matches: list[Any],
    search_terms: list[str],
    seed_files: set[str],
    max_symbol_queries: int = 8,
    max_grep_lookups: int = 12,
    max_reference_files: int = 8,
) -> tuple[set[str], list[dict[str, Any]]]:
    """Use Dart LSP to resolve symbols and references from grep/tree-sitter seeds."""
    if not USE_LSP:
        return set(), []
    if not os.path.isfile(os.path.join(project_path, "pubspec.yaml")):
        return set(), []

    from forge.tools.language_hooks import ensure_dependencies

    dep_result = ensure_dependencies(project_path)
    if not dep_result.get("passed", True):
        return set(), []

    discovered: set[str] = set()
    evidence: list[dict[str, Any]] = []

    try:
        with DartLspSession(project_path) as session:
            for rel in sorted(seed_files):
                if rel.endswith(".dart"):
                    session.open_relative(rel)

            for term in search_terms[:max_symbol_queries]:
                for symbol in session.workspace_symbols(term)[:5]:
                    uri = symbol.get("location", {}).get("uri")
                    abs_path = _path_in_project(project_path, uri)
                    if not abs_path:
                        continue
                    rel = os.path.relpath(abs_path, project_path)
                    discovered.add(abs_path)
                    evidence.append({"kind": "workspace_symbol", "query": term, "name": symbol.get("name"), "file_path": rel})

            for match in grep_matches[:max_grep_lookups]:
                rel = match.file_path
                line_idx = max(match.line_number - 1, 0)
                column = _match_column(match.line_text, search_terms)
                for location in session.goto_definition(rel, line_idx, column):
                    abs_path = _path_in_project(project_path, location.get("uri"))
                    if not abs_path:
                        continue
                    discovered.add(abs_path)
                    evidence.append({"kind": "definition", "from_file": rel, "from_line": match.line_number, "file_path": os.path.relpath(abs_path, project_path)})

                refs = session.find_references(rel, line_idx, column)
                for location in refs[:max_reference_files]:
                    abs_path = _path_in_project(project_path, location.get("uri"))
                    if not abs_path:
                        continue
                    discovered.add(abs_path)
                    evidence.append({"kind": "reference", "from_file": rel, "from_line": match.line_number, "file_path": os.path.relpath(abs_path, project_path)})
    except Exception:
        return set(), evidence

    return discovered, evidence


def _match_column(line_text: str, terms: list[str]) -> int:
    lowered = line_text.lower()
    for term in terms:
        idx = lowered.find(term.lower())
        if idx >= 0:
            return idx
    stripped = line_text.lstrip()
    return len(line_text) - len(stripped)
