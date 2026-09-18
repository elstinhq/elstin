"""forge/tools/workspace.py and the multi-repo read paths built on it.

A project with its UI and API in separate repos gets one writable repo per
story and the rest mounted read-only. The property that matters most here
is the asymmetry: every read tool spans the project, and no write tool can
be talked into touching a repo the story was not assigned.
"""
from __future__ import annotations

import os

import pytest

from forge.tools.dev_tools import TOOL_SCHEMAS, make_dev_tools
from forge.tools.path_guard import PathGuardError
from forge.tools.planner_tools import make_planner_tools
from forge.tools.workspace import (
    REFERENCE_SIGIL,
    Workspace,
    build_reference_roots,
    slug_label,
    workspace_from_state,
)


def _write(root: str, rel: str, content: str = "x\n") -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


@pytest.fixture()
def two_repos(tmp_path):
    ui = tmp_path / "web-ui"
    api = tmp_path / "api"
    _write(str(ui), "src/cart.ts", "export class Cart {}\n")
    _write(str(api), "models/cart.py", "class Cart:\n    total = 0\n")
    return str(ui), str(api)


# --- labels and root assembly ------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("API", "api"), ("web ui", "web-ui"), ("My_Repo!", "my-repo"), ("", "repo"), ("---", "repo")],
)
def test_slug_label(raw, expected):
    assert slug_label(raw) == expected


def test_reference_roots_exclude_the_primary_repo():
    repos = [{"path": "/srv/ui", "label": "ui"}, {"path": "/srv/api", "label": "api"}]

    assert build_reference_roots(repos, "/srv/ui") == {"api": "/srv/api"}


def test_reference_roots_fall_back_to_the_directory_name():
    repos = [{"path": "/srv/payments-api", "label": None}]

    assert build_reference_roots(repos, "/srv/ui") == {"payments-api": "/srv/payments-api"}


def test_colliding_labels_are_suffixed_rather_than_overwritten():
    """Two repos sharing a label would otherwise make one unreachable."""
    repos = [
        {"path": "/a/api", "label": "api"},
        {"path": "/b/api", "label": "api"},
        {"path": "/c/api", "label": "API"},
    ]

    roots = build_reference_roots(repos, "/srv/ui")

    assert roots == {"api": "/a/api", "api-2": "/b/api", "api-3": "/c/api"}


def test_a_single_repo_project_has_no_references():
    assert build_reference_roots([{"path": "/srv/ui", "label": "ui"}], "/srv/ui") == {}
    assert workspace_from_state("/srv/ui", None).has_references is False


# --- path resolution ---------------------------------------------------


def test_an_unprefixed_path_belongs_to_the_primary_repo(two_repos):
    ui, api = two_repos
    workspace = Workspace(primary=ui, references={"api": api})

    assert workspace.split_reference("src/cart.ts") == (None, "src/cart.ts")
    assert workspace.resolve_read("src/cart.ts") == os.path.realpath(os.path.join(ui, "src/cart.ts"))


def test_a_prefixed_path_resolves_inside_the_reference_repo(two_repos):
    ui, api = two_repos
    workspace = Workspace(primary=ui, references={"api": api})

    assert workspace.resolve_read("@api/models/cart.py") == os.path.realpath(os.path.join(api, "models/cart.py"))


def test_an_unknown_label_is_an_error_not_a_silent_primary_read(two_repos):
    """Falling through to the primary repo would hand the model a
    different file's contents under the name it asked for."""
    ui, api = two_repos
    workspace = Workspace(primary=ui, references={"api": api})

    with pytest.raises(PathGuardError, match="Unknown reference repo"):
        workspace.split_reference("@backend/models/cart.py")


def test_traversal_out_of_a_reference_repo_is_blocked(two_repos):
    """The reference roots get the same guard the primary one has."""
    ui, api = two_repos
    workspace = Workspace(primary=ui, references={"api": api})

    with pytest.raises(PathGuardError):
        workspace.resolve_read("@api/../../etc/passwd")


def test_describe_is_empty_without_references_so_prompts_do_not_change(two_repos):
    ui, api = two_repos

    assert Workspace(primary=ui).describe() == ""
    assert REFERENCE_SIGIL + "api" in Workspace(primary=ui, references={"api": api}).describe()


# --- planner tools span the project ------------------------------------


