"""forge/tools/agent_env.py — the environment a command run for an agent sees.

Code the agent wrote runs inside these commands, so the daemon's credentials
(DATABASE_URL, tokens in the shell forge was started from) must not be in it.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from forge.tools import agent_env, language_hooks, safe_shell
from forge.tools.agent_env import agent_environment


def _env(**extra):
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/forge",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "C",
        "JAVA_HOME": "/jvm/21",
        "GRADLE_OPTS": "-Xmx2g",
        "HTTPS_PROXY": "http://proxy.internal:3128",
        "DATABASE_URL": "postgresql://forge:hunter2@db/forge",
        "OPENAI_API_KEY": "sk-live",
        "GITHUB_TOKEN": "ghp_live",
        "AWS_SECRET_ACCESS_KEY": "aws",
        "MY_SERVICE_PASSWORD": "pw",
        "SOMETHING_ELSE": "x",
        **extra,
    }


def test_a_toolchain_gets_what_it_needs_and_nothing_else():
    env = agent_environment(_env(), passthrough=())
    assert env == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/forge",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "C",
        "JAVA_HOME": "/jvm/21",
        "GRADLE_OPTS": "-Xmx2g",
        "HTTPS_PROXY": "http://proxy.internal:3128",
    }


@pytest.mark.parametrize(
    "name", ["NODE_AUTH_TOKEN", "NPM_CONFIG__AUTH", "GRADLE_PASSWORD", "ANDROID_KEYSTORE_PASSWORD", "XDG_SESSION_ID"]
)
def test_a_credential_inside_an_allowed_family_is_still_refused(name):
    assert name not in agent_environment({"PATH": "/bin", name: "v"}, passthrough=())


def test_a_listed_name_is_forwarded_even_though_it_looks_like_a_secret():
    env = agent_environment(_env(NPM_TOKEN="npm_live"), passthrough=("NPM_TOKEN", "SOMETHING_ELSE"))
    assert env["NPM_TOKEN"] == "npm_live"
    assert env["SOMETHING_ELSE"] == "x"
    assert "GITHUB_TOKEN" not in env


def test_a_trailing_star_lists_a_prefix():
    env = agent_environment(_env(ARTIFACTORY_USER="u", ARTIFACTORY_TOKEN="t"), passthrough=("ARTIFACTORY_*",))
    assert env["ARTIFACTORY_USER"] == "u" and env["ARTIFACTORY_TOKEN"] == "t"


def test_a_lone_star_lists_nothing():
    assert "SOMETHING_ELSE" not in agent_environment(_env(), passthrough=("*",))


def test_the_database_url_is_never_forwarded_even_when_listed():
    assert "DATABASE_URL" not in agent_environment(_env(), passthrough=("DATABASE_URL",))
    assert "DATABASE_URL" not in agent_environment(_env(), passthrough=("DATA*",))


def test_what_the_caller_computed_is_applied_last_and_unfiltered():
    env = agent_environment(_env(), passthrough=(), extra={"JAVA_HOME": "/jvm/17", "MY_TOKEN": "kept"})
    assert env["JAVA_HOME"] == "/jvm/17"
    assert env["MY_TOKEN"] == "kept"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, ()),
        ("", ()),
        ("  ,, ", ()),
        ("A, B  C\nD", ("A", "B", "C", "D")),
        ("A,A,B", ("A", "B")),
    ],
)
def test_parse_names(raw, expected):
    assert agent_env.parse_names(raw) == expected


def test_the_setting_is_unreadable_without_a_database_and_that_is_not_an_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert agent_env.configured_passthrough() == ()


def test_the_name_pattern_and_its_exceptions():
    assert agent_env.is_secret_name("GITHUB_TOKEN")
    assert agent_env.is_secret_name("db_password")
    assert not agent_env.is_secret_name("PATH")
    assert "SSH_AUTH_SOCK" in agent_env.SECRET_NAME_NOT_A_SECRET


def test_a_command_in_a_worktree_does_not_see_the_daemons_credentials(tmp_path, monkeypatch):
    """End to end through both runners the agent's code goes through: the
    developer step's allowlisted shell and the gate's shared runner."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://forge:hunter2@db/forge")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_live")
    script = tmp_path / "show-env.sh"
    script.write_text('#!/bin/sh\necho "db=${DATABASE_URL:-unset} gh=${GITHUB_TOKEN:-unset} path=${PATH:+set}"\n')
    script.chmod(0o755)

    via_gate = language_hooks.run([str(script)], cwd=str(tmp_path), timeout=10)["stdout"].strip()
    assert via_gate == "db=unset gh=unset path=set"

    monkeypatch.setattr(safe_shell, "is_safe_command", lambda command: True)
    via_shell = safe_shell.run_safe_command(str(tmp_path), str(script)).stdout.strip()
    assert via_shell == "db=unset gh=unset path=set"


def test_the_host_environment_really_has_the_variable_the_test_hides(tmp_path, monkeypatch):
    """Guards the test above against passing because the variable never
    reached this process in the first place."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_live")
    out = subprocess.run(["sh", "-c", 'echo "${GITHUB_TOKEN:-unset}"'], capture_output=True, text=True, cwd=tmp_path)
    assert out.stdout.strip() == "ghp_live"
    assert os.environ["GITHUB_TOKEN"] == "ghp_live"
