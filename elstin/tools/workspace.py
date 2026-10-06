"""A story's workspace: one writable repo plus any number of read-only
reference repos from the same project.

The motivating case is a project whose UI and API live in separate repos.
A ticket usually changes one of them, but it cannot be planned or
implemented without reading the other: the UI change has to match a
contract the API repo defines. Before this, a story's agents were rooted
at exactly one directory and the path guard rejected everything outside
it, so the other repo was simply invisible.

No repo is bound to an epic or a story. Every repo on the project is in
the workspace, the planner reads all of them, and **the plan decides
which ones get written**: a path in `files_to_create`/`files_to_modify`
carries a `@label/` prefix to name its repo, or no prefix to mean the
project's default repo. A story may therefore write one repo or several,
and that is discovered from the task rather than configured ahead of it.

Paths use an explicit `@label/` prefix rather than a bare one. A bare
`api/...` is ambiguous the moment the default repo has a top-level
`api/` directory, and silently touching the wrong repo is far worse than
a tool error. The sigil also makes it obvious in a transcript, and in a
plan, which repo a path belongs to.

The consequence of writing several repos is unavoidable and is git's,
not elstin's: each written repo gets its own branch, its own commit and
its own pull request, and there is no transaction across them. A story
can therefore land in one repo and fail in another. Elstin makes that
visible — per-repo pull requests, all of which must be green before the
epic's integration branches merge — rather than pretending otherwise.

Reads reach a repo through its clone when no worktree exists for it yet
(planning, before any worktree is made) and through the story's worktree
once there is one, which is what lets dev read back what it just wrote.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from elstin.tools.path_guard import PathGuardError, resolve_read_path

# The prefix that addresses a reference repo. See the module docstring on
# why a bare label would be ambiguous.
REFERENCE_SIGIL = "@"

_LABEL_RE = re.compile(r"[^a-z0-9]+")


def slug_label(raw: str) -> str:
    """A short, path-safe label. Labels appear in every tool result and in
    every path the model writes back, so they have to survive a round trip
    through a JSON string and a path split."""
    slug = _LABEL_RE.sub("-", (raw or "").strip().lower()).strip("-")
    return slug or "repo"


def build_repo_roots(repos: list[dict], default_path: str | None) -> tuple[str, dict[str, str]]:
    """(default_label, {label: path}) for every repo on the project.

    `repos` is elstin/state/manager_project.py's project-repo dicts, so the
    caller stays the only thing touching the DB. Labels come from
    ProjectRepo.label when set, else the directory name, and collisions get
    a numeric suffix rather than silently overwriting — two repos sharing a
    label would make one unreachable.

    `default_path` is the repo an unprefixed path means (the epic's
    assigned folder). It is a default, not a binding: the plan may write
    any repo in the returned mapping."""
    default_real = os.path.realpath(default_path) if default_path else None
    roots: dict[str, str] = {}
    default_label = ""
    for repo in repos:
        path = repo.get("path")
        if not path:
            continue
        base = repo.get("label") or os.path.basename(os.path.normpath(path))
        label = slug_label(base)
        if label in roots:
            suffix = 2
            while f"{label}-{suffix}" in roots:
                suffix += 1
            label = f"{label}-{suffix}"
        roots[label] = path
        if default_real and os.path.realpath(path) == default_real:
            default_label = label

    if not default_label:
        # The epic's folder is not among the project's registered repos
        # (or none is assigned). Fall back to naming it directly, so a
        # workspace always has a default root to resolve against.
        if default_path:
            default_label = slug_label(os.path.basename(os.path.normpath(default_path)))
            roots.setdefault(default_label, default_path)
        elif roots:
            default_label = next(iter(roots))
    return default_label, roots


@dataclass(frozen=True)
class Workspace:
    """Every repo a story may read or write, keyed by label.

    `roots` maps label -> filesystem root. `default_label` is the repo an
    unprefixed path belongs to, which is what keeps a single-repo project
    behaving exactly as it did before any of this existed: one root, no
    prefixes anywhere, no change to a single prompt or path.

    Writes are not restricted here. Which repos a story writes is decided
    by its approved plan, and plan scope (elstin/tools/dev_tools.py's
    `_scope_error`) is what enforces it — this class only resolves a
    labelled path to a root."""

    default_label: str
    roots: dict[str, str] = field(default_factory=dict)
    # Labels no plan may write — read-only repos and reference folders
    # (elstin/db/models/project.py's ProjectRepo). Read like any other root;
    # elstin/agents/roles/plan_gate.py rejects a plan that changes one, and
    # dev never gets a worktree in one.
    read_only: frozenset[str] = frozenset()

    @property
    def default_root(self) -> str:
        return self.roots[self.default_label]

    @property
    def is_multi_repo(self) -> bool:
        return len(self.roots) > 1

    def labels(self) -> list[str]:
        """Default first, then the rest alphabetically — the order every
        search and listing reports in, so the repo an unprefixed path
        means always leads."""
        others = sorted(label for label in self.roots if label != self.default_label)
        return [self.default_label, *others] if self.default_label in self.roots else others

    def read_roots(self) -> list[tuple[str, str]]:
        """(label, path) for every readable root, in `labels()` order."""
        return [(label, self.roots[label]) for label in self.labels()]

    def split(self, path: str) -> tuple[str, str]:
        """(label, relative) for a path. An unprefixed path belongs to the
        default repo.

        An unknown label raises rather than falling back: a model that
        typed `@api/foo` meant the API repo, and resolving that against a
        different one would hand it the wrong file under the name it asked
        for — or, for a write, put the edit in the wrong repo."""
        text = str(path).strip()
        if not text.startswith(REFERENCE_SIGIL):
            return self.default_label, text
        label, _, remainder = text[len(REFERENCE_SIGIL) :].partition("/")
        label = label.strip()
        if label not in self.roots:
            known = ", ".join(f"{REFERENCE_SIGIL}{name}" for name in self.labels()) or "none"
            raise PathGuardError(
                f"Unknown repo '{REFERENCE_SIGIL}{label}'. Repos in this workspace: {known}. "
                "Omit the prefix for the default repo."
            )
        return label, remainder

    def root_for(self, path: str) -> tuple[str, str, str]:
        """(label, root, relative) for a path, without touching the disk."""
        label, relative = self.split(path)
        return label, self.roots[label], relative

    def resolve_read(self, path: str) -> str:
        """Absolute path for a read, guarded within whichever root it
        addresses. Traversal out of any root is blocked by the same check
        that protects the default one."""
        _label, root, relative = self.root_for(path)
        return resolve_read_path(root, relative)

    def display_path(self, label: str, relative_path: str) -> str:
        """How a path from `label` should be shown, so a later read or edit
        of it round-trips. Unprefixed for the default repo."""
        if label == self.default_label:
            return relative_path
        return f"{REFERENCE_SIGIL}{label}/{relative_path}"

    def describe(self) -> str:
        """One line naming the repos, for a tool description. Empty for a
        single-repo workspace, so those prompts are byte-identical to what
        they were before multi-repo support existed."""
        if not self.is_multi_repo:
            return ""
        others = [label for label in self.labels() if label != self.default_label]
        listed = ", ".join(f"{REFERENCE_SIGIL}{label}" for label in others)
        read_only = [label for label in self.labels() if label in self.read_only]
        return (
            f" This project spans several repos. Paths with no prefix belong to the default repo "
            f"({self.default_label}); prefix a path to address another: {listed}. "
            f"For example '{REFERENCE_SIGIL}{others[0]}/src/app.ts'. "
            + (
                f"Read-only — read these for context, never change them (a change the ticket needs there goes "
                f"in the plan's read_only_changes, for a person): "
                f"{', '.join(f'{REFERENCE_SIGIL}{label}' for label in read_only)}. "
                if read_only else ""
            )
            + "You may only create or modify a file that the approved plan lists, in whichever repo the plan lists it."
        )


def labels_in_plan(plan: dict, default_label: str) -> list[str]:
    """Every repo label the plan's file lists name, default first.

    This is how the set of repos a story writes is discovered: from the
    approved plan rather than from configuration. A plan with no prefixed
    path yields just the default, which is the single-repo case."""
    labels: list[str] = []
    for key in ("files_to_create", "files_to_modify"):
        for entry in plan.get(key) or []:
            raw = entry.get("path") if isinstance(entry, dict) else str(entry)
            if not raw:
                continue
            text = str(raw).strip()
            label = (
                text[len(REFERENCE_SIGIL) :].partition("/")[0].strip()
                if text.startswith(REFERENCE_SIGIL)
                else default_label
            )
            if label and label not in labels:
                labels.append(label)
    if default_label in labels:
        labels.remove(default_label)
        labels.insert(0, default_label)
    return labels


def workspace_from_state(default_label: str, repo_roots: dict[str, str] | None) -> Workspace:
    """Build a Workspace from the plain dicts carried in
    elstin/orchestration/state.py's FeatureRunState, which is JSON in a
    LangGraph checkpoint and so cannot hold the dataclass itself."""
    return Workspace(default_label=default_label, roots=dict(repo_roots or {}))