def test_planner_read_reaches_a_reference_repo(two_repos):
    ui, api = two_repos
    functions, _ = make_planner_tools(project_path=ui, reference_roots={"api": api})

    assert "class Cart:" in functions["read_file_tool"]("@api/models/cart.py")


def test_planner_grep_covers_both_repos_and_labels_the_hits(two_repos):
    """A labelled hit is one the model can read back verbatim, which is
    the whole point of prefixing rather than returning a bare path."""
    ui, api = two_repos
    functions, _ = make_planner_tools(project_path=ui, reference_roots={"api": api})

    result = functions["grep_tool"]("Cart")

    assert "src/cart.ts:1:" in result
    assert "@api/models/cart.py:1:" in result
    # The repo being changed leads, since that is the one the plan is about.
    assert result.index("src/cart.ts") < result.index("@api/models/cart.py")


def test_planner_find_files_covers_both_repos(two_repos):
    ui, api = two_repos
    functions, _ = make_planner_tools(project_path=ui, reference_roots={"api": api})

    result = functions["find_files_tool"]("*cart*")

    assert "src/cart.ts" in result
    assert "@api/models/cart.py" in result


def test_planner_tools_are_unchanged_for_a_single_repo_project(two_repos):
    ui, _ = two_repos
    functions, schemas = make_planner_tools(project_path=ui)

    assert "READ-ONLY repos" not in schemas[0]["function"]["description"]
    assert "BLOCKED" in functions["read_file_tool"]("@api/models/cart.py")


# --- dev tools read across, write only to the primary ------------------


def _dev(ui: str, api: str | None, allowed=frozenset({"src/cart.ts"})):
    functions, schemas = make_dev_tools(
        worktree_path=ui,
        allowed_paths=set(allowed),
        analyze_targets=[],
        allow_test_run=False,
        planned_test_files=[],
        reference_roots={"api": api} if api else None,
    )
    return functions, schemas


def test_dev_read_and_search_reach_a_reference_repo(two_repos):
    ui, api = two_repos
    functions, _ = _dev(ui, api)

    assert "class Cart:" in functions["read_file_tool"]("@api/models/cart.py")
    assert "@api/models/cart.py" in functions["search_codebase_tool"]("Cart")


@pytest.mark.parametrize("tool,args", [
    ("write_file_tool", ("@api/models/cart.py", "wiped")),
    ("edit_file_tool", ("@api/models/cart.py", "class Cart:", "class Gone:")),
    ("roll_back_file_tool", ("@api/models/cart.py",)),
])
def test_no_write_tool_can_touch_a_reference_repo(two_repos, tool, args):
    """The existing plan-scope check does this for free: a prefixed path is
    never in allowed_paths. Asserted per tool anyway, because this is the
    property that keeps a story's branch, commit and PR in one repo."""
    ui, api = two_repos
    functions, _ = _dev(ui, api)
    before = open(os.path.join(api, "models/cart.py")).read()

    result = functions[tool](*args)

    assert result.startswith("BLOCKED")
    assert open(os.path.join(api, "models/cart.py")).read() == before


def test_a_reference_path_is_not_writable_even_if_the_plan_names_it(two_repos):
    """Belt and braces: a plan that somehow carries a prefixed path still
    cannot escape the worktree, because the write resolves under it."""
    ui, api = two_repos
    functions, _ = _dev(ui, api, allowed={"@api/models/cart.py"})
    before = open(os.path.join(api, "models/cart.py")).read()

    functions["write_file_tool"]("@api/models/cart.py", "wiped")

    assert open(os.path.join(api, "models/cart.py")).read() == before
    assert os.path.isfile(os.path.join(ui, "@api/models/cart.py"))


def test_dev_schemas_are_the_shared_constant_for_a_single_repo(two_repos):
    """Byte-identical prompts for the single-repo case, which is what keeps
    a provider's cached prefix valid."""
    ui, api = two_repos

    assert _dev(ui, None)[1] is TOOL_SCHEMAS
    assert _dev(ui, api)[1] is not TOOL_SCHEMAS


def test_only_the_reading_tools_advertise_the_reference_repos(two_repos):
    ui, api = two_repos
    _, schemas = _dev(ui, api)

    advertised = {s["function"]["name"] for s in schemas if "READ-ONLY repos" in s["function"]["description"]}

    assert advertised == {"read_file_tool", "search_codebase_tool"}
