"""No database needed — exercises real git against a throwaway repo in a
temp dir, confirming branch naming, worktree creation/removal, and the
per-language dependency hook's "nothing recognized" path all work.
`git_repo` fixture lives in conftest.py (shared with test_daemon.py)."""
from __future__ import annotations

import logging
import os
import subprocess

import pytest

from forge.tools.worktree import (
    DEFAULT_TYPE_PREFIX,
    branch_name_for,
    branch_type_for,
    _fetch_origin,
    create_worktree,
    remove_worktree,
    slugify,
)


def test_slugify_and_branch_naming():
    assert slugify("Add a Widget!! To The Cart") == "add-a-widget-to-the-cart"
    # `<type>/<ticket ref>-<slug>`. An unlabelled ticket of no recognised
    # type takes the default prefix; the JIRA key is kept whole and upper,
    # because JIRA auto-links a branch by finding its key in the name.
    assert branch_name_for("eng-42", "Add a widget") == "feat/ENG-42-add-a-widget"


def test_branch_type_comes_from_labels_first_then_issue_type():
    """A label is the signal a human set on purpose, so it outranks the
    issue type — the same precedence forge/services/story_kind.py uses. It
    is also the only signal GitHub gives: every GitHub issue's issue_type
    is the literal "Issue", and enhancement/bug live in the labels."""
    assert branch_type_for("Issue", ["enhancement"]) == "feat"
    assert branch_type_for("Issue", ["bug"]) == "fix"
    assert branch_type_for("Bug", []) == "fix"
    assert branch_type_for("Task", ["chore"]) == "chore"
    # The label wins over a contradicting issue type.
    assert branch_type_for("Story", ["bug"]) == "fix"
    # Nothing recognisable falls back rather than inventing a prefix.
    assert branch_type_for(None, None) == DEFAULT_TYPE_PREFIX
    assert branch_type_for("Whatever", ["needs-triage"]) == DEFAULT_TYPE_PREFIX


def test_a_github_story_is_named_by_its_issue_number_not_the_internal_key():
    """`gh.leo-elstin.tg_case_study.3` is an identifier forge synthesises;
    it means nothing to a person reading `git branch`. The issue number
    does, and it is what the PR links back to."""
    assert branch_name_for(
        "gh.leo-elstin.tg_case_study.3", "Show the currency next to each price",
        issue_type="Issue", labels=["enhancement"],
        external_source="github", external_id="leo-elstin/tg_case_study#3",
    ) == "feat/3-show-the-currency-next-to-each-price"


def test_a_jira_key_survives_whole_so_jira_still_auto_links_the_branch():
    """forge/tools/worktree.py's module docstring: the key is in the branch
    on purpose. Shortening or lower-casing it silently breaks the link from
    the issue's development panel back to the work."""
    name = branch_name_for("ENG-123", "Prices page crashes on empty", issue_type="Bug")
    assert name == "fix/ENG-123-prices-page-crashes-on-empty"
    assert "ENG-123" in name


def test_an_explicit_branch_overrides_the_computed_one(git_repo, tmp_path):
    """One story writes one branch name in every repo it touches. The
    worktree for a second repo is created later, from the plan's summary
    rather than the story's, so it must be told the name rather than
    deriving a different one."""
    result = create_worktree(
        git_repo, story_key="ENG-2", summary="A quite different summary",
        worktrees_base_dir=str(tmp_path / "wt"), branch="feat/7-the-real-branch",
    )

    assert result["branch"] == "feat/7-the-real-branch"


def test_create_and_remove_worktree(git_repo, tmp_path):
    worktrees_dir = str(tmp_path / "worktrees")
    result = create_worktree(
        git_repo, story_key="ENG-2", summary="Add a widget", worktrees_base_dir=worktrees_dir
    )

    assert result["branch"] == "feat/ENG-2-add-a-widget"
    assert result["story_key"] == "ENG-2"
    import os

    assert os.path.isdir(result["worktree_path"])
    assert os.path.isfile(os.path.join(result["worktree_path"], "README.md"))
    assert result["dependencies"]["skipped"] is True  # no pom.xml/pubspec.yaml/etc in this fixture repo

    remove_worktree(git_repo, result["worktree_path"])
    assert not os.path.isdir(result["worktree_path"])


