"""forge/agents/roles/plan_gate.py — the deterministic checks a plan must
clear before it can be approved or handed to dev. Real filesystem via the
`git_repo` fixture, no DB and no provider: every check here is an
os.path question, which is the whole point of the module."""
from __future__ import annotations

import os

from forge.agents.roles.plan_gate import (
    KIND_CREATE_TARGET_EXISTS,
    KIND_EVIDENCE_MISSING,
    KIND_MALFORMED_OUTPUT,
    KIND_MODIFY_TARGET_MISSING,
    KIND_NO_CRITERIA,
    KIND_NO_FILES,
    KIND_QUESTION_PLACEHOLDER,
    KIND_STUB_PLAN,
    KIND_UNKNOWN_REPO,
    blocking,
    check_plan,
    check_plan_integrity,
    check_questions,
    describe_plan_shape,
    format_plan_feedback,
    normalize_plan,
    person_actions_of,
    plan_shape,
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


def test_feedback_for_an_empty_plan_points_an_implemented_ticket_at_files_declared_satisfied(git_repo):
    """An empty plan on a ticket that is already built used to be told to
    plan "the files that still need changing" — of which there are none —
    so it stayed empty and the story failed. The brief now names the way
    through: the existing files, under files_to_modify, for dev to declare."""
    defects = check_plan(_plan(files_to_modify=[]), _ws(git_repo))

    text = format_plan_feedback(defects, attempt=1, max_attempts=3)

    assert "under files_to_modify" in text
    assert '"Already satisfied:"' in text
    assert "finishes as done with no change" in text
    assert "Do not invent an edit" in text
    assert "still need changing" not in text


def test_a_plan_listing_existing_files_as_already_satisfied_clears_the_gate(git_repo):
    """What the planner is told to write for a built ticket must itself pass:
    the files exist, so nothing blocks, and there is nothing to warn about."""
    plan = _plan(files_to_modify=[{"path": "README.md", "purpose": "Already satisfied: it documents the widget"}])

    assert check_plan(plan, _ws(git_repo)) == []


# --- an answer that is not a plan object --------------------------------

# One planner's first answer on claude-sonnet-5 behind an OpenAI-compatible proxy: valid JSON
# whose only field held the model's own tool-call syntax, so every field after
# it was inside that string and none of them was a field of the plan.
_LEAKED = {
    "acceptance_criteria": (
        '["POST /search/items returns every matching item"]\n'
        '<parameter name="architecture">{"layers": "controller -> service"}\n'
        '<parameter name="files_to_modify">[{"path": "README.md", "purpose": "x"}]\n'
        '<parameter name="plan_markdown">## Plan'
    ),
}


def test_tool_call_markup_in_a_value_is_malformed_output_naming_what_it_swallowed():
    defects = check_plan_integrity(_LEAKED)

    assert _kinds(defects) == {KIND_MALFORMED_OUTPUT}
    assert len(defects) == 1
    reason = defects[0]["reason"]
    assert '"acceptance_criteria" holds tool-call markup' in reason
    assert "architecture, files_to_modify, plan_markdown" in reason
    assert blocking(defects) == defects


def test_markup_nested_inside_a_list_or_an_object_is_found_too():
    nested = _plan(acceptance_criteria=["fine", "<invoke name=\"x\">oops"], architecture={"layers": "<parameter name=\"a\">"})

    reasons = [d["reason"] for d in check_plan_integrity(nested)]

    assert any('"acceptance_criteria[1]"' in r for r in reasons)
    assert any('"architecture.layers"' in r for r in reasons)


def test_prose_that_merely_mentions_a_parameter_is_not_markup():
    plan = _plan(acceptance_criteria=["the request parameter `size` is clamped to 200", "a <div> is rendered"])

    assert check_plan_integrity(plan) == []


def test_a_string_where_an_array_belongs_is_malformed_output():
    plan = _plan(files_to_modify='[{"path": "README.md", "purpose": "x"}]', context_files="README.md")

    reasons = [d["reason"] for d in check_plan_integrity(plan)]

    assert '"files_to_modify" must be a JSON array but is a string (a JSON list written inside a string)' in reasons
    assert '"context_files" must be a JSON array but is a string' in reasons


def test_acceptance_criteria_as_one_string_is_still_tolerated():
    # `criteria_list` has always accepted this shape; the integrity check
    # must not start rejecting it.
    assert check_plan_integrity(_plan(acceptance_criteria="it works")) == []


def test_a_stub_feature_summary_is_a_defect():
    for stub in ("placeholder", "# placeholder", "TBD", "", "  n/a  "):
        defects = check_plan_integrity(_plan(feature_summary=stub))

        assert _kinds(defects) == {KIND_STUB_PLAN}, stub
        assert blocking(defects) == defects


def test_a_real_summary_that_starts_with_a_stub_word_is_not_a_stub():
    # A ticket about a search field's placeholder text is a plausible ticket.
    assert check_plan_integrity(_plan(feature_summary="Placeholder text for the empty search field")) == []


def test_a_clarification_plan_may_carry_stub_fields():
    # The plan is a placeholder until a person answers (planner_loop.py);
    # check_questions judges the part that reaches them.
    assert check_plan_integrity(_plan(feature_summary="placeholder"), clarifying=True) == []
    # Markup is wrong in any plan.
    assert _kinds(check_plan_integrity(_LEAKED, clarifying=True)) == {KIND_MALFORMED_OUTPUT}


def test_a_well_formed_plan_and_one_with_absent_fields_pass_integrity():
    assert check_plan_integrity(_plan(feature_summary="Add a widget", plan_markdown="# Plan")) == []
    # An absent field is `check_plan`'s to report as an empty plan, not a malformed one.
    assert check_plan_integrity({}) == []


def test_feedback_for_a_mangled_answer_names_the_cause_not_the_missing_files():
    text = format_plan_feedback(check_plan_integrity(_LEAKED), attempt=1, max_attempts=3)

    assert "problem(s) with the answer itself" in text
    assert "plan attempt 1/3" in text
    assert "architecture, files_to_modify, plan_markdown" in text
    assert "never <parameter> or <invoke> tags" in text
    assert "every field a top-level key" in text
    # None of check_plan's advice applies to an answer it never saw.
    assert "Checks against the repository" not in text
    assert "names no files" not in text
    assert "Verify each path" not in text


def test_feedback_for_a_stub_answer_says_to_investigate_first():
    text = format_plan_feedback(check_plan_integrity(_plan(feature_summary="placeholder")), attempt=2, max_attempts=3)

    assert "feature_summary is stub text ('placeholder')" in text
    assert "Do the investigation first" in text
    assert "never answer with placeholder text" in text


def test_plan_shape_of_a_mangled_answer_shows_the_fields_that_never_arrived():
    shape = plan_shape(_LEAKED)

    assert shape["fields"] == ["acceptance_criteria"]
    assert shape["files_to_modify"] == "missing"
    assert shape["files_to_create"] == "missing"
    assert shape["acceptance_criteria"] == f"string ({len(_LEAKED['acceptance_criteria'])} chars)"
    assert "files_to_modify missing" in describe_plan_shape(shape)


def test_plan_shape_of_an_empty_plan_shows_empty_lists_not_missing_ones():
    shape = plan_shape(_plan(files_to_modify=[], feature_summary="Add a widget", acceptance_criteria=["a", "b"], questions=[]))

    assert shape["files_to_modify"] == 0
    assert shape["acceptance_criteria"] == 2
    assert shape["questions"] == 0
    assert shape["feature_summary"] == "Add a widget"
    assert shape["files_to_create"] == 0 and "files_to_create" in shape["fields"]


def test_a_plan_that_changes_a_read_only_repo_is_rejected(tmp_path):
    """A read-only repo or reference folder is context, never a target. The
    planner loop moves such entries to read_only_changes first
    (normalize_plan, below); this is the backstop if one gets through."""
    from forge.agents.roles.plan_gate import KIND_READ_ONLY_REPO

    app = tmp_path / "app"
    (app / "src").mkdir(parents=True)
    (app / "src" / "main.ts").write_text("x\n")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "api.yaml").write_text("openapi: 3.0\n")
    workspace = Workspace(
        default_label="app", roots={"app": str(app), "contracts": str(contracts)}, read_only=frozenset({"contracts"}),
    )
    plan = {
        "files_to_create": [{"path": "@contracts/new.yaml"}],
        "files_to_modify": [{"path": "src/main.ts"}, {"path": "@contracts/api.yaml"}],
        "context_files": ["@contracts/api.yaml"],
        "acceptance_criteria": ["the API returns customers"],
    }

    defects = blocking(check_plan(plan, workspace))

    assert sorted((d["kind"], d["path"]) for d in defects) == [
        (KIND_READ_ONLY_REPO, "@contracts/api.yaml"), (KIND_READ_ONLY_REPO, "@contracts/new.yaml"),
    ]
    feedback = format_plan_feedback(defects, attempt=1, max_attempts=3)
    assert "read-only repo or reference folder" in feedback and "@contracts/api.yaml" in feedback
    # Reading it is exactly what it is for.
    assert blocking(check_plan({**plan, "files_to_create": [], "files_to_modify": [{"path": "src/main.ts"}]}, workspace)) == []


