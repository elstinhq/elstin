"""outline_file_tool: a file's declarations — types, functions, methods,
constructors — with their line numbers, without reading the file.

Why it exists. One dev run on a Java service spent 237 of its 341 tool
calls reading and searching (160 read_file_tool, 77 search_codebase_tool)
against 44 writes. The pattern behind most of them: search for
`class OrderSearchServiceImpl`, then read that file from line 1 in
250-line pages to find the one method the plan touches — and every page
then rides along in the history for the rest of the run. An outline
answers "where is `search(`" in one short result, so the next read can be
the method's own range.

Language-neutral on purpose, like elstin/tools/codebase_skeleton.py (whose
keyword list this shares): nothing here branches on a language name. A
line is listed when, at shallow nesting, it is either

- a declaration keyword followed by a name (`class`, `interface`, `enum`,
  `record`, `def`, `function`, `fun`, `func`, `fn`, `struct`, …), or
- a signature: a name followed by `(`, preceded by at least a type or
  modifier (`public List<Order> search(`, `Widget build(`, `const Foo({`),
  or ending in `{` (`render() {`), and not a control statement, a call
  (`foo.bar(`, `return x(`, `child: Text(`) or an assignment — except an
  arrow function (`const load = async () =>`).

Shallow means at most two indentation units deep, with the unit taken from
the file itself (2 spaces for Dart, 4 for Java): methods of a top-level
class and of a class nested in it are listed, statement-level code inside
a method body is not. It is a heuristic and says so in its header; the
file itself stays the authority, which is why every entry carries the line
to read from.
"""
from __future__ import annotations

import os
import re

from elstin.tools.path_guard import resolve_read_path

# Entries per call; a larger file's outline says how many it left out.
MAX_OUTLINE_ENTRIES = 250

# Shared by the dev and planner tool schemas.
OUTLINE_FILE_DESCRIPTION = (
    "List a file's classes, functions, methods and constructors with their line numbers, without reading the "
    "file. Use it before read_file_tool on a file you have not read yet: find the declaration you need, then read "
    "just its range with offset/limit — far cheaper than paging through the file or searching for 'class X'."
)
# A listed line longer than this is cut: a signature's name and first
# parameters are what identify it.
_MAX_ENTRY_CHARS = 160
# Beyond this size a file is generated or vendored; outlining it would
# cost a scan for nothing a model can use.
_MAX_FILE_BYTES = 2_000_000

_KEYWORD_RE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|private|protected|internal|abstract|final|sealed|static|async|open|data|partial|declare|"
    r"override|inline|suspend|external|factory|const|pub(?:\([^)]*\))?|unsafe|virtual)\s+)*"
    r"(?:class|interface|enum|mixin|extension|record|struct|trait|protocol|def|function\*?|func|fun|fn|"
    r"typedef|object|module|impl|namespace)\b\s*[A-Za-z_$(<]"
)
# `const load = async (x) =>`, `export let f = y =>`: an arrow function
# bound at module/class level, the one assignment worth listing.
_ARROW_RE = re.compile(
    r"^\s*(?:export\s+)?(?:(?:const|let|var|static|readonly|private|public|protected)\s+)*"
    r"[A-Za-z_$][\w$]*\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"
)
# `[type/modifier words ]name(` — group 1 is the prefix, group 2 the name
# (dotted for a named constructor: `factory Order.fromJson(`).
_SIGNATURE_RE = re.compile(
    r"^\s*((?:[\w$@][\w$<>\[\],.?@:*&]*(?:\s*<[^()]*>)?\s+)*)"
    r"([A-Za-z_$~][\w$]*(?:\.[A-Za-z_$][\w$]*)?)\s*(?:<[^()]*>)?\s*\("
)
# Words that make a `word name(` line a statement rather than a
# declaration, wherever they sit in the prefix.
_STATEMENT_WORDS = frozenset({
    "if", "for", "foreach", "while", "switch", "catch", "return", "new", "throw", "raise", "else", "do", "try",
    "synchronized", "await", "yield", "case", "assert", "with", "when", "super", "this", "print", "sizeof",
    "typeof", "delete", "in", "is", "as", "not", "and", "or", "elif", "except", "lambda", "using", "lock",
    "defer", "go", "select", "match", "unless", "until", "loop", "guard", "del", "echo", "goto", "import", "from",
})


def _indent_width(line: str) -> int:
    width = 0
    for char in line:
        if char == " ":
            width += 1
        elif char == "\t":
            width += 4
        else:
            break
    return width


def _indent_unit(lines: list[str]) -> int:
    """The file's nesting step: its smallest indentation that recurs.

    Recurs (three or more lines), so a stray alignment — a two-space
    continuation line in a four-space file — does not halve the unit and
    hide every nested member."""
    counts: dict[int, int] = {}
    for line in lines:
        if line.strip() and not line.lstrip().startswith(("*", "//", "#")):
            width = _indent_width(line)
            if width > 0:
                counts[width] = counts.get(width, 0) + 1
    recurring = [width for width, count in counts.items() if count >= 3]
    if recurring:
        return min(recurring)
    return min(counts) if counts else 4


