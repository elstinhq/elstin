"""find_files (elstin/tools/find_files.py) over a workspace of several repos —
the `@repo/` prefix its own results carry, accepted back in a glob."""
from __future__ import annotations

from pathlib import Path

import pytest

from elstin.tools.find_files import find_files
from elstin.tools.workspace import Workspace


@pytest.fixture()
def workspace(tmp_path):
    api, web = tmp_path / "api", tmp_path / "web"
    (api / "src").mkdir(parents=True)
    (api / "src" / "Detail.java").write_text("class Detail {}\n")
    (web / "src" / "Customers").mkdir(parents=True)
    (web / "src" / "Customers" / "Detail.vue").write_text("<template/>\n")
    (web / "src" / "Customers" / "Panel.vue").write_text("<template/>\n")
    (web / "src" / "router.ts").write_text("export {}\n")
    return Workspace(default_label="api", roots={"api": str(api), "web": str(web)})


def test_double_star_slash_also_matches_no_folder(workspace):
    """`src/Customers/**/*` includes the files directly in Customers — fnmatch alone skips them."""
    deep = Path(workspace.roots["web"]) / "src" / "Customers" / "deep"
    deep.mkdir()
    (deep / "Nested.ts").write_text("export {}\n")
    found = find_files(workspace, "src/Customers/**/*").splitlines()
    assert found == [
        "@web/src/Customers/Detail.vue", "@web/src/Customers/Panel.vue", "@web/src/Customers/deep/Nested.ts",
    ]
    assert find_files(workspace, "**/router.ts") == "@web/src/router.ts"


def test_a_glob_without_a_prefix_searches_every_repo(workspace):
    assert find_files(workspace, "Detail.*").splitlines() == ["src/Detail.java", "@web/src/Customers/Detail.vue"]


def test_a_result_path_can_be_fed_back_as_a_glob(workspace):
    """The case that cost a PO draft two wrong assessments: the agent wrote the
    prefix it had been shown and was told nothing matched."""
    assert find_files(workspace, "@web/src/Customers/**/*").splitlines() == [
        "@web/src/Customers/Detail.vue", "@web/src/Customers/Panel.vue",
    ]
    assert find_files(workspace, "@web/src/Customers/Detail.vue") == "@web/src/Customers/Detail.vue"


def test_the_prefix_limits_the_search_to_that_repo(workspace):
    assert find_files(workspace, "@web/*.java") == "No files matched."
    assert find_files(workspace, "@web/**/*.java") == "No files matched."
    # the default repo may be named too, though its results come unprefixed
    assert find_files(workspace, "@api/src/*") == "src/Detail.java"
    # a bare repo prefix lists the repo
    assert "@web/src/router.ts" in find_files(workspace, "@web/")


def test_an_unknown_repo_says_which_exist_instead_of_matching_nothing(workspace):
    message = find_files(workspace, "@nope/src/*")
    assert "Unknown repo '@nope'" in message and "@web" in message and "@api" in message