def test_create_worktree_reuses_existing_local_branch(git_repo, tmp_path):
    worktrees_dir = str(tmp_path / "worktrees")
    first = create_worktree(git_repo, story_key="ENG-3", summary="Retry me", worktrees_base_dir=worktrees_dir)
    remove_worktree(git_repo, first["worktree_path"])

    # branch ENG-3-retry-me still exists locally after the worktree was removed —
    # a second create_worktree call for the same story should reuse it, not fail.
    second = create_worktree(git_repo, story_key="ENG-3", summary="Retry me", worktrees_base_dir=worktrees_dir)
    assert second["branch"] == first["branch"]


def test_create_worktree_rejects_non_git_directory(tmp_path):
    from forge.tools.worktree import WorktreeError

    with pytest.raises(WorktreeError):
        create_worktree(
            str(tmp_path), story_key="ENG-4", summary="x", worktrees_base_dir=str(tmp_path / "wt")
        )


# --- discard_worktree: forgetting a ticket's worktree and local branch ------


def _local_branches(repo: str) -> set[str]:
    out = subprocess.run(["git", "-C", repo, "for-each-ref", "--format=%(refname:short)", "refs/heads"], capture_output=True, text=True, check=True)
    return set(out.stdout.split())


def test_discard_removes_the_worktree_and_its_local_branch_and_nothing_else(git_repo, tmp_path):
    from forge.tools.worktree import create_worktree, discard_worktree

    wt = create_worktree(git_repo, story_key="ENG-1", summary="Add a widget", worktrees_base_dir=str(tmp_path / "wt"))
    keep = create_worktree(git_repo, story_key="ENG-2", summary="Other work", worktrees_base_dir=str(tmp_path / "wt"))

    result = discard_worktree(git_repo, wt["worktree_path"], wt["branch"])

    assert result == {"worktree_removed": True, "branch_deleted": True, "errors": []}
    assert not os.path.exists(wt["worktree_path"])
    assert wt["branch"] not in _local_branches(git_repo)
    assert os.path.isdir(keep["worktree_path"]) and keep["branch"] in _local_branches(git_repo)
    assert "main" in _local_branches(git_repo)


def test_discard_never_touches_a_pushed_copy_of_the_branch(git_repo, tmp_path):
    """Removing a ticket is local only: what was pushed stays pushed."""
    from forge.tools.worktree import create_worktree, discard_worktree

    wt = create_worktree(git_repo, story_key="ENG-1", summary="Add a widget", worktrees_base_dir=str(tmp_path / "wt"))
    subprocess.run(["git", "-C", wt["worktree_path"], "push", "-q", "origin", wt["branch"]], check=True, capture_output=True)

    discard_worktree(git_repo, wt["worktree_path"], wt["branch"])

    remote = subprocess.run(["git", "-C", git_repo, "ls-remote", "--heads", "origin", wt["branch"]], capture_output=True, text=True, check=True)
    assert wt["branch"] in remote.stdout


def test_discard_is_idempotent_and_quiet_about_what_is_already_gone(git_repo, tmp_path):
    from forge.tools.worktree import create_worktree, discard_worktree

    wt = create_worktree(git_repo, story_key="ENG-1", summary="Add a widget", worktrees_base_dir=str(tmp_path / "wt"))
    discard_worktree(git_repo, wt["worktree_path"], wt["branch"])

    again = discard_worktree(git_repo, wt["worktree_path"], wt["branch"])

    assert again == {"worktree_removed": False, "branch_deleted": False, "errors": []}


def test_discard_removes_a_leftover_directory_git_no_longer_knows_about(git_repo, tmp_path):
    from forge.tools.worktree import discard_worktree

    stray = tmp_path / "wt" / "eng-1"
    stray.mkdir(parents=True)
    (stray / "file.txt").write_text("x")

    result = discard_worktree(git_repo, str(stray), None)

    assert result["worktree_removed"] is True and not stray.exists() and result["errors"] == []


def test_discard_refuses_to_delete_the_default_branch(git_repo):
    from forge.tools.worktree import discard_worktree

    result = discard_worktree(git_repo, None, "main")

    assert result["branch_deleted"] is False and "refusing to delete the default branch main" in result["errors"][0]
    assert "main" in _local_branches(git_repo)