def _is_declaration(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith(("//", "#", "*", "/*", "--", "@", "}", ")", ".", "<", "{")):
        return False
    if _KEYWORD_RE.match(line) or _ARROW_RE.match(line):
        return True
    match = _SIGNATURE_RE.match(line)
    if not match:
        return False
    words = match.group(1).split()
    name = match.group(2)
    if name in _STATEMENT_WORDS or any(word.rstrip(":") in _STATEMENT_WORDS for word in words):
        return False
    if "=" in line[: match.end()].replace("==", "").replace("=>", ""):
        return False  # `x = foo(` is a call, not a declaration
    if any(":" in word and "::" not in word for word in words):
        return False  # `child: Text(` — a named argument, not a declaration
    if words and words[0].endswith(",") and "<" not in words[0]:
        return False  # `key, other.get(` — the middle of an argument list
    if words:
        # A type or modifier before the name: a method, constructor or
        # abstract declaration (`List<Order> find(Query q);`). A call has
        # no space between receiver and name (`log.info(`), so it never
        # gets this far.
        return True
    # A bare `name(`: a declaration only when its body opens on the line
    # (`render() {`, `Foo(this.x) {`) and its parentheses hold parameters,
    # not a callback or a literal — `setState(() {`, `it("works", () {`,
    # `items.forEach(item -> {` are calls.
    if "." in name:
        return False
    arguments = stripped[stripped.index("(") + 1:]
    if "(" in arguments or '"' in arguments or "'" in arguments or "`" in arguments:
        return False
    # The parameter list closes before the body opens; `defineProps({` —
    # a call taking an object literal — never closes it on this line.
    return ")" in arguments and stripped.endswith("{")


# Python docstrings and Java/Kotlin text blocks. Backticks are left out: a
# lone "`" inside an ordinary string literal is common (a shell
# metacharacter list), and taking it for an opener would swallow the rest
# of the file.
_MULTILINE_STRING_DELIMITERS = ('"""', "'''")


def _opens_multiline_string(line: str, delimiter: str) -> bool:
    """`delimiter` occurs an odd number of times AND where a multi-line
    string begins — at the start or end of the line, or right after `=`,
    `(`, `,` or `:` (with an optional r/b/f prefix) — not quoted inside an
    ordinary string, as in `('\"\"\"', "'''")`."""
    if line.count(delimiter) % 2 == 0:
        return False
    stripped = line.strip()
    if stripped.startswith(delimiter) or stripped.endswith(delimiter):
        return True
    return re.search(r"[=(,:]\s*[rRbBuUfF]{0,2}" + re.escape(delimiter), line) is not None


def outline_source(source: str) -> list[tuple[int, str]]:
    """[(1-based line number, the declaration line as written)] for every
    declaration at most two indentation units deep, outside multi-line
    strings (a docstring's prose or a Java text block's SQL reads like
    `word word name(` too)."""
    lines = source.splitlines()
    unit = _indent_unit(lines)
    entries: list[tuple[int, str]] = []
    open_string: str | None = None
    for number, line in enumerate(lines, start=1):
        if open_string is not None:
            if line.count(open_string) % 2 == 1:
                open_string = None
            continue
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "*", "/*")):
            continue  # a comment's quotes and backticks open nothing
        opener = next((d for d in _MULTILINE_STRING_DELIMITERS if _opens_multiline_string(line, d)), None)
        if opener is not None:
            # Opens a string that runs past this line; whatever precedes
            # it (`query = """`) is an assignment, not a declaration.
            open_string = opener
            continue
        if _indent_width(line) > 2 * unit:
            continue
        if _is_declaration(line):
            text = line.rstrip()
            if len(text) > _MAX_ENTRY_CHARS:
                text = text[:_MAX_ENTRY_CHARS] + " …"
            entries.append((number, text))
    return entries


def outline_file(project_path: str, relative_path: str, *, max_entries: int = MAX_OUTLINE_ENTRIES) -> str:
    """The outline as tool output: a header, then `<line>: <declaration>`
    per entry, indentation kept so nesting shows."""
    abs_path = resolve_read_path(project_path, relative_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(relative_path)
    if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
        return f"[{relative_path} is too large to outline ({os.path.getsize(abs_path):,} bytes); search it instead]"
    with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
        source = handle.read()
    if "\x00" in source[:8192]:
        return f"[{relative_path} is a binary file; there is nothing to outline]"
    total_lines = source.count("\n") + (0 if source.endswith("\n") or not source else 1)
    entries = outline_source(source)
    if not entries:
        return (
            f"[no declarations recognized in {relative_path} ({total_lines} lines) — read it with "
            "read_file_tool, or search it]"
        )
    width = len(str(entries[-1][0]))
    shown = entries[:max_entries]
    body = "\n".join(f"{str(number).rjust(width)}: {text}" for number, text in shown)
    header = (
        f"[outline of {relative_path}: {len(entries)} declaration(s) in {total_lines} lines — line numbers are "
        "where to start a read_file_tool offset; heuristic, so the file itself is the authority]"
    )
    footer = f"\n... ({len(entries) - len(shown)} more not shown)" if len(entries) > len(shown) else ""
    return f"{header}\n{body}{footer}"
