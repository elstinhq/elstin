"""Which JDK a Java project is built with.

Maven and Gradle run on whatever `JAVA_HOME` (else `java` on PATH) the
process inherits — on a developer's machine, the newest JDK installed. A
project states the version it targets, and a build tool on a much newer
JDK than that can fail before compiling a single file: one story's
`mono-apiservice` targets Java 21 with Lombok 1.18.36, elstin ran it on JDK 26,
and javac crashed (`TypeTag :: UNKNOWN`) three builds in a row while the
developer step "fixed" working code.

So every command elstin runs in a Java worktree — the build check's and the
developer step's own — gets `JAVA_HOME` (and PATH) for an installed JDK of
the version the project states (`java_env`). No stated version, or none
installed, leaves the environment as it is; `describe` says which, for the
message a failed build carries.
"""
from __future__ import annotations

import glob
import os
import re
import threading
import time

# Where JDKs are installed: macOS (system, per-user as IntelliJ downloads
# them, Homebrew), Linux packages and images, SDKMAN.
SEARCH_GLOBS = (
    "/Library/Java/JavaVirtualMachines/*/Contents/Home",
    "~/Library/Java/JavaVirtualMachines/*/Contents/Home",
    "/opt/homebrew/opt/openjdk*/libexec/openjdk.jdk/Contents/Home",
    "/usr/local/opt/openjdk*/libexec/openjdk.jdk/Contents/Home",
    "/usr/lib/jvm/*",
    "/opt/java/*",
    "~/.sdkman/candidates/java/*",
    "~/.jdks/*",
)
# A scan is reused this long: one per run start, not one per command.
SCAN_SECONDS = 60

_RELEASE_VERSION_RE = re.compile(r'^JAVA_VERSION="([^"]+)"', re.MULTILINE)
# Most specific first: an explicit release beats java.version, which beats source.
_POM_VERSION_RES = tuple(re.compile(p) for p in (
    r"<maven\.compiler\.release>\s*([\d.]+)\s*</maven\.compiler\.release>",
    r"<release>\s*([\d.]+)\s*</release>",
    r"<java\.version>\s*([\d.]+)\s*</java\.version>",
    r"<maven\.compiler\.source>\s*([\d.]+)\s*</maven\.compiler\.source>",
))
_GRADLE_VERSION_RES = tuple(re.compile(p) for p in (
    r"JavaLanguageVersion\.of\(\s*(\d+)\s*\)",
    r"jvmToolchain\(\s*(\d+)\s*\)",
    r"JavaVersion\.VERSION_([\d_]+)",
    r"sourceCompatibility\s*=\s*['\"]?([\d.]+)",
))

_scan_lock = threading.Lock()
_scan: dict = {"at": 0.0, "homes": {}}


def major_of(version: str) -> int | None:
    """21 for "21", "21.0.12" or "21.0.12+7"; 8 for "1.8", "1.8.0_392" or "1_8"."""
    parts = re.split(r"[._]", version.strip())
    numbers = [int(match.group()) for part in parts if (match := re.match(r"\d+", part))]
    if not numbers:
        return None
    return numbers[1] if numbers[0] == 1 and len(numbers) > 1 else numbers[0]


def jdk_major(home: str) -> int | None:
    """The major version of the JDK at `home`, from its `release` file."""
    try:
        with open(os.path.join(home, "release"), encoding="utf-8") as handle:
            match = _RELEASE_VERSION_RE.search(handle.read())
    except OSError:
        return None
    return major_of(match.group(1)) if match else None


def required_java_version(project_path: str) -> int | None:
    """The Java version a project states: its pom.xml, its Gradle build, or
    a `.java-version` file. None when it states none."""
    for name, patterns in (("pom.xml", _POM_VERSION_RES), ("build.gradle", _GRADLE_VERSION_RES), ("build.gradle.kts", _GRADLE_VERSION_RES)):
        text = _read(os.path.join(project_path, name))
        for pattern in patterns if text else ():
            match = pattern.search(text)
            if match and (major := major_of(match.group(1))):
                return major
    text = _read(os.path.join(project_path, ".java-version"))
    return major_of(text) if text else None


def installed_jdks() -> dict[int, list[str]]:
    """{major: [JDK home, ...]} for every JDK in SEARCH_GLOBS and JAVA_HOME,
    newest patch first."""
    with _scan_lock:
        if time.monotonic() - _scan["at"] < SCAN_SECONDS:
            return _scan["homes"]
        found: dict[int, dict[str, str]] = {}
        candidates = [path for pattern in SEARCH_GLOBS for path in glob.glob(os.path.expanduser(pattern))]
        if os.environ.get("JAVA_HOME"):
            candidates.append(os.environ["JAVA_HOME"])
        for home in candidates:
            home = os.path.realpath(home)
            major = jdk_major(home)
            if major is not None and os.path.isfile(os.path.join(home, "bin", "java")):
                found.setdefault(major, {})[home] = _read(os.path.join(home, "release"))
        _scan["homes"] = {
            major: sorted(homes, key=lambda h: _version_key(homes[h]), reverse=True) for major, homes in found.items()
        }
        _scan["at"] = time.monotonic()
        return _scan["homes"]


def find_jdk(major: int) -> str | None:
    homes = installed_jdks().get(major)
    return homes[0] if homes else None


def java_env(project_path: str) -> dict[str, str]:
    """JAVA_HOME and PATH for the JDK `project_path` states, or {} to leave
    the environment as it is."""
    major = required_java_version(project_path)
    home = find_jdk(major) if major is not None else None
    if home is None:
        return {}
    return {"JAVA_HOME": home, "PATH": os.path.join(home, "bin") + os.pathsep + os.environ.get("PATH", "")}


def describe(project_path: str) -> str:
    """Which JDK elstin builds `project_path` with, in a sentence."""
    major = required_java_version(project_path)
    if major is None:
        return "The project states no Java version, so elstin builds it with the default JDK."
    home = find_jdk(major)
    if home is None:
        installed = ", ".join(str(m) for m in sorted(installed_jdks())) or "none"
        return (
            f"The project targets Java {major}, but no JDK {major} is installed where elstin looks "
            f"(installed: {installed}) — install one, then Retry."
        )
    return f"The project targets Java {major}; elstin builds it with {home}."


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _version_key(release_text: str) -> tuple[int, ...]:
    match = _RELEASE_VERSION_RE.search(release_text or "")
    return tuple(int(n) for n in re.findall(r"\d+", match.group(1))) if match else ()
