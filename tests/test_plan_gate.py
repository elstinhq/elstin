"""forge/agents/roles/plan_gate.py — the deterministic checks a plan must
clear before it can be approved or handed to dev. Real filesystem via the
`git_repo` fixture, no DB and no provider: every check here is an
os.path question, which is the whole point of the module."""
from __future__ import annotations

import os

from forge.agents.roles.plan_gate import (
    KIND_CREATE_TARGET_EXISTS,
    KIND_EVIDENCE_MISSING,
    KIND_MODIFY_TARGET_MISSING,
    KIND_NO_CRITERIA,
    KIND_NO_FILES,
    KIND_QUESTION_PLACEHOLDER,
    KIND_UNKNOWN_REPO,
    blocking,
    check_plan,
    check_questions,
    format_plan_feedback,
)
from forge.tools.workspace import Workspace


def _write(root: str, rel: str, content: str = "x\n") -> None:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _ws(root: str, **others: str) -> Workspace:
    return Workspace(default_label="app", roots={"app": root, **others})


def _plan(**overrides) -> dict:
    base = {
        "files_to_create": [],
        "files_to_modify": [{"path": "README.md", "purpose": "x"}],
        "acceptance_criteria": ["it works"],
        "discovery_evidence": [],
    }
    base.update(overrides)
    return base


def _kinds(defects) -> set[str]:
    return {d["kind"] for d in defects}


def test_a_plan_whose_files_all_exist_passes(git_repo):
    assert check_plan(_plan(), _ws(git_repo)) == []


def test_a_modify_target_that_does_not_exist_is_a_defect(git_repo):
    """The real one. A planner reasoning from a truncated investigation
    named `openapi.json` when the file is at `pricing-web/openapi.json` —
    nothing checked it, so dev inherited the guess and burned its fix
    rounds discovering the path was wrong."""
    _write(git_repo, "pricing-web/openapi.json", "{}\n")
    plan = _plan(files_to_modify=[{"path": "openapi.json"}])

    defects = check_plan(plan, _ws(git_repo))

    assert _kinds(defects) == {KIND_MODIFY_TARGET_MISSING}
    # And it says where the file actually is, so the retry is a correction
    # rather than another guess.
    assert defects[0]["suggestions"] == ["pricing-web/openapi.json"]


def test_a_create_target_that_already_exists_is_a_defect(git_repo):
    """Planning an existing file as new invites dev to overwrite working
    code — it is a modification, and the plan should say so."""
    defects = check_plan(_plan(files_to_create=[{"path": "README.md"}]), _ws(git_repo))

    assert _kinds(defects) == {KIND_CREATE_TARGET_EXISTS}


def test_an_empty_plan_is_still_a_defect(git_repo):
    defects = check_plan(_plan(files_to_modify=[], acceptance_criteria=[]), _ws(git_repo))

    assert _kinds(defects) == {KIND_NO_FILES, KIND_NO_CRITERIA}


def test_a_citation_to_a_file_that_does_not_exist_is_a_warning_not_a_blocker(git_repo):
    """The plan's stated grounds are checkable, and a planner reasoning
    from files it never read cites lines that are not there. But a wrong
    citation does not make the plan unusable — dev never reads it — so it
    must not cost a retry. Rejecting on one threw away a good plan on this
    gate's first real run."""
    plan = _plan(discovery_evidence=["src/model/PriceResponse.java:16 declares no currency"])

    defects = check_plan(plan, _ws(git_repo))

    assert _kinds(defects) == {KIND_EVIDENCE_MISSING}
    assert blocking(defects) == []


def test_a_citation_past_the_end_of_a_real_file_is_a_defect(git_repo):
    _write(git_repo, "src/app.ts", "one\ntwo\n")
    plan = _plan(discovery_evidence=["src/app.ts:400 shows the mapping"])

    defects = check_plan(plan, _ws(git_repo))

    assert _kinds(defects) == {KIND_EVIDENCE_MISSING}
    assert "only 2 line(s)" in defects[0]["reason"]
    assert blocking(defects) == []


def test_a_citation_within_a_real_file_passes(git_repo):
    _write(git_repo, "src/app.ts", "one\ntwo\nthree\n")

    assert check_plan(_plan(discovery_evidence=["src/app.ts:2 is the mapping"]), _ws(git_repo)) == []


def test_a_bare_filename_citation_is_not_judged(git_repo):
    """`PriceResponse.java:16` is shorthand for a file the planner read,
    not a claim about a repo-relative location. Flagging it would reject
    good plans, so only a citation that says where it is gets checked."""
    assert check_plan(_plan(discovery_evidence=["PriceResponse.java:16 maps it"]), _ws(git_repo)) == []


def test_prose_that_merely_looks_like_a_citation_is_not_judged(git_repo):
    assert check_plan(_plan(discovery_evidence=["run `npm run generate:api` first"]), _ws(git_repo)) == []


def test_a_citation_relative_to_a_subdirectory_is_accepted(git_repo):
    """Regression, from this gate's first run against a real ticket. The
    planner cited `test/utils.tsx:21` for a file at
    `pricing-web/src/test/utils.tsx`. That is shorthand from an agent that
    had been reading inside that directory, not invention — the file is
    real and the citation names enough of it to be unambiguous. Judging it
    a phantom rejected an otherwise usable plan."""
    _write(git_repo, "pricing-web/src/test/utils.tsx", "\n" * 40)

    assert check_plan(_plan(discovery_evidence=["test/utils.tsx:21 seeds the fixture"]), _ws(git_repo)) == []


