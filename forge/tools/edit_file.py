import os
from typing import Callable

from forge.tools.path_guard import resolve_write_path


class EditFileError(ValueError):
    pass


def _apply_spans(content: str, spans: list[tuple[int, int]], replacement: str) -> str:
    """Replace each (start, end) char span with *replacement*, working back-to-front
    so earlier spans keep their offsets. Each span is expected to be line-aligned
    (end points at the start of the following line, or EOF), so the block's trailing
    newline is inside the span — we re-attach it to the replacement to preserve the
    file's line structure."""
    for start, end in sorted(spans, reverse=True):
        block = content[start:end]
        repl = replacement
        if block.endswith("\n") and not repl.endswith("\n"):
            repl = repl + "\n"
        elif not block.endswith("\n") and repl.endswith("\n"):
            repl = repl.rstrip("\n")
        content = content[:start] + repl + content[end:]
    return content


def _tolerant_spans(
    content: str, target_content: str, normalize: Callable[[str], str]
) -> list[tuple[int, int]]:
    """Find line-aligned char spans in *content* whose lines equal *target_content*'s
    lines after applying *normalize* (e.g. rstrip or full strip). This recovers from
    the common LLM failure where the target block drifts by trailing spaces, blank
    lines, or indentation, which defeats an exact substring match."""
    target_lines = target_content.splitlines()
    if not target_lines:
        return []
    raw_lines = content.splitlines(keepends=True)
    # offs[i] = char offset where raw line i starts; offs[len] = len(content)
    offs = [0]
    for line in raw_lines:
        offs.append(offs[-1] + len(line))

    norm_target = [normalize(line) for line in target_lines]
    m = len(norm_target)
    spans: list[tuple[int, int]] = []
    for i in range(len(raw_lines) - m + 1):
        if all(normalize(raw_lines[i + j]) == norm_target[j] for j in range(m)):
            spans.append((offs[i], offs[i + m]))
    return spans


def edit_file(
    worktree_path: str,
    relative_path: str,
    target_content: str,
    replacement_content: str,
    allow_multiple: bool = False,
) -> None:
    """
    Surgically replace a contiguous block of text (target_content) with replacement_content.

    Matching is tiered so a near-miss on whitespace does not force the agent to re-read
    and retry:
      1. Exact substring match (fast path; also the only tier that can match inside a line).
      2. Line-aligned match tolerant of trailing whitespace / blank-line drift.
      3. Line-aligned match tolerant of all leading/trailing whitespace (indentation drift).

    A tier is only used when the previous one found nothing. Within the chosen tier the
    match must be unique unless allow_multiple is True, so an ambiguous edit still fails
    loudly rather than changing the wrong block.
    """
    abs_path = resolve_write_path(worktree_path, relative_path)
    if not os.path.isfile(abs_path):
        raise EditFileError(f"Target file does not exist: {relative_path}")

    with open(abs_path, "r", encoding="utf-8") as handle:
        content = handle.read()

    # Tier 1 — exact substring.
    count = content.count(target_content)
    if count >= 1:
        if count > 1 and not allow_multiple:
            raise EditFileError(
                f"Target content found {count} times in file: {relative_path}. "
                "Please provide a more unique context matching block, or pass "
                "allow_multiple=true if you intend to replace all occurrences."
            )
        new_content = (
            content.replace(target_content, replacement_content)
            if allow_multiple
            else content.replace(target_content, replacement_content, 1)
        )
        with open(abs_path, "w", encoding="utf-8") as handle:
            handle.write(new_content)
        return

    # Tiers 2 & 3 — whitespace-tolerant, line-aligned.
    for normalize in (str.rstrip, str.strip):
        spans = _tolerant_spans(content, target_content, normalize)
        if not spans:
            continue
        if len(spans) > 1 and not allow_multiple:
            raise EditFileError(
                f"Target content matched {len(spans)} times (whitespace-tolerant) in "
                f"file: {relative_path}. Please provide a more unique context matching "
                "block, or pass allow_multiple=true to replace all occurrences."
            )
        chosen = spans if allow_multiple else spans[:1]
        new_content = _apply_spans(content, chosen, replacement_content)
        with open(abs_path, "w", encoding="utf-8") as handle:
            handle.write(new_content)
        return

    raise EditFileError(f"Target content not found in file: {relative_path}")
