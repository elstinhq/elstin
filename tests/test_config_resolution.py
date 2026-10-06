"""elstin/config/store.py's project-override-with-global-fallback logic:
resolve_config/resolve_bool_config, and the JiraService constructor that
consumes them."""
from __future__ import annotations

import pytest

from elstin.config.store import (
    clear_project_config,
    get_project_config,
    resolve_bool_config,
    resolve_config,
    set_config,
    set_project_config,
)
from elstin.state.manager import StateManager

pytestmark = pytest.mark.usefixtures("elstin_db")


def test_resolve_config_falls_back_to_global_when_no_override():
    project_id = StateManager().create_project("Widgets")
    set_config("jira_base_url", "https://global.atlassian.net")

    assert resolve_config("jira_base_url", project_id) == "https://global.atlassian.net"


def test_resolve_config_prefers_project_override():
    project_id = StateManager().create_project("Widgets")
    set_config("jira_base_url", "https://global.atlassian.net")
    set_project_config(project_id, "jira_base_url", "https://widgets.atlassian.net")

    assert resolve_config("jira_base_url", project_id) == "https://widgets.atlassian.net"
    assert resolve_config("jira_base_url", None) == "https://global.atlassian.net"  # global untouched


def test_clear_project_config_reverts_to_global():
    project_id = StateManager().create_project("Widgets")
    set_config("require_plan_approval", "true")
    set_project_config(project_id, "require_plan_approval", "false")
    assert resolve_bool_config("require_plan_approval", project_id) is False

    clear_project_config(project_id, "require_plan_approval")
    assert resolve_bool_config("require_plan_approval", project_id) is True
    assert get_project_config(project_id, "require_plan_approval") is None


def test_set_project_config_rejects_non_overridable_key():
    project_id = StateManager().create_project("Widgets")
    with pytest.raises(ValueError):
        set_project_config(project_id, "github_token", "ghp-x")  # still global: a GitHub token is not per project


def test_two_projects_have_independent_overrides():
    sm = StateManager()
    a = sm.create_project("A")
    b = sm.create_project("B")
    set_project_config(a, "jira_base_url", "https://a.atlassian.net")
    set_project_config(b, "jira_base_url", "https://b.atlassian.net")

    assert resolve_config("jira_base_url", a) == "https://a.atlassian.net"
    assert resolve_config("jira_base_url", b) == "https://b.atlassian.net"
