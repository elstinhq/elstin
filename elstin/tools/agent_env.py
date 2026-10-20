"""The environment a command run on an agent's behalf sees.

Everything an agent runs in a worktree — the dev step's allowlisted shell, the
build/lint/test commands of the gate, the Dart analysis server — executes code
the agent may have just written or edited (a test file, a build script, a
package manifest's hooks). Handed the daemon's whole environment, that code
could read whatever the daemon can: `DATABASE_URL` (where the database file lives), or,
on a laptop, every token in the shell elstin was started from.

So a command gets a **minimal** environment instead: the variables a toolchain
needs to find itself and behave (`PATH`, `HOME`, locale, proxy and CA settings,
the JDK/Node/Dart/Android/... homes), and nothing that looks like a credential.
Two things widen it, both deliberate:

- a profile's own `command_env` (`LanguageProfile.command_env`) — the JDK a
  project states, say — is always applied on top, since elstin computed it;
- the `agent_env_passthrough` setting names extra host variables to forward
  (`NPM_TOKEN` for a private registry), and a name listed there is forwarded
  even when it looks like a secret — the person asked for it by name.

`DATABASE_URL` is never forwarded, whatever is listed: it is elstin's own
bootstrap secret and no build has a use for it.

`SECRET_NAME_RE` is the single answer to "does this variable name look like a
credential?" — what is refused here and what is scrubbed from persisted run
output (elstin/services/redaction.py) are decided by the same pattern, so the
two cannot drift apart.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)

SETTING_KEY = "agent_env_passthrough"

SECRET_NAME_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_KEY$|_KEY_|APIKEY|API_KEY|PRIVATE_KEY|ACCESS_KEY|_AUTH$|_AUTH_"
    r"|SESSION|COOKIE|PASSPHRASE)",
    re.IGNORECASE,
)

# Names SECRET_NAME_RE matches whose value is not a credential — a socket path,
# a pid, a desktop-session id. Redaction skips them: scrubbing an ordinary path
# out of a transcript protects nothing.
SECRET_NAME_NOT_A_SECRET: frozenset[str] = frozenset({
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "SESSION_MANAGER",
    "SESSIONNAME",
    "XDG_SESSION_ID",
    "XDG_SESSION_TYPE",
    "XDG_SESSION_CLASS",
    "XDG_SESSION_DESKTOP",
})

# Never forwarded, not even when `agent_env_passthrough` lists it.
NEVER_FORWARD: frozenset[str] = frozenset({"DATABASE_URL"})

# What a toolchain needs to find itself and behave. Anything else is left out.
_ALLOWED_NAMES: frozenset[str] = frozenset({
    # the shell and the machine
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "CI", "DEVELOPER_DIR",
    "LANG", "LANGUAGE",
    "TMPDIR", "TEMP", "TMP",
    # network: proxies and the certificate authorities behind them
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    # toolchains that are located or tuned through a bare variable
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "PYTHONUTF8", "PYTHONIOENCODING",
    "GOPATH", "GOROOT", "GOFLAGS", "GOPROXY",
    "PUB_CACHE", "FLUTTER_ROOT",
    "M2_HOME", "MAVEN_OPTS", "MAVEN_ARGS",
    "VOLTA_HOME", "PNPM_HOME", "NVM_DIR", "NVM_BIN",
})

# Families of toolchain variables (`LC_ALL`, `GRADLE_OPTS`, `NPM_CONFIG_REGISTRY`, ...).
_ALLOWED_PREFIXES: tuple[str, ...] = (
    "LC_", "XDG_", "JAVA_", "JDK_", "MAVEN_", "GRADLE_", "NODE_", "NPM_CONFIG_", "COREPACK_",
    "ANDROID_", "DART_", "FLUTTER_", "CARGO_", "RUSTUP_", "ASDF_", "SDKMAN_", "FNM_",
)


def is_secret_name(name: str) -> bool:
    """Whether `name` looks like it holds a credential."""
    return bool(SECRET_NAME_RE.search(name))


def parse_names(raw: str | None) -> tuple[str, ...]:
    """The names in a setting value: comma- or whitespace-separated, blanks
    dropped, order kept. A trailing `*` makes a name a prefix (`AWS_*`)."""
    if not raw:
        return ()
    seen: dict[str, None] = {}
    for token in re.split(r"[,\s]+", raw):
        if token:
            seen.setdefault(token)
    return tuple(seen)


def _listed(name: str, passthrough: tuple[str, ...]) -> bool:
    return any(
        name == item or (item.endswith("*") and len(item) > 1 and name.startswith(item[:-1]))
        for item in passthrough
    )


def _is_toolchain_name(name: str) -> bool:
    return name in _ALLOWED_NAMES or name.startswith(_ALLOWED_PREFIXES)


def configured_passthrough() -> tuple[str, ...]:
    """The `agent_env_passthrough` setting, or nothing when settings cannot be
    read (no database in a unit test, a database that is down): a command must
    never fail to start because elstin could not read an optional setting."""
    try:
        from elstin.config.store import get_config

        return parse_names(get_config(SETTING_KEY))
    except Exception:  # noqa: BLE001 - see docstring
        logger.debug("could not read %s; forwarding nothing extra", SETTING_KEY, exc_info=True)
        return ()


def agent_environment(
    base: Mapping[str, str] | None = None,
    *,
    passthrough: Iterable[str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """`base` (default: the process's own environment) cut down to what a
    toolchain needs, plus the `passthrough` names (default: the setting), plus
    `extra` — which the caller computed and is applied last, unfiltered."""
    source = os.environ if base is None else base
    listed = tuple(passthrough) if passthrough is not None else configured_passthrough()

    env: dict[str, str] = {}
    for name, value in source.items():
        if name in NEVER_FORWARD:
            continue
        if _listed(name, listed) or (_is_toolchain_name(name) and not is_secret_name(name)):
            env[name] = value
    if extra:
        env.update(extra)
    return env