def test_discard_still_clears_the_directory_when_the_repo_is_gone(tmp_path):
    from forge.tools.worktree import discard_worktree

    stray = tmp_path / "wt" / "eng-1"
    stray.mkdir(parents=True)

    result = discard_worktree(str(tmp_path / "no-such-repo"), str(stray), "ENG-1-x")

    assert result["worktree_removed"] is True and not stray.exists()
    assert result["branch_deleted"] is False and "is not a git repository" in result["errors"][0]


# --- the fetch that precedes every worktree ---------------------------------
#
# A private repo the machine has no git credentials for used to hang the
# whole daemon: `git fetch origin` inherited the daemon's terminal and sat
# waiting for a password to be typed. These pin what makes that impossible.


def _spy_on_fetch(monkeypatch, *, returncode=0, stderr="", raises=None):
    """Stands in for the `git fetch origin` subprocess only (every other git
    call still runs for real) and records how it was invoked."""
    seen: list[dict] = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if list(cmd[-2:]) != ["fetch", "origin"]:
            return real_run(cmd, *args, **kwargs)
        env = kwargs.get("env") or {}
        askpass = env.get("GIT_ASKPASS")
        seen.append({
            "env": dict(env), "stdin": kwargs.get("stdin"), "timeout": kwargs.get("timeout"),
            "askpass_existed": bool(askpass) and os.path.exists(askpass), "askpass": askpass,
        })
        if raises:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _set_origin(repo: str, url: str) -> None:
    subprocess.run(["git", "-C", repo, "remote", "set-url", "origin", url], check=True, capture_output=True)


def test_fetch_can_never_prompt_or_wait_forever(git_repo, monkeypatch):
    seen = _spy_on_fetch(monkeypatch)

    _fetch_origin(git_repo)

    assert len(seen) == 1
    assert seen[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen[0]["stdin"] == subprocess.DEVNULL
    assert seen[0]["timeout"]


def test_fetch_uses_the_github_token_for_a_github_origin_and_cleans_up(git_repo, monkeypatch):
    _set_origin(git_repo, "https://x-access-token@github.com/leo-elstin/fitpal")
    monkeypatch.setattr("forge.services.github_service.get_config", lambda key, *a, **k: "ghp_secret")
    seen = _spy_on_fetch(monkeypatch)

    _fetch_origin(git_repo)

    assert seen[0]["askpass_existed"]
    assert seen[0]["env"]["FORGE_PUSH_GIT_TOKEN"] == "ghp_secret"
    assert not os.path.exists(seen[0]["askpass"]), "the askpass script must not outlive the fetch"


def test_fetch_never_hands_the_github_token_to_another_host(git_repo, monkeypatch):
    _set_origin(git_repo, "https://gitlab.example.com/team/widgets.git")
    monkeypatch.setattr("forge.services.github_service.get_config", lambda key, *a, **k: "ghp_secret")
    seen = _spy_on_fetch(monkeypatch)

    _fetch_origin(git_repo)

    assert "GIT_ASKPASS" not in seen[0]["env"]
    assert "ghp_secret" not in seen[0]["env"].values()
    assert seen[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_a_failed_fetch_is_logged_but_does_not_stop_the_worktree(git_repo, tmp_path, monkeypatch, caplog):
    _spy_on_fetch(monkeypatch, returncode=128, stderr="fatal: could not read Username for 'https://github.com'")

    with caplog.at_level(logging.WARNING, logger="forge.worktree"):
        result = create_worktree(git_repo, story_key="ENG-9", summary="Private repo", worktrees_base_dir=str(tmp_path / "wt"))

    assert os.path.isdir(result["worktree_path"])
    assert "could not read Username" in caplog.text


def test_a_timed_out_fetch_does_not_stop_the_worktree(git_repo, tmp_path, monkeypatch, caplog):
    _spy_on_fetch(monkeypatch, raises=subprocess.TimeoutExpired(cmd="git fetch origin", timeout=120))

    with caplog.at_level(logging.WARNING, logger="forge.worktree"):
        result = create_worktree(git_repo, story_key="ENG-10", summary="Stalled remote", worktrees_base_dir=str(tmp_path / "wt"))

    assert os.path.isdir(result["worktree_path"])
    assert "timed out" in caplog.text