def test_the_tools_tell_the_planner_which_repos_are_read_only(tmp_path):
    workspace = Workspace(
        default_label="app", roots={"app": str(tmp_path), "contracts": str(tmp_path), "notes": str(tmp_path)},
        read_only=frozenset({"contracts", "notes"}),
    )
    assert "Read-only — read these for context, never change them (a change the ticket needs there goes in the plan's read_only_changes, for a person): @contracts, @notes." in workspace.describe()


# --- normalize_plan: fixed rather than sent back ---------------------------------


def _mono(tmp_path) -> Workspace:
    """One story's shape: the admin portal, the API, and a contract repo
    marked read-only."""
    for rel in ("portal/src/app/search.ts", "api/src/Search.java", "contracts/apis/v1/items.yaml"):
        _write(str(tmp_path), rel)
    return Workspace(
        default_label="mono-apiservice",
        roots={
            "mono-apiservice": str(tmp_path / "api"), "mono-adminportal": str(tmp_path / "portal"),
            "api-schemas": str(tmp_path / "contracts"),
        },
        read_only=frozenset({"api-schemas"}),
    )


def test_a_change_in_a_read_only_repo_is_listed_for_a_person_and_the_plan_goes_ahead(tmp_path):
    """One story: the ticket needed the OpenAPI contract changed, the contract
    repo is read-only, and three rejected plans failed the whole story."""
    workspace = _mono(tmp_path)
    plan = _plan(
        files_to_modify=[
            {"path": "src/Search.java", "purpose": "add the columns"},
            {"path": "@api-schemas/apis/v1/items.yaml", "purpose": "add the columns to the contract"},
        ],
        files_to_create=[{"path": "@api-schemas/apis/v1-bundled/items.yaml", "purpose": "bundle"}],
    )

    fixed, notes = normalize_plan(plan, workspace)

    assert fixed["files_to_modify"] == [{"path": "src/Search.java", "purpose": "add the columns"}]
    assert fixed["files_to_create"] == []
    assert fixed["read_only_changes"] == [
        {"path": "@api-schemas/apis/v1-bundled/items.yaml", "purpose": "bundle"},
        {"path": "@api-schemas/apis/v1/items.yaml", "purpose": "add the columns to the contract"},
    ]
    assert "@api-schemas/apis/v1/items.yaml" in fixed["context_files"]  # still read
    assert len(notes) == 2 and all("read-only repo" in note for note in notes)
    assert blocking(check_plan(fixed, workspace)) == []


