"""Who made a device: the IEEE registry of MAC address blocks (v0.7.8).

The first three bytes of a MAC address are an organisation's
identifier, handed out by IEEE, and the register of those hand-outs is
public. It comes in four files, because blocks come in three sizes:

  MA-L  24-bit prefixes (6 hex digits) — most of the register;
  MA-M  28-bit prefixes (7 hex digits);
  MA-S  36-bit prefixes (9 hex digits);
  IAB   36-bit prefixes, the older register MA-S replaced.

MA-M, MA-S and IAB blocks are slices of somebody's MA-L block, and that
MA-L row reads "IEEE Registration Authority": a device from such a block
is told apart only by its longer prefix. So all four are needed, and
the longest match wins.

The register is not kept in the repository: it is several megabytes
and changes every week. `python -m moonlan.oui update` fetches it into
a folder next to the database (`oui.path` to put it elsewhere); a
machine without the internet gets the same four files copied by hand.
The service reads them at startup and runs without them.

Standard library only: csv and urllib.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

log = logging.getLogger("moonlan")


@dataclass(frozen=True)
class Source:
    registry: str      # MA-L, MA-M, MA-S, IAB
    file: str          # its name in the folder — the name IEEE serves it under
    url: str
    digits: int        # hex digits of a prefix in it
    fewest: int        # rows a complete file has at least


SOURCES = (
    Source("MA-L", "oui.csv", "https://standards-oui.ieee.org/oui/oui.csv",
           6, 20000),
    Source("MA-M", "mam.csv", "https://standards-oui.ieee.org/oui28/mam.csv",
           7, 2000),
    Source("MA-S", "oui36.csv",
           "https://standards-oui.ieee.org/oui36/oui36.csv", 9, 2000),
    Source("IAB", "iab.csv", "https://standards-oui.ieee.org/iab/iab.csv",
           9, 2000),
)

# A register this old still names nearly every device, but the newest
# makers are missing from it: worth a line at startup, not an alarm
STALE_DAYS = 180

# IEEE answers 418 to Python's own User-Agent; a client that says what
# it is gets the files
USER_AGENT = (f"MoonLan/{__version__} (network map; "
              f"+https://github.com/neonight-d/MoonLan)")
FETCH_TIMEOUT = 120

UPDATE_COMMAND = "python -m moonlan.oui update"


class RegistryError(Exception):
    """A file that is not the register it should be."""


def parse(data: bytes, source: Source) -> dict[str, str]:
    """prefix (upper-case hex) -> organisation name, from one CSV file.
    Raises RegistryError when it is not that register: no header, no
    rows, or prefixes of the wrong length."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RegistryError(f"not UTF-8 text ({exc.reason})") from None
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    if "Assignment" not in fields or "Organization Name" not in fields:
        raise RegistryError(
            "no 'Assignment' and 'Organization Name' columns — not an IEEE "
            "register file (a web page saved instead of the file?)"
        )
    rows: dict[str, str] = {}
    wrong = 0
    try:
        for row in reader:
            prefix = (row.get("Assignment") or "").strip().upper()
            name = (row.get("Organization Name") or "").strip()
            if len(prefix) != source.digits or not _is_hex(prefix) or not name:
                wrong += 1
                continue
            # the name exactly as the register has it: that is what a
            # search on the internet will find
            rows[prefix] = name
    except csv.Error as exc:
        raise RegistryError(f"broken CSV ({exc})") from None
    if not rows:
        raise RegistryError(
            f"no {source.digits}-digit prefixes in it — not the "
            f"{source.registry} register"
        )
    if wrong > len(rows):
        raise RegistryError(
            f"{wrong} rows are not {source.digits}-digit prefixes against "
            f"{len(rows)} that are — not the {source.registry} register"
        )
    return rows


def _is_hex(text: str) -> bool:
    try:
        int(text, 16)
    except ValueError:
        return False
    return True


@dataclass
class Loaded:
    """One register file as read at startup."""

    source: Source
    path: Path
    rows: int
    modified: float


