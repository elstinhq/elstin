"""Dart/Flutter SDK symbol lookup via LSP (falls back to a plain grep over
the SDK source tree). Genuinely Dart-specific (as Dart's own
dev_conventions block in elstin/tools/dart_tools.py says: use this before
writing a fix for a deprecated API) — a Java equivalent would look up
javadoc/JDK source instead, not built here. Always degrades gracefully
(returns a "could not find" string, never raises), so it's safe to keep
wired in even for non-Dart projects."""
import os
import shutil
import subprocess

from elstin.tools.lsp_client import DART_BIN, DartLspSession, uri_to_abs_path

FLUTTER_BIN = "flutter"


def _read_lines_around(file_path: str, center_line: int, context_lines: int = 15) -> str:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return f"Error reading file {file_path}: {e}"

    start_line = max(0, center_line - context_lines)
    end_line = min(len(lines), center_line + context_lines + 1)

    snippet = []
    for i in range(start_line, end_line):
        prefix = ">>" if i == center_line else "  "
        snippet.append(f"{prefix} {i + 1:4d} | {lines[i].rstrip()}")
    return "\n".join(snippet)


def lookup_sdk_symbol(worktree_path: str, query: str) -> str:
    """Use LSP to find the SDK source definition of a given symbol (e.g.
    Color.withValues) and return the source snippet containing its
    signature and docstring."""
    if not os.path.isdir(worktree_path):
        return f"Error: Worktree path {worktree_path} does not exist."

    evidence = []
    error_msg = None
    try:
        with DartLspSession(worktree_path) as session:
            symbols = session.workspace_symbols(query)

            for symbol in symbols:
                location = symbol.get("location", {})
                uri = location.get("uri")
                if not uri:
                    continue

                abs_path = uri_to_abs_path(uri)
                if not abs_path:
                    continue

                project_root = os.path.realpath(worktree_path)
                abs_path_real = os.path.realpath(abs_path)

                is_sdk = False
                if not abs_path_real.startswith(project_root + os.sep):
                    if "sky_engine" in abs_path_real or os.path.join("packages", "flutter", "lib") in abs_path_real:
                        is_sdk = True
                    dart_bin_resolved = shutil.which(DART_BIN)
                    if dart_bin_resolved:
                        dart_sdk_root = os.path.dirname(os.path.dirname(dart_bin_resolved))
                        if dart_sdk_root and abs_path_real.startswith(dart_sdk_root):
                            is_sdk = True

                if is_sdk:
                    range_info = location.get("range", {})
                    start = range_info.get("start", {})
                    line = start.get("line", 0)

                    snippet = _read_lines_around(abs_path_real, line, context_lines=15)
                    evidence.append(
                        f"Found symbol: {symbol.get('name')}\n"
                        f"File: {abs_path_real}\n"
                        f"Line: {line + 1}\n\n"
                        f"```dart\n{snippet}\n```\n"
                    )
                    if evidence:
                        return "\n---\n".join(evidence)
    except Exception as e:
        error_msg = str(e)

    if not evidence:
        try:
            flutter_bin = shutil.which(FLUTTER_BIN)
            if flutter_bin:
                flutter_bin_real = os.path.realpath(flutter_bin)
                flutter_root = os.path.dirname(os.path.dirname(flutter_bin_real))
                search_paths = [
                    os.path.join(flutter_root, "packages", "flutter", "lib"),
                    os.path.join(flutter_root, "bin", "cache", "pkg", "sky_engine", "lib"),
                ]
                for sp in search_paths:
                    if not os.path.isdir(sp):
                        continue
                    proc = subprocess.run(["grep", "-rn", f"{query}", sp], capture_output=True, text=True, timeout=5)
                    if proc.returncode == 0:
                        for line in proc.stdout.splitlines()[:3]:
                            parts = line.split(":", 2)
                            if len(parts) >= 3:
                                file_path = parts[0]
                                line_num = int(parts[1]) - 1
                                snippet = _read_lines_around(file_path, line_num, context_lines=15)
                                evidence.append(
                                    f"Found symbol (via grep): {query}\n"
                                    f"File: {file_path}\n"
                                    f"Line: {line_num + 1}\n\n"
                                    f"```dart\n{snippet}\n```\n"
                                )
                        if evidence:
                            return "\n---\n".join(evidence)
        except Exception:
            pass

        return f"Could not find SDK symbol matching '{query}' (LSP error: {error_msg}). Try a more specific or broader query, or check if the API exists."

    return "\n---\n".join(evidence)
