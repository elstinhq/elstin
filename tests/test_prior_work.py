"""The lookup of work already done on a ticket (forge/services/prior_work.py),
against real git repos: a clone with an `origin`, branches pushed the way a
teammate would push them."""
from __future__ import annotations

import subprocess

from forge.services.prior_work import is_searchable_key, needs_choice, start_candidate, survey_repo


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _branch_with(repo: str, branch: str, files: dict[str, str], *, start: str = "main", push: bool = True, keep_local: bool = False) -> str:
    """Commit `files` on `branch` (from `start`), push it, and by default
    drop the local copy — in forge's clone a teammate's branch exists only
    as `origin/<branch>`."""
    _git(repo, "checkout", "-q", "-b", branch, start)
    for path, text in files.items():
        with open(f"{repo}/{path}", "w") as handle:
            handle.write(text)
        _git(repo, "add", path)
        _git(repo, "commit", "-q", "-m", f"{branch}: {path}")
    tip = _git(repo, "rev-parse", "HEAD")
    if push:
        _git(repo, "push", "-q", "origin", branch)
    _git(repo, "checkout", "-q", "main")
    if push and not keep_local:
        _git(repo, "branch", "-q", "-D", branch)
    return tip


def test_a_pushed_branch_for_the_story_is_continued_from(git_repo):
    tip = _branch_with(git_repo, "feature/ENG-2-search-api", {"api.py": "def search(): ...\n", "schema.json": "{}\n"})

    finding = survey_repo(git_repo, {"ENG-2": ["ENG-2"]})["ENG-2"]

    assert finding["verdict"] == "continue" and finding["chosen"] == "feature/ENG-2-search-api"
    [candidate] = finding["candidates"]
    assert candidate["ref"] == "origin/feature/ENG-2-search-api" and candidate["tip"] == tip
    assert candidate["ahead"] == 2 and candidate["behind"] == 0 and candidate["in_default"] is False
    assert [c.split(" ", 1)[1] for c in candidate["commits"]] == [
        "feature/ENG-2-search-api: schema.json", "feature/ENG-2-search-api: api.py",
    ]
    assert candidate["files"] == ["A api.py", "A schema.json"] and candidate["files_total"] == 2
    assert candidate["last_author"] == "Test"


def test_the_key_must_be_a_whole_token(git_repo):
    _branch_with(git_repo, "feature/ENG-20-other", {"a.txt": "a\n"})
    _branch_with(git_repo, "XENG-2-other", {"b.txt": "b\n"})
    _branch_with(git_repo, "eng-2-lowercase", {"c.txt": "c\n"})

    finding = survey_repo(git_repo, {"ENG-2": ["ENG-2"]})["ENG-2"]

    assert [c["branch"] for c in finding["candidates"]] == ["eng-2-lowercase"]


def test_a_branch_named_after_a_sub_task_counts_for_its_story(git_repo):
    _branch_with(git_repo, "ENG-5-validation", {"v.py": "x = 1\n"})

    findings = survey_repo(git_repo, {"ENG-2": ["ENG-2", "ENG-5"], "ENG-3": ["ENG-3"]})

    assert findings["ENG-2"]["chosen"] == "ENG-5-validation"
    assert findings["ENG-3"]["verdict"] == "none"


def test_a_branch_that_contains_the_others_is_chosen_without_asking(git_repo):
    _branch_with(git_repo, "feature/ENG-2-first", {"a.txt": "a\n"}, keep_local=True)
    _branch_with(git_repo, "feature/ENG-2-rework", {"b.txt": "b\n"}, start="feature/ENG-2-first")

    finding = survey_repo(git_repo, {"ENG-2": ["ENG-2"]})["ENG-2"]

    assert finding["verdict"] == "continue" and finding["chosen"] == "feature/ENG-2-rework"


def test_diverged_branches_wait_for_a_human_to_pick(git_repo):
    _branch_with(git_repo, "feature/ENG-2-api", {"a.txt": "a\n"})
    _branch_with(git_repo, "ENG-2-spike", {"b.txt": "b\n"})

    finding = survey_repo(git_repo, {"ENG-2": ["ENG-2"]})["ENG-2"]
    assert finding["verdict"] == "choose" and finding["chosen"] is None

    stored = {"repos": {"widgets": finding}}
    assert needs_choice(stored) == ["widgets"]
    assert start_candidate(stored, "widgets") is None

    stored["picks"] = {"widgets": "ENG-2-spike"}
    assert needs_choice(stored) == []
    assert start_candidate(stored, "widgets")["ref"] == "origin/ENG-2-spike"

    stored["picks"] = {"widgets": ""}  # start fresh
    assert start_candidate(stored, "widgets") is None


