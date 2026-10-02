"""code_forge's only configuration store. No `.env` file — every setting
except the bootstrap `DATABASE_URL` (see forge/db/engine.py) lives in the
`app_config` Postgres table and is edited through the settings UI/API.

`CONFIG_FIELDS` is the schema: it lets the settings UI render every known
field (with its label/type/secret flag) even before a value has ever been
saved, and lets `get_config`/`get_bool_config` fall back to a sane default
when a row doesn't exist yet.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select

from forge.db.engine import session_scope
from forge.db.models import AppConfig, ProjectConfig


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    secret: bool
    default: str | None
    # "model" is a string the settings screens offer the provider's models for
    # (GET /api/config/models) and that a project may override.
    type: Literal["string", "bool", "model"]


CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField("jira_base_url", "JIRA base URL", False, None, "string"),
    ConfigField("jira_email", "JIRA account email", False, None, "string"),
    ConfigField("jira_api_token", "JIRA API token", True, None, "string"),
    ConfigField("jira_write_enabled", "Allow the framework to write to JIRA (transitions)", False, "false", "bool"),
    ConfigField("jira_attachments_dir", "JIRA attachment download directory", False, "/data/jira-attachments", "string"),
    ConfigField(
        "jira_filter_poll_seconds",
        "How often to re-run each project's ticket filter, in seconds (minimum 30)",
        False, "300", "string",
    ),
    # A ticket in one of these is set aside until it leaves it — not started,
    # not re-planned (forge/services/external_sync.py). Matched on the
    # status name, since JIRA files "On Hold" under To Do.
    ConfigField(
        "ticket_hold_statuses",
        "Source statuses that mean \"on hold\": forge leaves such a ticket alone until it moves out of them "
        "(comma-separated, any case)",
        False, "On Hold, Blocked, Paused, Parked, Deferred", "string",
    ),
    ConfigField("github_org", "GitHub organization", False, None, "string"),
    ConfigField("github_token", "GitHub token", True, None, "string"),
    ConfigField(
        "github_issues_write_enabled",
        "Allow forge to comment on and close GitHub issues (for projects whose tickets come from GitHub Issues)",
        False, "false", "bool",
    ),
    ConfigField(
        "git_author_name",
        "Commit author name (blank = commits are authored by code_forge itself)",
        False, None, "string",
    ),
    ConfigField(
        "git_author_email",
        "Commit author email (use the address on your GitHub account so commits are credited to you; code_forge is added as co-author)",
        False, None, "string",
    ),
    ConfigField(
        "manage_gitignore",
        "Add a repo's missing build-output entries (target/, node_modules/, …) to its .gitignore, committed on the epic branch",
        False, "true", "bool",
    ),
    ConfigField("llm_provider", "LLM provider (only 'openai' is wired today)", False, "openai", "string"),
    ConfigField(
        "llm_request_timeout_seconds",
        "Give up on a single model call after this many seconds (0 = never). A timed-out call is retried once. "
        "The slowest legitimate dev step seen took about 7.5 minutes",
        False, "900", "string",
    ),
    ConfigField("openai_api_key", "OpenAI API key", True, None, "string"),
    ConfigField(
        "llm_base_url",
        "OpenAI-compatible API base URL (blank = OpenAI's own API; set to reach Azure/OpenRouter/vLLM/Ollama/etc.)",
        False, None, "string",
    ),
    # LangSmith tracing (forge/services/tracing.py). Off unless turned on: a trace
    # carries whole prompts — ticket text, source code, tool output — to a third
    # party, so it is a decision, and one a project can make for itself.
    ConfigField(
        "langsmith_tracing_enabled",
        "Send run traces to LangSmith. Prompts, source code, ticket text and tool output leave this machine (off = nothing is sent)",
        False, "false", "bool",
    ),
    ConfigField("langsmith_api_key", "LangSmith API key", True, None, "string"),
    ConfigField("langsmith_project", "LangSmith project the traces go to", False, "forge", "string"),
    ConfigField(
        "langsmith_endpoint",
        "LangSmith API URL (blank = LangSmith cloud; set for the EU region or a self-hosted instance)",
        False, None, "string",
    ),
    ConfigField(
        "langsmith_workspace_id",
        "LangSmith workspace ID (only needed with an organization-scoped API key)",
        False, None, "string",
    ),
    ConfigField("worktrees_base_dir", "Worktrees base directory", False, "/data/repos", "string"),
    ConfigField("cloned_repos_base_dir", "Cloned project repos base directory", False, "/data/cloned-repos", "string"),
    ConfigField("data_dir", "Data directory (dev-loop baseline snapshots, etc.)", False, "/data", "string"),
    ConfigField(
        "auto_run_enabled",
        "Automatically dispatch and progress ready stories (off = wait for a manual Start)",
        False, "true", "bool",
    ),
    ConfigField("require_plan_approval", "Require plan approval before dev starts", False, "true", "bool"),
    # Stories an epic's dependencies allow to run at once do run at once, up to
    # this many across every epic (runs and merges both count — each one runs
    # a build). The rest start on a later poll as slots free up.
    ConfigField("max_parallel_stories", "Stories run in parallel at most (runs and merges, across all epics)", False, "3", "string"),
    ConfigField(
        "generate_project_guide",
        "Write an AGENTS.md-style guide (kept in forge, the repo is not touched) for a repo that has none, before its first plan",
        False, "false", "bool",
    ),
    ConfigField(
        "capture_lessons",
        "After a story that needed retries lands, propose short lessons for a person to accept into the project guide (one extra model call; nothing reaches an agent until accepted)",
        False, "false", "bool",
    ),
    ConfigField("dev_sdk_model", "Dev agent model", False, "gpt-5.1", "model"),
    ConfigField(
        "dev_web_access_enabled",
        "Let the dev agent search the web and fetch public pages (docs, package READMEs); internal/private addresses are always refused",
        False, "true", "bool",
    ),
    ConfigField(
        "brave_search_api_key",
        "Brave Search API key for the dev agent's web search (blank = fall back to DuckDuckGo, which is unofficial and can rate-limit)",
        True, None, "string",
    ),
    # forge/tools/agent_env.py: builds, tests and the dev shell get a minimal
    # environment, so a credential in the daemon's own never reaches code the
    # agent wrote. A private registry's token is the usual reason to list one.
    ConfigField(
        "agent_env_passthrough",
        "Extra environment variables that builds, tests and the dev agent's commands may see "
        "(comma-separated names, a trailing * matches a prefix; e.g. NPM_TOKEN, ARTIFACTORY_*). "
        "By default they get PATH, HOME, locale, proxy and toolchain variables and nothing that looks like a credential",
        False, None, "string",
    ),
    ConfigField("dev_sdk_effort", "Dev agent reasoning effort (low/medium/high/xhigh/max)", False, "medium", "string"),
    ConfigField("dev_sdk_max_turns", "Dev agent max turns per run (0 = unbounded)", False, "80", "string"),
    ConfigField("dev_sdk_max_budget_usd", "Dev agent max spend per run in USD (0 = unbounded)", False, "0", "string"),
    ConfigField(
        "dev_sdk_max_idle_turns",
        "Dev agent: stop early after this many consecutive turns with no file edit (0 = off; anti-looping guard for weaker models). "
        "This is the base for a small plan; a plan touching many files gets more (+1 per 2 extra files, up to 4x)",
        False, "4", "string",
    ),
    ConfigField("planner_sdk_model", "Planner agent model", False, "gpt-5.1", "model"),
    ConfigField("planner_sdk_effort", "Planner agent reasoning effort (low/medium/high/xhigh/max)", False, "medium", "string"),
    ConfigField("planner_sdk_max_turns", "Planner agent max turns per run (0 = unbounded)", False, "40", "string"),
    ConfigField("planner_sdk_max_retries", "Planner agent retries on a transient SDK error", False, "2", "string"),
    ConfigField("planner_sdk_max_budget_usd", "Planner agent max spend per run in USD (0 = unbounded)", False, "0", "string"),
    ConfigField(
        "planner_explorers_enabled",
        "Before planning, run parallel read-only agents to investigate the codebase "
        "(keeps their large file reads out of the planner's own context)",
        False, "true", "bool",
    ),
    ConfigField(
        "planner_explorer_max_agents",
        "Maximum parallel investigation agents per plan (0 = off; the scoping step often chooses fewer)",
        False, "3", "string",
    ),
    ConfigField("planner_explorer_max_turns", "Turn cap per investigation agent", False, "8", "string"),
    ConfigField(
        "planner_explorer_model",
        "Model for investigation agents (blank = use the planner model; a cheaper model usually suffices for reading code)",
        False, None, "model",
    ),
    ConfigField(
        "agent_compaction_token_threshold",
        "Compact an agent run's history once it exceeds this estimated token count "
        "(dev + planner). Sized for a ~1M-token context model; lower it to about "
        "100000 for a 200k-token model. Compaction tombstones old tool output, so a "
        "threshold set too low costs the agent detail it could have afforded to keep",
        False, "400000", "string",
    ),
    ConfigField(
        "story_max_budget_usd",
        "Maximum total spend per story in USD across planning, investigation, development "
        "and review (0 = unbounded). The outer bound the per-phase caps sit inside; "
        "counts what the provider reports it billed, else the forge/llm/pricing.py estimate",
        False, "0", "string",
    ),
    ConfigField("verifier_qa_model", "Verifier/QA review model", False, "gpt-5.1-mini", "model"),
    ConfigField("verifier_skip_llm_trivial_chars", "Skip LLM review below this diff size in chars (0 = never skip)", False, "0", "string"),
)

_FIELDS_BY_KEY: dict[str, ConfigField] = {f.key: f for f in CONFIG_FIELDS}


@dataclass(frozen=True)
class ConfigCategory:
    id: str
    title: str
    description: str
    keys: tuple[str, ...]


# The sections of the settings screen, in the order a person sets forge up:
# where tickets and code come from, which model, how work flows, how each
# agent behaves, then housekeeping. `keys` is both membership and the order
# the fields show in. Every key in CONFIG_FIELDS belongs to exactly one
# section — tests/test_config_store.py fails on a setting with no home, so
# the screen never needs a catch-all for a declared field.
CONFIG_CATEGORIES: tuple[ConfigCategory, ...] = (
    ConfigCategory(
        "tickets", "Ticket sources", "JIRA, and how often forge re-checks your ticket filters.",
        (
            "jira_base_url", "jira_email", "jira_api_token", "jira_write_enabled",
            "jira_filter_poll_seconds", "ticket_hold_statuses", "jira_attachments_dir",
        ),
    ),
    ConfigCategory(
        "github", "GitHub", "Pull requests, and GitHub Issues as a ticket source.",
        ("github_org", "github_token", "github_issues_write_enabled"),
    ),
    ConfigCategory(
        "models", "Model provider", "Where model calls go, and how long one may take.",
        ("llm_provider", "openai_api_key", "llm_base_url", "llm_request_timeout_seconds"),
    ),
    ConfigCategory(
        "workflow", "Workflow", "How stories are picked up, approved and run.",
        ("auto_run_enabled", "require_plan_approval", "max_parallel_stories", "generate_project_guide", "capture_lessons"),
    ),
    ConfigCategory(
        "planner", "Planner", "Reads the code and writes the plan, with optional investigation agents.",
        (
            "planner_sdk_model", "planner_sdk_effort", "planner_sdk_max_turns", "planner_sdk_max_retries",
            "planner_sdk_max_budget_usd", "planner_explorers_enabled", "planner_explorer_max_agents",
            "planner_explorer_max_turns", "planner_explorer_model",
        ),
    ),
    ConfigCategory(
        "dev", "Dev agent", "Writes the code from the approved plan.",
        (
            "dev_sdk_model", "dev_sdk_effort", "dev_sdk_max_turns", "dev_sdk_max_budget_usd",
            "dev_sdk_max_idle_turns", "dev_web_access_enabled", "brave_search_api_key", "agent_env_passthrough",
        ),
    ),
    ConfigCategory(
        "review", "Review", "The verifier and epic QA.",
        ("verifier_qa_model", "verifier_skip_llm_trivial_chars"),
    ),
    ConfigCategory(
        "limits", "Spend and context", "Ceilings that apply across agents.",
        ("story_max_budget_usd", "agent_compaction_token_threshold"),
    ),
    ConfigCategory(
        "git", "Git and storage", "Commit identity, and where forge keeps clones, worktrees and data.",
        (
            "git_author_name", "git_author_email", "manage_gitignore",
            "worktrees_base_dir", "cloned_repos_base_dir", "data_dir",
        ),
    ),
    ConfigCategory(
        "tracing", "Tracing", "Send run traces to LangSmith. Off by default.",
        (
            "langsmith_tracing_enabled", "langsmith_api_key", "langsmith_project",
            "langsmith_endpoint", "langsmith_workspace_id",
        ),
    ),
)


def list_config_categories() -> list[dict]:
    """The settings screen's sections, for GET /api/config/categories."""
    return [
        {"id": c.id, "title": c.title, "description": c.description, "keys": list(c.keys)} for c in CONFIG_CATEGORIES
    ]

