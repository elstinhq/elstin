"""Allowlisted shell command prefixes for the sandboxed run_command_tool.
Per-language prefixes come from forge/tools/language_hooks.py's registered
profiles (LanguageProfile.safe_command_prefixes) — adding a language never
requires editing this file."""
import shlex
import subprocess
from typing import NamedTuple

from forge.tools import language_hooks


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


# Git long options that reach outside the worktree even though the command
# prefix is allowed: --output writes the diff to an arbitrary path (past the
# plan-scope path guard), --no-index diffs arbitrary files on disk, and
# --pathspec-from-file reads one, echoing its first line in the error. Git
# accepts unambiguous abbreviations of long options, so a token is refused
# when it is a prefix of one of these.
#
# --ext-diff / --textconv are deliberately not here: the program they run
# comes from repo config (diff.external, diff.<driver>.textconv), never from
# argv, and textconv is already on by default for `git diff`. The agent can
# reach neither `git config` nor `git -c` through the allowlist.
_GIT_BLOCKED_LONG_OPTIONS = ("--output", "--no-index", "--pathspec-from-file")


def _escapes_worktree(arg: str) -> bool:
    """`git diff <a> <b>` silently becomes a --no-index diff when the paths
    lie outside the worktree, so any such path is as good as --no-index."""
    return arg.startswith(("/", "~")) or ".." in arg.split("/")


def _git_args_are_safe(tokens: list[str]) -> bool:
    subcommand = tokens[1] if len(tokens) > 1 else ""
    options_done = False
    for arg in tokens[2:]:
        if not options_done:
            if arg == "--":
                options_done = True
                continue
            if arg.startswith("--"):
                name = arg.split("=", 1)[0]
                if any(blocked.startswith(name) for blocked in _GIT_BLOCKED_LONG_OPTIONS):
                    return False
                continue
            if arg.startswith("-"):
                # -O<orderfile> reads an arbitrary path and may sit in a
                # cluster (-pOfile). -S/-G swallow the rest of the token as
                # their value, so a capital O there is just pickaxe text.
                if subcommand == "diff" and not arg.startswith(("-S", "-G")) and "O" in arg:
                    return False
                continue
        if subcommand == "diff" and _escapes_worktree(arg):
            return False
    return True


def is_safe_command(cmd_str: str) -> bool:
    """Check if a command matches the allowed prefixes, does not contain
    dangerous characters, and (for git) carries no flag that reads or writes
    outside the worktree."""
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
            return tokens[0] != "git" or _git_args_are_safe(tokens)

    return False


def run_safe_command(worktree_path: str, command: str, timeout_seconds: int = 180) -> SafeShellResult:
    """Execute a whitelisted shell command within the specified worktree path."""
    if not is_safe_command(command):
        return SafeShellResult(
            returncode=-1, stdout="", stderr=f"Command execution blocked by safety policy: '{command}'"
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