def test_a_squash_merged_branch_is_already_in_main(git_repo):
    _branch_with(git_repo, "feature/ENG-2-done", {"done.py": "ok = True\n"})
    # Squash-merged: the same change lands on main as a different commit.
    with open(f"{git_repo}/done.py", "w") as handle:
        handle.write("ok = True\n")
    _git(git_repo, "add", "done.py")
    _git(git_repo, "commit", "-q", "-m", "Finish search (ENG-2) (#14)")
    _git(git_repo, "push", "-q", "origin", "main")

    finding = survey_repo(git_repo, {"ENG-2": ["ENG-2"]})["ENG-2"]

    assert finding["verdict"] == "in_default" and finding["chosen"] is None
    assert finding["candidates"][0]["in_default"] is True and finding["candidates"][0]["ahead"] == 1
    assert [c.split(" ", 1)[1] for c in finding["default_commits"]] == ["Finish search (ENG-2) (#14)"]
    assert start_candidate({"repos": {"widgets": finding}}, "widgets") is None


def test_commits_on_main_alone_are_reported(git_repo):
    with open(f"{git_repo}/x.py", "w") as handle:
        handle.write("x = 1\n")
    _git(git_repo, "add", "x.py")
    _git(git_repo, "commit", "-q", "-m", "eng-2: first cut\n\nRefs ENG-20 too")
    _git(git_repo, "push", "-q", "origin", "main")

    findings = survey_repo(git_repo, {"ENG-2": ["ENG-2"], "ENG-20": ["ENG-20"], "ENG-3": ["ENG-3"]})

    assert findings["ENG-2"]["verdict"] == "in_default" and len(findings["ENG-2"]["default_commits"]) == 1
    assert len(findings["ENG-20"]["default_commits"]) == 1
    assert findings["ENG-3"]["verdict"] == "none"


def test_unpushed_local_commits_are_what_is_continued_from(git_repo):
    """An attached checkout: the local branch is one commit ahead of its
    pushed copy."""
    _branch_with(git_repo, "ENG-2-tab", {"a.txt": "a\n"}, keep_local=True)
    _git(git_repo, "checkout", "-q", "ENG-2-tab")
    with open(f"{git_repo}/b.txt", "w") as handle:
        handle.write("b\n")
    _git(git_repo, "add", "b.txt")
    _git(git_repo, "commit", "-q", "-m", "not pushed yet")
    tip = _git(git_repo, "rev-parse", "HEAD")
    _git(git_repo, "checkout", "-q", "main")

    [candidate] = survey_repo(git_repo, {"ENG-2": ["ENG-2"]})["ENG-2"]["candidates"]

    assert candidate["ref"] == "ENG-2-tab" and candidate["tip"] == tip and candidate["unpushed"] is True


def test_a_local_copy_that_diverged_from_origin_is_two_choices(git_repo):
    _branch_with(git_repo, "ENG-2-tab", {"a.txt": "a\n"}, keep_local=True)
    _git(git_repo, "checkout", "-q", "ENG-2-tab")
    _git(git_repo, "reset", "-q", "--hard", "main")
    with open(f"{git_repo}/c.txt", "w") as handle:
        handle.write("c\n")
    _git(git_repo, "add", "c.txt")
    _git(git_repo, "commit", "-q", "-m", "rewritten locally")
    _git(git_repo, "checkout", "-q", "main")

    finding = survey_repo(git_repo, {"ENG-2": ["ENG-2"]})["ENG-2"]

    assert finding["verdict"] == "choose"
    assert sorted(c["branch"] for c in finding["candidates"]) == ["ENG-2-tab", "origin/ENG-2-tab"]


def test_forges_own_branches_are_not_prior_work(git_repo):
    _branch_with(git_repo, "feat/ENG-2-forge-made", {"a.txt": "a\n"}, keep_local=True, push=False)

    finding = survey_repo(git_repo, {"ENG-2": ["ENG-2"]}, exclude_branches={"feat/ENG-2-forge-made"})["ENG-2"]

    assert finding["verdict"] == "none"


def test_a_branch_carrying_two_tickets_says_so(git_repo):
    _branch_with(git_repo, "feature/ENG-2-ENG-3-combined", {"a.txt": "a\n"})

    findings = survey_repo(git_repo, {"ENG-2": ["ENG-2"], "ENG-3": ["ENG-3"]})

    assert findings["ENG-2"]["candidates"][0]["shared_with"] == ["ENG-3"]
    assert findings["ENG-3"]["candidates"][0]["shared_with"] == ["ENG-2"]


def test_a_repo_without_origin_is_looked_up_locally(tmp_path):
    repo = str(tmp_path / "local")
    subprocess.run(["git", "init", "-q", "-b", "master", repo], check=True)
    for args in (["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        _git(repo, *args)
    (tmp_path / "local" / "README.md").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "checkout", "-q", "-b", "ENG-2-local")
    (tmp_path / "local" / "a.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "work")
    _git(repo, "checkout", "-q", "master")

    finding = survey_repo(repo, {"ENG-2": ["ENG-2"]})["ENG-2"]

    assert finding["verdict"] == "continue" and finding["default_branch"] == "master"
    assert finding["candidates"][0]["ref"] == "ENG-2-local"


def test_only_jira_style_keys_are_looked_for():
    assert is_searchable_key("MKT2-848") and is_searchable_key("ENG-2")
    assert not is_searchable_key("gh.acme.widgets.3") and not is_searchable_key("3")