def test_a_path_missing_its_at_sign_is_given_one(tmp_path):
    """One story's second attempt wrote `mono-adminportal/src/...` for every
    portal file and had eight "missing" files."""
    workspace = _mono(tmp_path)
    plan = _plan(
        files_to_modify=[{"path": "mono-adminportal/src/app/search.ts", "purpose": "columns"}],
        context_files=["mono-adminportal/src/app/search.ts"],
    )

    fixed, notes = normalize_plan(plan, workspace)

    assert fixed["files_to_modify"] == [{"path": "@mono-adminportal/src/app/search.ts", "purpose": "columns"}]
    assert fixed["context_files"] == ["@mono-adminportal/src/app/search.ts"]
    assert notes == ["mono-adminportal/src/app/search.ts is in another repo: read as @mono-adminportal/src/app/search.ts"] * 2
    assert blocking(check_plan(fixed, workspace)) == []


def test_a_folder_of_the_default_repo_named_like_another_repo_is_left_alone(tmp_path):
    workspace = _mono(tmp_path)
    _write(workspace.default_root, "mono-adminportal/config.json")

    fixed, notes = normalize_plan(_plan(files_to_modify=[{"path": "mono-adminportal/config.json"}]), workspace)

    assert fixed["files_to_modify"] == [{"path": "mono-adminportal/config.json"}] and notes == []


