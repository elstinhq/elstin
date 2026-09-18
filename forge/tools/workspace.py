"""A story's workspace: one writable repo plus any number of read-only
reference repos from the same project.

The motivating case is a project whose UI and API live in separate repos.
A ticket usually changes one of them, but it cannot be planned or
implemented without reading the other: the UI change has to match a
contract the API repo defines. Before this, a story's agents were rooted
at exactly one directory and the path guard rejected everything outside
it, so the other repo was simply invisible.

The asymmetry is the whole design. Reads span every repo on the project;
writes stay confined to the one repo the epic is assigned to, which is
where the worktree, the branch, the commit and the pull request all live.
That keeps every hard part of multi-repo work out of scope: there is no
second branch to open, no second PR to gate, and no possibility of a
story half-landing across two repos with no transaction to undo it. When
work genuinely must change both repos, that is two stories in one epic,
which the existing wave ordering already sequences.

Reference paths carry an explicit `@label/` prefix — `@api/src/auth.py`
rather than `api/src/auth.py`. A bare prefix would be ambiguous the
moment the primary repo has a top-level directory of the same name, and
silently reading the wrong repo is far worse than a tool error. The sigil
also makes it obvious in a transcript which repo a claim came from.

Reference repos are read at their own checkout path, not in a worktree,
so they reflect whatever branch that clone currently has. That is a real
caveat rather than a hidden one: a reference read is "what the sibling
repo looks like on disk right now", which is exactly what a human reading
the other repo would see, and it is why references are read-only.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from forge.tools.path_guard import PathGuardError, resolve_read_path

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


def build_reference_roots(repos: list[dict], primary_path: str | None) -> dict[str, str]:
    """{label: path} for every project repo that is not the primary.

    `repos` is forge/state/manager_project.py's project-repo dicts, so the
    caller stays the only thing touching the DB. Labels come from
    ProjectRepo.label when set, else the directory name, and collisions get
    a numeric suffix rather than silently overwriting — two repos sharing a
    label would otherwise make one of them unreachable."""
    primary_real = os.path.realpath(primary_path) if primary_path else None
    roots: dict[str, str] = {}
    for repo in repos:
        path = repo.get("path")
        if not path:
            continue
        if primary_real and os.path.realpath(path) == primary_real:
            continue
        base = repo.get("label") or os.path.basename(os.path.normpath(path))
        label = slug_label(base)
        if label in roots:
            suffix = 2
            while f"{label}-{suffix}" in roots:
                suffix += 1
            label = f"{label}-{suffix}"
        roots[label] = path
    return roots


@dataclass(frozen=True)
class Workspace:
    """One writable root and zero or more read-only reference roots.

    Construct it once per run and hand it to the tool factories. Nothing
    here writes: `resolve_read` is the only resolver, and the write tools
    keep using the primary path directly, so no code path can be talked
    into writing to a reference repo."""

    primary: str
    references: dict[str, str] = field(default_factory=dict)

    @property
    def has_references(self) -> bool:
        return bool(self.references)

    def read_roots(self) -> list[tuple[str | None, str]]:
        """(label, path) for every readable root, primary first with a
        label of None. The order is what determines which repo a search
        reports first, so the repo the story actually changes leads."""
        return [(None, self.primary), *self.references.items()]

    def split_reference(self, path: str) -> tuple[str | None, str]:
        """(label, remainder) for an `@label/...` path, else (None, path).

        An unknown label raises rather than falling through to the primary
        repo: a model that typed `@api/foo` meant the API repo, and quietly
        resolving that against the primary would hand it the wrong file's
        contents under the name it asked for."""
        text = str(path).strip()
        if not text.startswith(REFERENCE_SIGIL):
            return None, text
        body = text[len(REFERENCE_SIGIL) :]
        label, _, remainder = body.partition("/")
        label = label.strip()
        if label not in self.references:
            known = ", ".join(f"{REFERENCE_SIGIL}{name}" for name in sorted(self.references)) or "none"
            raise PathGuardError(
                f"Unknown reference repo '{REFERENCE_SIGIL}{label}'. Available reference repos: {known}. "
                "Omit the prefix to read the repo this story is changing."
            )
        return label, remainder

    def resolve_read(self, path: str) -> str:
        """Absolute path for a read, guarded within whichever root it
        addresses. Traversal out of a reference root is blocked by the
        same check that protects the primary one."""
        label, remainder = self.split_reference(path)
        root = self.primary if label is None else self.references[label]
        return resolve_read_path(root, remainder)

    def display_path(self, label: str | None, relative_path: str) -> str:
        """How a path from `label`'s root should be shown to the model —
        prefixed for a reference repo so a later read of it round-trips."""
        if label is None:
            return relative_path
        return f"{REFERENCE_SIGIL}{label}/{relative_path}"

    def describe(self) -> str:
        """One line naming the reference repos, for a tool description.
        Empty when there are none, so a single-repo run's prompts are
        byte-identical to what they were before this existed."""
        if not self.references:
            return ""
        listed = ", ".join(f"{REFERENCE_SIGIL}{label}" for label in sorted(self.references))
        return (
            f" This project has additional READ-ONLY repos addressed by prefix: {listed}. "
            f"For example '{REFERENCE_SIGIL}{sorted(self.references)[0]}/src/app.ts'. "
            "Paths without a prefix refer to the repo this story is changing, which is the only one you can modify."
        )


def workspace_from_state(primary: str, reference_roots: dict[str, str] | None) -> Workspace:
    """Build a Workspace from the plain dict carried in
    forge/orchestration/state.py's FeatureRunState, which is JSON in a
    LangGraph checkpoint and so cannot hold the dataclass itself."""
    return Workspace(primary=primary, references=dict(reference_roots or {}))
