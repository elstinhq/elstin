"""forge/services/redaction.py — credentials never reach the activity feed or
LangSmith. Tool output is not curated: a command that prints its environment,
a file read that turns out to be a `.env`, a build log that echoes a token."""
from __future__ import annotations

import pytest

from forge.config.store import set_config, set_project_config
from forge.services import redaction, tracing
from forge.services.redaction import REDACTED, redact, redact_value

# Built from parts so the repo itself never holds a string that looks like a live token.
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
OPENAI = "sk-proj-" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe"
ANTHROPIC = "sk-ant-api03-" + "Qq1Ww2Ee3Rr4Tt5Yy6Uu7Ii8Oo9Pp0Aa"
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
SLACK = "xoxb-" + "123456789012-abcdefghijkl"
JWT = "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


@pytest.fixture(autouse=True)
def _fresh_cache():
    redaction.reset_cache()
    yield
    redaction.reset_cache()


# --- shapes -------------------------------------------------------------------


@pytest.mark.parametrize("token", [GITHUB, OPENAI, ANTHROPIC, AWS, SLACK, JWT])
def test_a_known_token_shape_is_replaced(token):
    out = redact(f"before {token} after", values=())
    assert token not in out
    assert out == f"before {REDACTED} after"


def test_a_private_key_block_is_replaced_whole():
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabcdef\n-----END RSA PRIVATE KEY-----"
    assert redact(f"key:\n{block}\ndone", values=()) == f"key:\n{REDACTED}\ndone"


def test_a_private_key_block_cut_before_its_end_line_is_still_replaced():
    """Activity detail is truncated, so the END line can be missing."""
    out = redact("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC", values=())
    assert out == REDACTED


def test_a_bearer_header_keeps_its_scheme_and_loses_the_token():
    out = redact("curl -H 'Authorization: Bearer abcdef0123456789ABCDEF' https://x", values=())
    assert "abcdef0123456789ABCDEF" not in out
    assert "Authorization: Bearer " + REDACTED in out


def test_the_password_in_a_url_goes_and_the_rest_of_the_url_stays():
    out = redact("clone https://deploy:s3cr3tpassw0rd@git.example.com/org/repo.git", values=())
    assert out == f"clone https://deploy:{REDACTED}@git.example.com/org/repo.git"


@pytest.mark.parametrize(
    "text",
    [
        "disk-usage-statistics-report-for-the-weekly-job",  # contains "sk-", but not as a token
        "git@github.com:org/repo.git",
        "https://example.com:8080/path",
        "task-queue-worker-initialisation-complete",
        "postgresql-16 is installed",
        "AKIA is a prefix, and so is ghp_",
        "def build_key(self): return self.key",
    ],
)
def test_ordinary_text_is_left_alone(text):
    assert redact(text, values=()) == text


# --- values -------------------------------------------------------------------


def test_a_value_forge_holds_is_replaced_even_with_no_recognisable_shape():
    secret = "correct-horse-battery-staple"
    assert redact(f"password is {secret}!", values=[secret]) == f"password is {REDACTED}!"


def test_a_short_value_is_not_replaced_because_it_would_damage_ordinary_text():
    """`POSTGRES_PASSWORD=postgres` once turned `postgresql-16` into `[REDACTED]ql-16`."""
    assert redact("apt install postgresql-16", values=["postgres"]) == "apt install postgresql-16"


def test_the_longer_of_two_overlapping_values_is_replaced_whole():
    inner, outer = "abcdefghijkl", "abcdefghijkl-mnopqrstuvwx"
    assert redact(f"x {outer} y", values=[inner, outer]) == f"x {REDACTED} y"


def test_empty_text_and_none_pass_through():
    assert redact("") == ""
    assert redact_value(None) is None
    assert redact_value(7) == 7


def test_the_environments_credential_named_values_are_collected_and_the_rest_are_not():
    env = {
        "GITHUB_TOKEN": GITHUB,
        "DATABASE_URL": "postgresql://forge:hunter2hunter2@db/forge",
        "SSH_AUTH_SOCK": "/private/tmp/com.apple.launchd.abc/Listeners",
        "PATH": "/usr/bin:/bin",
        "EMPTY_TOKEN": "",
    }
    assert redaction.environment_secret_values(env) == {GITHUB, "postgresql://forge:hunter2hunter2@db/forge"}


# --- JSON-like values ----------------------------------------------------------


