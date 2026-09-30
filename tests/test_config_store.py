"""Round-trips every known config key through get_config/set_config, and
through the actual HTTP API (forge/api/config_routes.py) that the settings
UI calls — confirms secret-flagged fields mask on read at both layers."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("forge_db")


def test_unset_fields_fall_back_to_declared_defaults():
    from forge.config.store import get_bool_config, get_config

    assert get_config("worktrees_base_dir") == "/data/repos"
    assert get_bool_config("require_plan_approval") is True
    assert get_config("jira_base_url") is None  # no default — genuinely unset


def test_set_config_round_trips_and_invalidates_cache():
    from forge.config.store import get_config, set_config

    set_config("jira_base_url", "https://acme.atlassian.net")
    assert get_config("jira_base_url") == "https://acme.atlassian.net"

    set_config("jira_base_url", "https://updated.atlassian.net")
    assert get_config("jira_base_url") == "https://updated.atlassian.net"


def test_bool_config_parsing():
    from forge.config.store import get_bool_config, set_config

    set_config("require_plan_approval", "false")
    assert get_bool_config("require_plan_approval") is False

    set_config("require_plan_approval", "true")
    assert get_bool_config("require_plan_approval") is True


def test_list_config_for_display_masks_secrets_but_not_plain_fields():
    from forge.config.store import list_config_for_display, set_config

    set_config("jira_base_url", "https://acme.atlassian.net")
    set_config("openai_api_key", "sk-openai-super-secret")

    items = {item["key"]: item for item in list_config_for_display()}

    assert items["jira_base_url"]["value"] == "https://acme.atlassian.net"
    assert items["jira_base_url"]["secret"] is False

    assert items["openai_api_key"]["secret"] is True
    assert items["openai_api_key"]["is_set"] is True
    assert items["openai_api_key"]["value"] == "••••••••"
    assert "super-secret" not in items["openai_api_key"]["value"]


def test_unset_secret_field_is_not_masked_as_if_set():
    from forge.config.store import list_config_for_display

    items = {item["key"]: item for item in list_config_for_display()}
    assert items["github_token"]["is_set"] is False
    assert items["github_token"]["value"] is None


def _client() -> TestClient:
    from forge.app import create_app

    return TestClient(create_app())


def test_api_get_config_returns_every_declared_field():
    with _client() as client:
        response = client.get("/api/config")
        assert response.status_code == 200
        keys = {item["key"] for item in response.json()}
        assert {"jira_base_url", "openai_api_key", "require_plan_approval"} <= keys


def test_api_put_then_get_reflects_the_write_and_masks_secrets():
    with _client() as client:
        put = client.put("/api/config/openai_api_key", json={"value": "sk-openai-abc123"})
        assert put.status_code == 200
        assert put.json() == {"key": "openai_api_key", "updated": True}

        items = {item["key"]: item for item in client.get("/api/config").json()}
        assert items["openai_api_key"]["value"] == "••••••••"
        assert items["openai_api_key"]["is_set"] is True


def test_api_put_nonsecret_field_returns_it_in_plain_text():
    with _client() as client:
        client.put("/api/config/jira_base_url", json={"value": "https://acme.atlassian.net"})
        items = {item["key"]: item for item in client.get("/api/config").json()}
        assert items["jira_base_url"]["value"] == "https://acme.atlassian.net"


# --- how the settings screen groups the fields --------------------------------


def test_every_declared_setting_belongs_to_exactly_one_section():
    """The screen renders sections, not a flat list: a setting added to
    CONFIG_FIELDS without a home would have nowhere to show."""
    from forge.config.store import CONFIG_CATEGORIES, CONFIG_FIELDS

    placed = [key for category in CONFIG_CATEGORIES for key in category.keys]

    assert sorted(placed) == sorted(field.key for field in CONFIG_FIELDS)
    assert len(placed) == len(set(placed)), "a setting is listed in two sections"


def test_sections_are_named_and_not_empty():
    from forge.config.store import CONFIG_CATEGORIES

    ids = [category.id for category in CONFIG_CATEGORIES]

    assert len(ids) == len(set(ids))
    for category in CONFIG_CATEGORIES:
        assert category.title.strip() and category.description.strip() and category.keys, category.id


def test_api_lists_the_sections_in_order_with_their_fields():
    from forge.config.store import CONFIG_CATEGORIES

    with _client() as client:
        response = client.get("/api/config/categories")

    assert response.status_code == 200
    sections = response.json()
    assert [section["id"] for section in sections] == [category.id for category in CONFIG_CATEGORIES]
    assert set(sections[0]) == {"id", "title", "description", "keys"}
    by_id = {section["id"]: section for section in sections}
    assert by_id["tracing"]["keys"][0] == "langsmith_tracing_enabled"
    assert by_id["tickets"]["keys"][:3] == ["jira_base_url", "jira_email", "jira_api_token"]
