"""What MoonLan is and what it runs on, for "About" (v0.8.1).

The build is the commit this copy was started from, read out of .git
without running git: an operator who pulls main between tags has to be
able to say which code is running, and "0.8.1" does not say it. The
components are the versions installed, not the ones requirements.txt
allows — the first thing asked in a bug report. Their licences are a
table here: package metadata gives a licence in five different ways or
not at all, and tests/test_about.py holds the table to requirements.txt.
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (shown as, distribution, licence as SPDX says it)
COMPONENTS = (
    ("FastAPI", "fastapi", "MIT"),
    ("Starlette", "starlette", "BSD-3-Clause"),
    ("Uvicorn", "uvicorn", "BSD-3-Clause"),
    ("pysnmp", "pysnmp", "BSD-2-Clause"),
    ("PyYAML", "PyYAML", "MIT"),
    # BSD-2-Clause, with a few files under the other two
    ("fido2", "fido2", "BSD-2-Clause, Apache-2.0, MPL-2.0"),
    ("cryptography", "cryptography", "Apache-2.0 OR BSD-3-Clause"),
)
# in the browser, the version web/index.html pins
BROWSER_COMPONENTS = (
    ("vis-network", "9.1.9", "Apache-2.0 OR MIT"),
)

# what "HTTPS" is, by https.transport_mode
HTTPS_MODES = {"tls": "own", "proxy": "proxy", "http": "off"}

_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def build_hash(root: Path = ROOT) -> str | None:
    """The short hash of the commit checked out in `root`; without a
    git clone, MOONLAN_BUILD — what a Docker image was built from
    (v0.8.3), for an image has no .git — or None. `.git` is a folder,
    or a file naming one ("gitdir: …") in a worktree or a submodule; a
    branch is a file under refs/ or a line of packed-refs."""
    found = _git_hash(root)
    if found:
        return found
    built = os.environ.get("MOONLAN_BUILD", "").strip()
    return built[:12] if re.fullmatch(r"[0-9a-f]{7,40}", built) else None


def _git_hash(root: Path) -> str | None:
    try:
        git = root / ".git"
        if git.is_file():
            text = git.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return None
            git = (root / text[len("gitdir:"):].strip()).resolve()
        if not git.is_dir():
            return None
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if _SHA.fullmatch(head):  # detached: a commit, not a branch
            return head[:7]
        if not head.startswith("ref:"):
            return None
        ref = head[len("ref:"):].strip()
        # a worktree has a HEAD of its own; the branches are the clone's
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for folder in (git, common):
            path = folder / ref
            if path.is_file():
                sha = path.read_text(encoding="utf-8").strip()
                return sha[:7] if _SHA.fullmatch(sha) else None
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref and _SHA.fullmatch(parts[0]):
                    return parts[0][:7]
    except (OSError, UnicodeDecodeError):
        return None
    return None


def components() -> list[dict]:
    """What MoonLan runs on, with the versions installed; `version` is
    None for a package that is not (fido2 is optional)."""
    rows = []
    for name, dist, licence in COMPONENTS:
        try:
            version = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            version = None
        rows.append({"name": name, "version": version, "license": licence, "where": "server"})
    for name, version, licence in BROWSER_COMPONENTS:
        rows.append({"name": name, "version": version, "license": licence, "where": "browser"})
    return rows


def os_name() -> str:
    """The system and its distribution: "Linux, Debian GNU/Linux 13
    (trixie)". The distribution, not the kernel's build."""
    system = platform.system() or "?"
    try:
        release = platform.freedesktop_os_release()
        distro = release.get("PRETTY_NAME") or release.get("NAME") or ""
    except OSError:
        distro = ""
    return f"{system}, {distro}" if distro else f"{system} {platform.release()}".strip()


def server(started: float, sign_in_on: bool, transport: str) -> dict:
    """The server itself — for an administrator only: a kernel and its
    distribution are something to look up exploits for, not something
    the map's readers need."""
    return {
        "python": platform.python_version(),
        "os": os_name(),
        "started_at": started,
        "uptime": max(0, int(time.time() - started)),
        "sign_in": sign_in_on,
        "https": HTTPS_MODES.get(transport, "off"),
    }


def copyright_line(text: str | None) -> str | None:
    """Who holds the copyright, exactly as LICENSE says it: "2026
    neonight-d" out of "Copyright (c) 2026 neonight-d"."""
    match = re.search(r"^Copyright \(c\) (.+)$", text or "", re.MULTILINE)
    return match.group(1).strip() if match else None


def license_text(root: Path = ROOT) -> str | None:
    """LICENSE, which MIT asks to go with every copy; None without it."""
    try:
        return (root / "LICENSE").read_text(encoding="utf-8")
    except OSError:
        return None