def test_a_file_in_a_writable_repo_left_to_a_person_stays_left_to_a_person(tmp_path):
    """One story: the workspace's CLAUDE.md says `bundled-oas.yaml`
    (an untracked copy of the contract) is not source, and the planner left
    it to a person. Moving it back into the plan had dev hand-write it. It
    stays, flagged for whoever approves the plan."""
    workspace = _mono(tmp_path)
    oas = {"path": "@mono-adminportal/bundled-oas.yaml", "purpose": "regenerate from the contract, publish"}
    plan = _plan(files_to_modify=[{"path": "src/Search.java", "purpose": "export"}], read_only_changes=[oas])

    fixed, notes = normalize_plan(plan, workspace)

    assert fixed["read_only_changes"] == [oas]
    assert fixed["files_to_create"] == [] and fixed["files_to_modify"] == [{"path": "src/Search.java", "purpose": "export"}]
    assert notes == [
        "@mono-adminportal/bundled-oas.yaml is in a repo forge can change, but the plan leaves it to a "
        "person — check that at approval"
    ]
    assert blocking(check_plan(fixed, workspace)) == []


def test_a_file_both_planned_and_left_to_a_person_is_planned(tmp_path):
    workspace = _mono(tmp_path)
    plan = _plan(
        files_to_modify=[{"path": "@mono-adminportal/src/app/search.ts", "purpose": "columns"}],
        read_only_changes=[{"path": "@mono-adminportal/src/app/search.ts", "purpose": "columns"}],
    )

    fixed, notes = normalize_plan(plan, workspace)

    assert fixed["read_only_changes"] == [] and notes == []
    assert fixed["files_to_modify"] == [{"path": "@mono-adminportal/src/app/search.ts", "purpose": "columns"}]


def test_a_plan_whose_only_changes_are_read_only_is_sent_back_saying_so(tmp_path):
    workspace = _mono(tmp_path)
    plan = _plan(files_to_modify=[{"path": "@api-schemas/apis/v1/items.yaml", "purpose": "contract"}])

    fixed, _ = normalize_plan(plan, workspace)
    defects = blocking(check_plan(fixed, workspace))

    assert _kinds(defects) == {KIND_NO_FILES}
    assert "every change this plan makes is in a read-only repo" in defects[0]["reason"]


def test_a_single_repo_plan_is_untouched(git_repo):
    plan = _plan()
    assert normalize_plan(plan, _ws(git_repo)) == (
        {**plan, "context_files": [], "read_only_changes": [], "person_actions": []}, []
    )


# --- actions left to a person ------------------------------------------------


def test_person_actions_are_read_whatever_shape_the_model_left_them_in():
    plan = {"person_actions": [
        {"action": "Run the script against the reference extract", "reason": "the agent has no access to it"},
        "Publish the delta sheet",
        {"action": "  ", "reason": "blank, so dropped"},
        {"action": "Publish the delta sheet", "reason": "a repeat, so kept once"},
        {"action": "Get Sales sign-off", "reason": ""},
        None,
    ]}

    assert person_actions_of(plan) == [
        {"action": "Run the script against the reference extract", "reason": "the agent has no access to it"},
        {"action": "Publish the delta sheet", "reason": None},
        {"action": "Get Sales sign-off", "reason": None},
    ]


def test_a_plan_with_no_person_actions_has_none_whether_absent_or_not_a_list():
    assert person_actions_of({}) == []
    assert person_actions_of({"person_actions": None}) == []
    assert person_actions_of({"person_actions": "run it yourself"}) == []


def test_normalize_plan_cleans_person_actions_for_every_later_reader(git_repo):
    plan = _plan(person_actions=["Check open pull requests for a clashing migration version", {"action": "", "reason": "x"}])

    fixed, _ = normalize_plan(plan, _ws(git_repo))

    assert fixed["person_actions"] == [{"action": "Check open pull requests for a clashing migration version", "reason": None}]


def test_person_actions_do_not_stand_in_for_planned_files(git_repo):
    # Not a way out of planning work: a plan that only hands things to a
    # person is still an empty plan.
    plan = _plan(files_to_modify=[], person_actions=[{"action": "Do all of it", "reason": None}])

    assert _kinds(check_plan(plan, _ws(git_repo))) == {KIND_NO_FILES}


def test_person_actions_that_are_not_a_list_are_malformed_output():
    reasons = [d["reason"] for d in check_plan_integrity(_plan(person_actions="Publish it"))]

    assert '"person_actions" must be a JSON array but is a string' in reasons