def test_every_string_inside_a_nested_value_is_scrubbed_and_nothing_else_changes():
    value = {
        "messages": [{"role": "tool", "content": f"token={GITHUB}"}, {"role": "user", "content": "hi"}],
        "count": 3,
        "nested": {"deep": [("a", f"x {OPENAI}")]},
        GITHUB: "keys are left alone",
    }
    out = redact_value(value, values=())
    assert out["messages"][0]["content"] == f"token={REDACTED}"
    assert out["messages"][1] == {"role": "user", "content": "hi"}
    assert out["count"] == 3
    assert out["nested"]["deep"] == [["a", f"x {REDACTED}"]]
    assert GITHUB in out  # a key is not text the agent produced


def test_a_value_nested_too_deeply_to_walk_is_still_scrubbed():
    value: object = {"leaf": GITHUB}
    for _ in range(60):
        value = {"next": value}
    assert GITHUB not in str(redact_value(value, values=()))


# --- what forge holds ----------------------------------------------------------


@pytest.mark.usefixtures("forge_db")
def test_secret_settings_global_and_per_project_are_scrubbed_by_value(project_id):
    set_config("openai_api_key", "global-model-key-0123456789")
    set_config("github_token", "gh-token-with-no-known-shape-1")
    set_config("jira_base_url", "https://jira.example.com")  # not a secret
    set_project_config(project_id, "openai_api_key", "project-model-key-abcdefghij")
    redaction.reset_cache()

    text = (
        "a global-model-key-0123456789 b gh-token-with-no-known-shape-1 "
        "c project-model-key-abcdefghij d https://jira.example.com"
    )
    assert redact(text) == f"a {REDACTED} b {REDACTED} c {REDACTED} d https://jira.example.com"


@pytest.mark.usefixtures("forge_db")
def test_a_value_set_after_the_cache_was_filled_is_covered_once_it_expires(monkeypatch):
    set_config("openai_api_key", "first-key-0123456789abc")
    assert redact("x first-key-0123456789abc") == f"x {REDACTED}"

    set_config("github_token", "second-token-0123456789")
    assert "second-token-0123456789" in redact("x second-token-0123456789")  # still inside the TTL

    clock = [redaction.time.monotonic() + redaction._VALUES_TTL_SECONDS + 1]
    monkeypatch.setattr(redaction.time, "monotonic", lambda: clock[0])
    assert redact("x second-token-0123456789") == f"x {REDACTED}"


def test_unreadable_settings_fall_back_to_shapes_and_do_not_fail(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert redact(f"x {GITHUB}") == f"x {REDACTED}"


# --- where it is applied --------------------------------------------------------


@pytest.mark.usefixtures("forge_db")
def test_the_activity_feed_stores_and_streams_the_scrubbed_text(project_id, monkeypatch):
    import forge.services.run_activity as run_activity
    from forge.state.manager import StateManager

    sm = StateManager()
    sm.create_epic("ENG-1", project_id=project_id)
    sm.create_story("ENG-2", epic_key="ENG-1", wave=0)
    published: list[dict] = []
    monkeypatch.setattr(run_activity, "publish", published.append)

    event = run_activity.append_activity(
        "ENG-2", type="tool", phase="dev", title=f"Called tool: run_command {GITHUB}",
        detail=f"output: {OPENAI}", meta={"stdout": f"AWS_KEY={AWS}", "ok": True},
    )

    [stored] = run_activity.list_activity("ENG-2")
    for shown in (event, stored, published[0]):
        blob = repr(shown)
        assert GITHUB not in blob and OPENAI not in blob and AWS not in blob
    assert stored["detail"] == f"output: {REDACTED}"
    assert stored["meta"] == {"stdout": f"AWS_KEY={REDACTED}", "ok": True}


@pytest.mark.usefixtures("forge_db")
def test_the_langsmith_client_scrubs_inputs_outputs_errors_and_metadata(monkeypatch):
    monkeypatch.setattr(tracing, "_clients", {})  # a client built here must not outlive the test
    set_config("langsmith_tracing_enabled", "true")
    set_config("langsmith_api_key", "ls-test-key-not-a-real-one")
    client = tracing._client(tracing.tracing_settings(None))

    assert client._anonymizer is redact_value
    inputs = client._hide_run_inputs({"messages": [{"role": "tool", "content": f"x {GITHUB}"}]})
    outputs = client._hide_run_outputs({"output": f"y {OPENAI}", "is_error": False})
    assert inputs == {"messages": [{"role": "tool", "content": f"x {REDACTED}"}]}
    assert outputs == {"output": f"y {REDACTED}", "is_error": False}
    assert client._hide_run_error(f"RuntimeError: Bearer {'z' * 24}") == f"RuntimeError: Bearer {REDACTED}"
    assert client._hide_run_metadata({"note": ANTHROPIC}) == {"note": REDACTED}
