"""Allowlisted shell command prefixes for the sandboxed run_command_tool.
Per-language prefixes come from forge/tools/language_hooks.py's registered
profiles (LanguageProfile.safe_command_prefixes) — adding a language never
requires editing this file."""
import shlex
import subprocess
from typing import NamedTuple

from forge.tools import language_hooks


# run_safe_command's returncode for a command the allowlist refused (nothing
# ran). Distinct from -2 (timeout) and -3 (internal error) so a caller can tell
# "not permitted" from "ran and failed".
BLOCKED_RETURN_CODE = -1


class SafeShellResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


_BASE_COMMAND_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("git", "status"),
    ("git", "diff"),
    ("git", "add"),
    ("git", "checkout"),
)


def allowed_command_prefixes() -> list[tuple[str, ...]]:
    prefixes = list(_BASE_COMMAND_PREFIXES)
    for profile in language_hooks.all_profiles():
        prefixes.extend(profile.safe_command_prefixes)
    return prefixes


# Back-compat name some callers/tests may still reference.
ALLOWED_COMMAND_PREFIXES = allowed_command_prefixes()


def is_safe_command(cmd_str: str) -> bool:
    """Check if a command matches the allowed prefixes and does not contain
    dangerous characters."""
    cmd_str = cmd_str.strip()
    if not cmd_str:
        return False

    blocked_chars = [";", "&&", "||", "|", "`", "$", "(", ")", "<", ">", "\n", "\r"]
    for char in blocked_chars:
        if char in cmd_str:
            return False

    try:
        tokens = shlex.split(cmd_str)
    except ValueError:
        return False

    if not tokens:
        return False

    for prefix in ALLOWED_COMMAND_PREFIXES:
        length = len(prefix)
        if len(tokens) >= length and tuple(tokens[:length]) == prefix:
            return True

    return False


def run_safe_command(worktree_path: str, command: str, timeout_seconds: int = 180) -> SafeShellResult:
    """Execute a whitelisted shell command within the specified worktree path."""
    if not is_safe_command(command):
        return SafeShellResult(
            returncode=BLOCKED_RETURN_CODE, stdout="", stderr=f"Command execution blocked by safety policy: '{command}'"
        )

    try:
        proc = subprocess.run(
            shlex.split(command), cwd=worktree_path, capture_output=True, text=True, timeout=timeout_seconds
        )
        return SafeShellResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    except subprocess.TimeoutExpired:
        return SafeShellResult(returncode=-2, stdout="", stderr=f"Command timed out after {timeout_seconds} seconds.")
    except Exception as e:
        return SafeShellResult(returncode=-3, stdout="", stderr=f"Failed to run command due to internal error: {str(e)}")