def test_a_shorthand_citation_is_still_judged_on_its_line_number(git_repo):
    """Accepting the path does not mean accepting the claim: the line is
    checked against the file the citation actually names."""
    _write(git_repo, "pricing-web/src/test/utils.tsx", "one\ntwo\n")

    defects = check_plan(_plan(discovery_evidence=["test/utils.tsx:900 seeds it"]), _ws(git_repo))

    assert _kinds(defects) == {KIND_EVIDENCE_MISSING}


def test_blocking_keeps_the_defects_dev_would_act_on(git_repo):
    plan = _plan(
        files_to_modify=[{"path": "nowhere.ts"}],
        discovery_evidence=["src/gone.ts:4 does it"],
    )

    defects = check_plan(plan, _ws(git_repo))

    assert _kinds(defects) == {KIND_MODIFY_TARGET_MISSING, KIND_EVIDENCE_MISSING}
    assert _kinds(blocking(defects)) == {KIND_MODIFY_TARGET_MISSING}


def test_a_path_in_another_repo_is_checked_against_that_repo(git_repo, tmp_path):
    api = tmp_path / "api"
    (api / "src").mkdir(parents=True)
    (api / "src" / "model.py").write_text("x\n")
    workspace = _ws(git_repo, api=str(api))

    assert check_plan(_plan(files_to_modify=[{"path": "@api/src/model.py"}]), workspace) == []
    # The same path without its prefix means the DEFAULT repo, where it
    # does not exist — which is exactly the mistake the prefix prevents.
    assert _kinds(check_plan(_plan(files_to_modify=[{"path": "src/model.py"}]), workspace)) == {KIND_MODIFY_TARGET_MISSING}


def test_a_path_naming_a_repo_the_project_does_not_have_is_a_defect(git_repo):
    defects = check_plan(_plan(files_to_modify=[{"path": "@nope/src/model.py"}]), _ws(git_repo))

    assert _kinds(defects) == {KIND_UNKNOWN_REPO}


def test_suggestions_never_point_into_dependencies_or_build_output(git_repo):
    _write(git_repo, "node_modules/pkg/openapi.json", "{}\n")
    _write(git_repo, "pricing-web/openapi.json", "{}\n")

    defects = check_plan(_plan(files_to_modify=[{"path": "openapi.json"}]), _ws(git_repo))

    assert defects[0]["suggestions"] == ["pricing-web/openapi.json"]


# --- clarification questions --------------------------------------------


def test_a_real_question_with_real_options_passes():
    questions = [{
        "id": "q0", "question": "Which layer should own the currency lookup?", "context": "seen in x.py",
        "options": [{"id": "A", "label": "The pricing service", "description": None}],
    }]
    assert check_questions(questions) == []


def test_a_question_with_no_options_passes():
    """The plan may be thin on a clarification round-trip — that includes
    an option list the planner hasn't filled in yet. Only stub *content*
    is a defect, not an incomplete shape."""
    questions = [{"id": "q0", "question": "Which layer?", "context": None, "options": []}]
    assert check_questions(questions) == []


def test_a_stub_question_is_a_defect():
    """Regression: the planner emitted id/question/context all literally
    "placeholder" and an option labelled "placeholder", which reached the
    human as if it were a real question."""
    questions = [{
        "id": "placeholder", "question": "placeholder", "context": "placeholder",
        "options": [{"id": "A", "label": "placeholder", "description": None}],
    }]

    defects = check_questions(questions)

    assert _kinds(defects) == {KIND_QUESTION_PLACEHOLDER}
    assert len(defects) == 2  # the question text and the option label
    assert blocking(defects) == defects


def test_a_blank_question_text_is_a_defect():
    questions = [{"id": "q0", "question": "   ", "context": None, "options": []}]

    assert _kinds(check_questions(questions)) == {KIND_QUESTION_PLACEHOLDER}


def test_a_stub_option_label_among_real_ones_is_a_defect():
    questions = [{
        "id": "q0", "question": "Which store should own this?", "context": None,
        "options": [
            {"id": "A", "label": "Redux", "description": None},
            {"id": "B", "label": "TBD", "description": None},
        ],
    }]

    defects = check_questions(questions)

    assert _kinds(defects) == {KIND_QUESTION_PLACEHOLDER}
    assert "'TBD'" in defects[0]["reason"]


# --- the retry brief ---------------------------------------------------


def test_feedback_names_every_bad_path_and_what_to_do_with_it(git_repo):
    _write(git_repo, "pricing-web/openapi.json", "{}\n")
    plan = _plan(
        files_to_modify=[{"path": "openapi.json"}],
        files_to_create=[{"path": "README.md"}],
        discovery_evidence=["src/gone.ts:4 does the mapping"],
    )

    text = format_plan_feedback(check_plan(plan, _ws(git_repo)), attempt=1, max_attempts=3)

    assert "3 problem(s)" in text
    assert "plan attempt 1/3" in text
    assert "openapi.json is listed under files_to_modify" in text
    assert "a file of that name exists at: pricing-web/openapi.json" in text
    assert "move them to files_to_modify" in text
    assert "src/gone.ts:4" in text
    # The instruction that stops the next attempt repeating the guess.
    assert "Verify each path" in text


def test_feedback_for_an_empty_plan_explains_why_empty_is_not_an_answer(git_repo):
    defects = check_plan(_plan(files_to_modify=[], acceptance_criteria=[]), _ws(git_repo))

    text = format_plan_feedback(defects, attempt=2, max_attempts=3)

    assert "names no files" in text
    assert "states no acceptance criteria" in text
    assert "empty plan ships the ticket as an empty change" in text