# The subset of CONFIG_FIELDS a project can override — JIRA connection, the
# approval gates, the LangSmith switch, and the model side: which provider,
# endpoint and key its calls use and which model each role runs on.
# Everything else (GitHub, the other agent limits, worktree/data dirs) stays
# global, shared across all projects.
PROJECT_OVERRIDABLE_KEYS = frozenset({
    "jira_base_url",
    "jira_email",
    "jira_api_token",
    "jira_write_enabled",
    "auto_run_enabled",
    "require_plan_approval",
    "generate_project_guide",
    "capture_lessons",
    "ticket_hold_statuses",
    "manage_gitignore",
    "langsmith_tracing_enabled",
    "langsmith_project",
    # Which model each role runs on. A project that sets none uses the global
    # value (resolve_text), so a project can move one role without pinning
    # the rest.
    "planner_sdk_model",
    "planner_explorer_model",
    "dev_sdk_model",
    "verifier_qa_model",
    # Where its model calls go and as whom (forge/llm/registry.py). A project
    # that points at another endpoint must bring its own key: the global key
    # is never sent to an endpoint it was not issued for.
    "llm_provider",
    "llm_base_url",
    "openai_api_key",
})

_cache_lock = threading.Lock()
_cache: dict[str, str | None] | None = None


def _load_all_locked() -> dict[str, str | None]:
    with session_scope() as session:
        rows = session.execute(select(AppConfig)).scalars().all()
        return {row.key: row.value for row in rows}


def invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def _cached() -> dict[str, str | None]:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = _load_all_locked()
        return _cache


def get_config(key: str) -> str | None:
    """Raw string value: DB row if one exists, else the field's declared
    default, else None. Unknown keys (not in CONFIG_FIELDS) are still
    readable if a row exists — the schema documents the app's own fields,
    it isn't an enforced allowlist."""
    values = _cached()
    if key in values and values[key] is not None:
        return values[key]
    field = _FIELDS_BY_KEY.get(key)
    return field.default if field else None


def get_bool_config(key: str) -> bool:
    raw = get_config(key)
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def get_int_config(key: str, default: int = 0) -> int:
    raw = get_config(key)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def get_float_config(key: str, default: float = 0.0) -> float:
    raw = get_config(key)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def is_configured() -> bool:
    """Whether the minimum needed for the daemon to do anything useful is
    set — JIRA + an LLM key. Used to decide whether to poll or just log
    'not configured'."""
    return bool(get_config("jira_base_url") and get_config("jira_api_token") and get_config("openai_api_key"))


def set_config(key: str, value: str | None, *, secret: bool | None = None) -> None:
    field = _FIELDS_BY_KEY.get(key)
    resolved_secret = secret if secret is not None else (field.secret if field else False)
    with session_scope() as session:
        row = session.get(AppConfig, key)
        if row is None:
            row = AppConfig(key=key, value=value, secret=resolved_secret)
            session.add(row)
        else:
            row.value = value
            row.secret = resolved_secret
    invalidate_cache()


