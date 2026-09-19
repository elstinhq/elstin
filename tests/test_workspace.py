"""forge/tools/workspace.py and the multi-repo tool paths built on it.

No repo is bound to an epic or a story. The whole project is the
workspace, the planner reads all of it, and the approved plan is what
decides which repos dev writes — one or several, discovered per task.

Two properties matter most here and are asserted directly: a path
resolves to the repo its prefix names and never silently to another, and
plan scope still authorises every write, so a writable repo does not
become a free-for-all.
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
    build_repo_roots,
    labels_in_plan,
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


def test_repo_roots_cover_every_project_repo_including_the_default():
    """No repo is bound to the epic or the story — the whole project is
    the workspace, and the plan decides which repos get written."""
    repos = [{"path": "/srv/ui", "label": "ui"}, {"path": "/srv/api", "label": "api"}]

    default_label, roots = build_repo_roots(repos, "/srv/ui")

    assert default_label == "ui"
    assert roots == {"ui": "/srv/ui", "api": "/srv/api"}


def test_repo_roots_fall_back_to_the_directory_name():
    repos = [{"path": "/srv/payments-api", "label": None}]

    _default, roots = build_repo_roots(repos, "/srv/payments-api")

    assert roots == {"payments-api": "/srv/payments-api"}


def test_colliding_labels_are_suffixed_rather_than_overwritten():
    """Two repos sharing a label would otherwise make one unreachable."""
    repos = [
        {"path": "/a/api", "label": "api"},
        {"path": "/b/api", "label": "api"},
        {"path": "/c/api", "label": "API"},
    ]

    _default, roots = build_repo_roots(repos, "/a/api")

    assert roots == {"api": "/a/api", "api-2": "/b/api", "api-3": "/c/api"}


def test_a_default_repo_outside_the_registered_list_still_gets_a_root():
    """An epic can point at a folder the project never registered; the
    workspace must still have something to resolve unprefixed paths to."""
    default_label, roots = build_repo_roots([], "/srv/legacy-ui")

    assert default_label == "legacy-ui"
    assert roots == {"legacy-ui": "/srv/legacy-ui"}


def test_a_single_repo_project_is_not_multi_repo():
    assert workspace_from_state("ui", {"ui": "/srv/ui"}).is_multi_repo is False


# --- which repos a plan writes -----------------------------------------


def test_plan_labels_come_from_the_paths_the_plan_names():
    """This is how the written repo set is discovered: from the approved
    plan, not from configuration."""
    plan = {
        "files_to_create": [{"path": "@api/models/cart.py"}],
        "files_to_modify": [{"path": "src/cart.ts"}, {"path": "@api/routes.py"}],
    }

    assert labels_in_plan(plan, "ui") == ["ui", "api"]


def test_a_plan_with_no_prefixes_writes_only_the_default_repo():
    plan = {"files_to_create": [{"path": "src/cart.ts"}], "files_to_modify": []}

    assert labels_in_plan(plan, "ui") == ["ui"]


def test_a_plan_that_never_touches_the_default_repo_says_so():
    """A UI-epic story that only changes the API repo should not get a
    pointless worktree in the UI repo."""
    plan = {"files_to_create": [{"path": "@api/models/cart.py"}], "files_to_modify": []}

    assert labels_in_plan(plan, "ui") == ["api"]


# --- path resolution ---------------------------------------------------


def _ws(ui: str, api: str) -> Workspace:
    return Workspace(default_label="ui", roots={"ui": ui, "api": api})


def test_an_unprefixed_path_belongs_to_the_default_repo(two_repos):
    ui, api = two_repos
    workspace = _ws(ui, api)

    assert workspace.split("src/cart.ts") == ("ui", "src/cart.ts")
    assert workspace.resolve_read("src/cart.ts") == os.path.realpath(os.path.join(ui, "src/cart.ts"))


def test_a_prefixed_path_resolves_inside_the_named_repo(two_repos):
    ui, api = two_repos

    assert _ws(ui, api).resolve_read("@api/models/cart.py") == os.path.realpath(
        os.path.join(api, "models/cart.py")
    )


def test_an_unknown_label_is_an_error_not_a_silent_default_read(two_repos):
    """Falling through to the default repo would hand the model a
    different file's contents under the name it asked for — or, for a
    write, put the edit in the wrong repo."""
    ui, api = two_repos

    with pytest.raises(PathGuardError, match="Unknown repo"):
        _ws(ui, api).split("@backend/models/cart.py")


def test_traversal_out_of_any_repo_is_blocked(two_repos):
    ui, api = two_repos

    with pytest.raises(PathGuardError):
        _ws(ui, api).resolve_read("@api/../../etc/passwd")


def test_the_default_repo_leads_the_ordering(two_repos):
    """Search results and listings report the default repo first, since an
    unprefixed path is the common case."""
    ui, api = two_repos

    assert _ws(ui, api).labels() == ["ui", "api"]


def test_describe_is_empty_for_one_repo_so_prompts_do_not_change(two_repos):
    ui, api = two_repos

    assert Workspace(default_label="ui", roots={"ui": ui}).describe() == ""
    assert REFERENCE_SIGIL + "api" in _ws(ui, api).describe()


# --- planner tools span the project ------------------------------------


def _planner(ui: str, api: str | None):
    roots = {"ui": ui} if api is None else {"ui": ui, "api": api}
    return make_planner_tools(project_path=ui, repo_roots=roots, default_label="ui")


def test_planner_read_reaches_another_repo(two_repos):
    ui, api = two_repos
    functions, _ = _planner(ui, api)

    assert "class Cart:" in functions["read_file_tool"]("@api/models/cart.py")


def test_planner_grep_covers_both_repos_and_labels_the_hits(two_repos):
    """A labelled hit is one the model can read back verbatim, which is
    the whole point of prefixing rather than returning a bare path."""
    ui, api = two_repos
    functions, _ = _planner(ui, api)

    result = functions["grep_tool"]("Cart")

    assert "src/cart.ts:1:" in result
    assert "@api/models/cart.py:1:" in result
    # The repo being changed leads, since that is the one the plan is about.
    assert result.index("src/cart.ts") < result.index("@api/models/cart.py")


def test_planner_find_files_covers_both_repos(two_repos):
    ui, api = two_repos
    functions, _ = _planner(ui, api)

    result = functions["find_files_tool"]("*cart*")

    assert "src/cart.ts" in result
    assert "@api/models/cart.py" in result


def test_planner_tools_are_unchanged_for_a_single_repo_project(two_repos):
    ui, _ = two_repos
    functions, schemas = _planner(ui, None)

    assert "spans several repos" not in schemas[0]["function"]["description"]
    assert "BLOCKED" in functions["read_file_tool"]("@api/models/cart.py")


# --- dev tools: read every repo, write the ones the plan names ---------


def _dev(ui: str, api: str | None, allowed=frozenset({"src/cart.ts"}), writes_api: bool = False):
    """write_roots is what the approved plan produced: the default repo
    always, plus any other repo the plan named."""
    write_roots = {"ui": ui}
    if api and writes_api:
        write_roots["api"] = api
    read_roots = {"ui": ui} if api is None else {"ui": ui, "api": api}
    return make_dev_tools(
        worktree_path=ui,
        allowed_paths=set(allowed),
        analyze_targets=[],
        allow_test_run=False,
        planned_test_files=[],
        default_label="ui",
        write_roots=write_roots,
        read_roots=read_roots,
    )


def test_dev_read_and_search_reach_every_repo(two_repos):
    """Readable regardless of whether the plan writes it — a UI change
    still has to be written against the API contract it matches."""
    ui, api = two_repos
    functions, _ = _dev(ui, api)

    assert "class Cart:" in functions["read_file_tool"]("@api/models/cart.py")
    assert "@api/models/cart.py" in functions["search_codebase_tool"]("Cart")


def test_dev_writes_the_repo_the_plan_named(two_repos):
    """The point of the whole model: the plan decides, so a story can edit
    the API repo when its plan says so."""
    ui, api = two_repos
    functions, _ = _dev(ui, api, allowed={"@api/models/cart.py"}, writes_api=True)

    result = functions["edit_file_tool"]("@api/models/cart.py", "total = 0", "total = 1")

    assert result.startswith("Successfully")
    assert "total = 1" in open(os.path.join(api, "models/cart.py")).read()


@pytest.mark.parametrize("tool,args", [
    ("write_file_tool", ("@api/models/cart.py", "wiped")),
    ("edit_file_tool", ("@api/models/cart.py", "class Cart:", "class Gone:")),
    ("roll_back_file_tool", ("@api/models/cart.py",)),
])
def test_a_write_outside_the_plan_is_refused_in_any_repo(two_repos, tool, args):
    """Plan scope is what authorises a write, and it is not weakened by
    the repo being writable — the path still has to be in the plan."""
    ui, api = two_repos
    functions, _ = _dev(ui, api, writes_api=True)
    before = open(os.path.join(api, "models/cart.py")).read()

    result = functions[tool](*args)

    assert result.startswith("BLOCKED")
    assert open(os.path.join(api, "models/cart.py")).read() == before


def test_a_repo_with_no_worktree_cannot_be_written_even_if_the_plan_names_it(two_repos):
    """A repo the plan did not name has no branch to carry a commit, so an
    edit there would be silently lost. Refused rather than landed."""
    ui, api = two_repos
    functions, _ = _dev(ui, api, allowed={"@api/models/cart.py"}, writes_api=False)
    before = open(os.path.join(api, "models/cart.py")).read()

    result = functions["edit_file_tool"]("@api/models/cart.py", "total = 0", "total = 1")

    assert result.startswith("BLOCKED")
    assert "Unknown repo" in result
    assert open(os.path.join(api, "models/cart.py")).read() == before


def test_dev_schemas_are_the_shared_constant_for_a_single_repo(two_repos):
    """Byte-identical prompts for the single-repo case, which is what keeps
    a provider's cached prefix valid."""
    ui, api = two_repos

    assert _dev(ui, None)[1] is TOOL_SCHEMAS
    assert _dev(ui, api)[1] is not TOOL_SCHEMAS


def test_the_prefix_aware_tools_are_the_reads_and_the_writes(two_repos):
    ui, api = two_repos
    _, schemas = _dev(ui, api)

    advertised = {s["function"]["name"] for s in schemas if "spans several repos" in s["function"]["description"]}

    assert advertised == {
        "read_file_tool", "search_codebase_tool", "write_file_tool", "edit_file_tool", "apply_edits_tool",
    }