@dataclass
class Registry:
    """The register, as the service and diag read it. Prefixes by length,
    longest looked up first."""

    folder: Path | None = None
    tables: dict[int, dict[str, tuple[str, str]]] = field(
        default_factory=lambda: {9: {}, 7: {}, 6: {}}
    )
    files: list[Loaded] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def loaded(self) -> bool:
        return any(self.tables.values())

    def oldest(self) -> float | None:
        """When the oldest of the files was written, or None."""
        return min((f.modified for f in self.files), default=None)

    @classmethod
    def load(cls, folder: Path | str) -> "Registry":
        """Reads whichever of the four files are in `folder`. A missing
        or broken one is noted in `problems`; the rest are used."""
        registry = cls(folder=Path(folder))
        for source in SOURCES:
            path = registry.folder / source.file
            if not path.exists():
                continue
            try:
                rows = parse(path.read_bytes(), source)
            except (OSError, RegistryError) as exc:
                registry.problems.append(f"{path}: {exc}")
                continue
            table = registry.tables[source.digits]
            for prefix, name in rows.items():
                table[prefix] = (name, source.registry)
            registry.files.append(
                Loaded(source, path, len(rows), path.stat().st_mtime)
            )
        return registry


def log_state(registry: Registry) -> None:
    """One line at startup: what the register is, or how to get it."""
    for problem in registry.problems:
        log.warning("Vendors: %s — that file is not used", problem)
    if not registry.loaded:
        log.info(
            "Vendors: no IEEE register in %s — devices show no maker. "
            "%s fetches it (or copy the four CSV files there by hand, see "
            "docs/OPERATIONS.md)", registry.folder, UPDATE_COMMAND,
        )
        return
    counts = ", ".join(f"{f.source.registry} {f.rows}" for f in registry.files)
    oldest = registry.oldest()
    age = (time.time() - oldest) / 86400 if oldest else 0
    log.info(
        "Vendors: IEEE register in %s — %s, files from %s",
        registry.folder, counts,
        time.strftime("%Y-%m-%d", time.localtime(oldest)),
    )
    if age > STALE_DAYS:
        log.warning(
            "Vendors: the IEEE register is %d days old — the newest makers "
            "are missing from it; %s", age, UPDATE_COMMAND,
        )


# ---------- python -m moonlan.oui update ----------

def fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv, */*"}
    )
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as answer:
        return answer.read()


def update(folder: Path, get=fetch, out=print) -> int:
    """Fetches the four files, checks every one, and only then puts them
    in place of the old ones: a download cut short, a page of HTML
    instead of a CSV or a file of a hundred rows leaves the register the
    service has untouched. 0 when updated, 1 when not."""
    folder.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path, int]] = []
    failed: list[str] = []
    refused = False
    for source in SOURCES:
        out(f"  {source.registry:<5} {source.url} …", end=" ", flush=True)
        try:
            data = get(source.url)
        except urllib.error.HTTPError as exc:
            refused |= exc.code in (403, 418, 429)
            out(f"refused: HTTP {exc.code}")
            failed.append(source.registry)
            continue
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            out(f"failed: {reason}")
            failed.append(source.registry)
            continue
        try:
            rows = parse(data, source)
        except RegistryError as exc:
            out(f"not usable: {exc}")
            failed.append(source.registry)
            continue
        if len(rows) < source.fewest:
            out(f"not usable: {len(rows)} rows, a complete file has over "
                f"{source.fewest}")
            failed.append(source.registry)
            continue
        part = folder / (source.file + ".part")
        part.write_bytes(data)
        staged.append((part, folder / source.file, len(rows)))
        out(f"{len(rows)} rows")
    if failed:
        for part, _, _ in staged:
            part.unlink(missing_ok=True)
        out(f"\nNot updated ({', '.join(failed)}): the register in {folder} "
            f"is left as it was.")
        if refused:
            out("IEEE refuses some clients it takes for robots.")
        out(_by_hand(folder))
        return 1
    for part, final, _ in staged:
        os.replace(part, final)
    out(f"\nUpdated: {folder}. MoonLan reads the register at startup — "
        f"restart it to use this one.")
    return 0


def _by_hand(folder: Path) -> str:
    lines = [f"To do it by hand, download these files in a browser and put "
             f"them into {folder}:"]
    for source in SOURCES:
        lines.append(f"  {source.url}  ->  {source.file}")
    lines.append("then restart MoonLan; python -m moonlan.diag --config shows "
                 "what it reads.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m moonlan.oui",
        description="The IEEE register of MAC address blocks, which names "
                    "the maker of a device.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("update", help="download the register (four CSV files) "
                                  "into the folder oui.path names")
    parser.parse_args(argv)
    from .config import load_config
    folder = load_config().oui_folder()
    print(f"IEEE register: {folder}")
    return update(folder)


if __name__ == "__main__":
    sys.exit(main())