def list_config_for_display() -> list[dict]:
    """Every known field plus any unrecognized DB rows, secret values masked."""
    values = _cached()
    seen: set[str] = set()
    items: list[dict] = []

    for field in CONFIG_FIELDS:
        seen.add(field.key)
        raw = values.get(field.key)
        set_flag = raw is not None and raw != ""
        items.append(
            {
                "key": field.key,
                "label": field.label,
                "type": field.type,
                "secret": field.secret,
                "value": "••••••••" if (field.secret and set_flag) else (raw if raw is not None else field.default),
                "is_set": set_flag,
            }
        )

    for key, raw in values.items():
        if key in seen:
            continue
        items.append({"key": key, "label": key, "type": "string", "secret": False, "value": raw, "is_set": raw is not None})

    return items


# ---------------------------------------------------------------------------
# Project-scoped overrides. Deliberately no caching layer here (unlike the
# global _cache above) — read at most once per epic per daemon poll cycle,
# not a hot path. Absence of a row means "inherit the global value."
# ---------------------------------------------------------------------------


def get_project_config(project_id: int, key: str) -> str | None:
    with session_scope() as session:
        row = session.get(ProjectConfig, (project_id, key))
        return row.value if row is not None else None


def set_project_config(project_id: int, key: str, value: str | None) -> None:
    if key not in PROJECT_OVERRIDABLE_KEYS:
        raise ValueError(f"'{key}' is not a project-overridable setting")
    field = _FIELDS_BY_KEY.get(key)
    resolved_secret = field.secret if field else False
    with session_scope() as session:
        row = session.get(ProjectConfig, (project_id, key))
        if row is None:
            session.add(ProjectConfig(project_id=project_id, key=key, value=value, secret=resolved_secret))
        else:
            row.value = value
            row.secret = resolved_secret


