"""forge adding a repo's missing build-output entries to its .gitignore
(forge/tools/worktree.py's missing_gitignore_entries / add_to_gitignore,
forge/daemon/main.py's _ignore_build_output_on_epic). MKT2-866: the
`mkt-apiservice` main branch had no .gitignore, and a build left target/
to be committed. Real git; agents are fakes."""
from __future__ import annotations

import os
import subprocess

import pytest

from forge.tools.git_tools import commit_paths
from forge.tools.worktree import add_to_gitignore, missing_gitignore_entries


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _write(repo: str, name: str, text: str) -> None:
    with open(os.path.join(repo, name), "w", encoding="utf-8") as handle:
        handle.write(text)


def test_a_maven_repo_without_a_gitignore_gets_one(git_repo):
    _write(git_repo, "pom.xml", "<project/>")

    assert missing_gitignore_entries(git_repo) == ["target/"]
    add_to_gitignore(git_repo, ["target/"])

    assert open(os.path.join(git_repo, ".gitignore")).read() == "# Build output (added by code_forge)\ntarget/\n"
    assert missing_gitignore_entries(git_repo) == []


def test_only_what_the_repo_does_not_already_ignore_is_added(git_repo):
    _write(git_repo, "vite.config.ts", "export default {}")
    _write(git_repo, ".gitignore", "node_modules\n/dist\n!coverage/")  # no trailing newline

    assert missing_gitignore_entries(git_repo) == ["coverage/"]  # a negated pattern does not ignore
    add_to_gitignore(git_repo, ["coverage/"])

    assert open(os.path.join(git_repo, ".gitignore")).read() == (
        "node_modules\n/dist\n!coverage/\n\n# Build output (added by code_forge)\ncoverage/\n"
    )


def test_this_machines_own_excludes_do_not_count(git_repo):
    """info/exclude (where forge hides build output locally) does nothing for
    the next person to clone the repo."""
    _write(git_repo, "pom.xml", "<project/>")
    with open(os.path.join(git_repo, ".git", "info", "exclude"), "a") as handle:
        handle.write("target/\n")

    assert missing_gitignore_entries(git_repo) == ["target/"]


def test_a_repo_without_a_known_toolchain_is_left_alone(git_repo):
    assert missing_gitignore_entries(git_repo) == []


def test_commit_paths_commits_only_what_it_is_given(git_repo):
    _write(git_repo, ".gitignore", "target/\n")
    _write(git_repo, "README.md", "changed, not part of this commit\n")

    commit_paths(git_repo, [".gitignore"], "Ignore build output: target/", author=("Leo", "leo@example.com"))

    from forge.tools.git_tools import CODE_FORGE_CO_AUTHOR

    assert _git(git_repo, "log", "-1", "--format=%an <%ae>") == "Leo <leo@example.com>"
    assert _git(git_repo, "log", "-1", "--format=%B") == f"Ignore build output: target/\n\n{CODE_FORGE_CO_AUTHOR}"
    assert _git(git_repo, "show", "--name-only", "--format=", "HEAD") == ".gitignore"
    assert _git(git_repo, "status", "--porcelain") == "M README.md"


# --- on the epic branch, before a story branches from it -------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("forge_db", "fake_agents")
@pytest.mark.parametrize("manage", [True, False])
async def test_the_epic_branch_gets_the_missing_entries(git_repo, tmp_path, project_id, manage, monkeypatch):
    import forge.daemon.main as daemon
    import forge.tools.worktree as worktree

    monkeypatch.setattr(worktree, "ensure_dependencies", lambda path: {"passed": True})  # no Maven here
    from forge.config.store import set_config, set_project_config
    from forge.orchestration.integration import integration_branch_name
    from forge.state.manager import StateManager

    _write(git_repo, "pom.xml", "<project/>")
    _git(git_repo, "add", "pom.xml")
    _git(git_repo, "commit", "-q", "-m", "pom")
    _git(git_repo, "push", "-q", "origin", "main")
    set_config("openai_api_key", "sk-openai-dummy")
    set_config("worktrees_base_dir", str(tmp_path / "worktrees"))
    set_config("require_plan_approval", "true")
    if not manage:
        set_project_config(project_id, "manage_gitignore", "false")
    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id, levels=[["ENG-2"]], manual_gate=False)
    sm.set_epic_project_repo("ENG-1", sm.add_project_repo(project_id, git_repo))
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0, summary="work", depends_on=[])

    await daemon.poll_once(sm, wait=True)
    await daemon.poll_once(sm, wait=True)  # never twice

    epic_branch = integration_branch_name("ENG-1")
    subjects = _git(git_repo, "log", "--format=%s", f"main..{epic_branch}").splitlines()
    story_worktree = sm.get_story("ENG-2")["worktree_path"]
    if manage:
        assert subjects == ["Ignore build output: target/"]
        assert _git(git_repo, "show", f"{epic_branch}:.gitignore") == "# Build output (added by code_forge)\ntarget/"
        assert os.path.isfile(os.path.join(story_worktree, ".gitignore"))  # the story branched after it
    else:
        assert subjects == []
        assert not os.path.exists(os.path.join(story_worktree, ".gitignore"))