def secret_values() -> set[str]:
    """The current value of every secret setting — global and any project's
    override — for forge/services/redaction.py, which scrubs them from what is
    persisted or sent to a third party. A row counts as secret when it is
    flagged so or when its key is a declared secret field, so a value saved
    before the flag existed is still covered. Blank values are left out."""
    declared = {f.key for f in CONFIG_FIELDS if f.secret}
    values: set[str] = set()
    with session_scope() as session:
        for row in session.execute(select(AppConfig)).scalars():
            if (row.secret or row.key in declared) and row.value and row.value.strip():
                values.add(row.value.strip())
        for row in session.execute(select(ProjectConfig)).scalars():
            if (row.secret or row.key in declared) and row.value and row.value.strip():
                values.add(row.value.strip())
    return values


def clear_project_config(project_id: int, key: str) -> None:
    """Removes the override — the project reverts to the global value."""
    with session_scope() as session:
        row = session.get(ProjectConfig, (project_id, key))
        if row is not None:
            session.delete(row)


def resolve_config(key: str, project_id: int | None) -> str | None:
    """Project override if one exists, else the existing global get_config."""
    if project_id is not None:
        override = get_project_config(project_id, key)
        if override is not None:
            return override
    return get_config(key)


def resolve_text(key: str, project_id: int | None) -> str | None:
    """A text setting as this project sees it: its own override, else the
    global value. None when neither is set, for the caller to default. A blank
    override counts as no override — the global value wins, never an empty
    model id or key."""
    return project_override(key, project_id) or (get_config(key) or "").strip() or None


def project_override(key: str, project_id: int | None) -> str | None:
    """What the project itself set for `key` — not the global fallback. None
    when it set nothing, or only a blank."""
    if project_id is None:
        return None
    return (get_project_config(project_id, key) or "").strip() or None


def resolve_bool_config(key: str, project_id: int | None) -> bool:
    return (resolve_config(key, project_id) or "").strip().lower() in ("1", "true", "yes", "on")


def list_project_config_for_display(project_id: int) -> list[dict]:
    """PROJECT_OVERRIDABLE_KEYS only, each flagged `overridden` so the UI
    can show 'using global default' vs a project-specific value."""
    with session_scope() as session:
        rows = {
            row.key: row.value
            for row in session.execute(select(ProjectConfig).where(ProjectConfig.project_id == project_id)).scalars()
        }

    items: list[dict] = []
    for field in CONFIG_FIELDS:  # deterministic order — PROJECT_OVERRIDABLE_KEYS is a frozenset
        if field.key not in PROJECT_OVERRIDABLE_KEYS:
            continue
        key = field.key
        overridden = key in rows
        raw = rows[key] if overridden else get_config(key)
        set_flag = raw is not None and raw != ""
        items.append(
            {
                "key": field.key,
                "label": field.label,
                "type": field.type,
                "secret": field.secret,
                "value": "••••••••" if (field.secret and set_flag) else (raw if raw is not None else field.default),
                "is_set": set_flag,
                "overridden": overridden,
            }
        )
    return items
