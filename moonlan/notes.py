"""Names and notes on the map's nodes (v0.8.0).

A switch without SNMP stands in a classroom, and on the map every one
of them was called the same. Nothing MoonLan polls knows where it is;
the person looking at the map does. A node gets a **name** — one line,
on the map, above what MoonLan calls it — and a **note**, free text in
its card. Both are a person's word: MoonLan shows them and never
changes them, and never deletes one by itself.

They are kept by the node id the layout uses (`sw:<ip>`, `host:<mac>`,
`pseudo:<switch>:<port>` …). An offline group has none: it is whatever
is missing from the tables right now, and naming it would be naming
"somewhere they were".
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import oui

LABEL_MAX = 40
NOTE_MAX = 2000
# A node has to be gone for this many scans in a row before its note
# counts as "without a node": one failed poll or a restart is not a
# cable moved (with the default ten-minute scans, half an hour)
MISSING_SCANS = 3

# the kinds of node a note may be on, by the prefix of the id
NOTED_KINDS = ("sw", "host", "bridge", "pseudo", "external", "trunk")


class NoteError(ValueError):
    """A note that cannot be written; `code` is what the page words."""

    def __init__(self, code: str, text: str):
        super().__init__(text)
        self.code = code


def note_key(node_id: str) -> str:
    """The id a note is kept under. A device off the map (`unloc:`) is
    the same device as on it, and has the same note."""
    kind, _, rest = node_id.partition(":")
    if not rest:
        raise NoteError("not_a_node", f"not a node id: {node_id!r}")
    if kind == "unloc":
        return "host:" + rest
    if kind == "offline":
        raise NoteError(
            "offline_group",
            "an offline group is whatever is missing from the switch "
            "tables right now; it has no name of its own",
        )
    if kind not in NOTED_KINDS:
        raise NoteError("not_a_node", f"not a node id: {node_id!r}")
    return node_id


def _control(text: str, allowed: str = "") -> bool:
    return any(
        unicodedata.category(ch) in ("Cc", "Cf") and ch not in allowed
        for ch in text
    )


def clean_label(label: str | None) -> str:
    """One line of at most LABEL_MAX characters, trimmed."""
    text = (label or "").strip()
    if _control(text):
        raise NoteError("label_control", "a name is one line of plain text")
    if len(text) > LABEL_MAX:
        raise NoteError("label_long", f"a name is at most {LABEL_MAX} characters")
    return text


def clean_note(note: str | None) -> str:
    """Free text with line breaks, at most NOTE_MAX characters, trimmed."""
    text = (note or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if _control(text, "\n\t"):
        raise NoteError("note_control", "a note is plain text")
    if len(text) > NOTE_MAX:
        raise NoteError("note_long", f"a note is at most {NOTE_MAX} characters")
    return text


def public(row: dict) -> dict:
    """A note as the map gets it with its node."""
    return {
        "label": row["label"] or "",
        "note": row["note"] or "",
        "source": row["source"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"] or "",
    }


def state(row: dict) -> str:
    """"present" — its node is on the map, or has been gone for less
    than MISSING_SCANS scans; "missing" — gone longer; "not_found" — an
    import whose device has never been on the map."""
    if row.get("seen_at") is None:
        return "not_found"
    if (row.get("missing_scans") or 0) >= MISSING_SCANS:
        return "missing"
    return "present"


# ---------- CSV: importing and exporting notes by MAC ----------
#
# The format is what Excel saves in a Russian Windows: `;`, cp1251.
# Accepted besides: `,` and tab, UTF-8 with or without a BOM, RFC 4180
# quotes (a quoted note may run over several lines), the header in
# English or in Russian, and a MAC in any common writing.

HEADER_NAMES = {
    "mac": "mac", "label": "label", "note": "note",
    "подпись": "label", "заметка": "note",
}
DELIMITERS = (";", ",", "\t")
# a file bigger than this is not a list of names
IMPORT_MAX_BYTES = 2 * 1024 * 1024


@dataclass
class ImportRow:
    line: int
    mac: str
    label: str | None   # None: the cell was empty — leave the name as it is
    note: str | None


def mac_key(text: str | None) -> str | None:
    """aa:bb:cc:dd:ee:ff from any common writing of a MAC, or None"""
    digits = oui.normalize(text or "")
    if digits is None:
        return None
    digits = digits.lower()
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def decode(data: bytes) -> str:
    """UTF-8 (a BOM or not); what is not UTF-8 is cp1251, which is what
    Excel in a Russian Windows saves"""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


def parse_csv(data: bytes) -> tuple[list[ImportRow], list[dict], list[dict]]:
    """(rows, errors, warnings). An error is a line that cannot be taken
    — said with its number and why; a warning is one that was taken with
    a remark. Two lines with one MAC: the last one counts."""
    text = decode(data)
    first = text.lstrip("﻿").split("\n", 1)[0]
    delimiter = max(DELIMITERS, key=first.count)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    errors: list[dict] = []
    warnings: list[dict] = []
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        return [], [{"line": 1, "error": "no_header"}], []
    fields = [HEADER_NAMES.get(cell.strip().lower()) for cell in header]
    if "mac" not in fields or "label" not in fields or (
        None in fields and any(cell.strip() for cell, f in zip(header, fields) if f is None)
    ):
        return [], [{"line": 1, "error": "bad_header"}], []
    column = {name: fields.index(name) for name in ("mac", "label", "note") if name in fields}
    by_mac: dict[str, ImportRow] = {}
    previous = reader.line_num
    while True:
        line = previous + 1
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error:
            errors.append({"line": line, "error": "bad_csv"})
            break
        previous = reader.line_num
        if not any(cell.strip() for cell in cells):
            continue
        cell = lambda name: (cells[column[name]] if name in column and column[name] < len(cells) else "")
        mac = mac_key(cell("mac"))
        if mac is None:
            errors.append({"line": line, "error": "bad_mac", "mac": cell("mac").strip()})
            continue
        try:
            label = clean_label(cell("label")) or None
            note = clean_note(cell("note")) or None
        except NoteError as exc:
            errors.append({"line": line, "error": exc.code, "mac": mac})
            continue
        if label is None and note is None:
            errors.append({"line": line, "error": "empty", "mac": mac})
            continue
        if mac in by_mac:
            warnings.append({"line": line, "error": "duplicate", "mac": mac,
                             "first": by_mac[mac].line})
            del by_mac[mac]
        by_mac[mac] = ImportRow(line, mac, label, note)
    return list(by_mac.values()), errors, warnings


def plan_import(
    rows: list[ImportRow], existing: dict[str, dict], directory: dict[str, str],
) -> dict:
    """What an import would do, without doing it. `directory` is MAC ->
    node id: a device first, then a switch by its bridge MAC, then an
    LLDP bridge by its chassis MAC; a MAC nobody has is kept on
    `host:<mac>`, waiting for the device. A note somebody wrote by hand
    is not overwritten; an earlier import is. An empty cell changes
    nothing."""
    writes, skipped = [], []
    for row in rows:
        node_id = directory.get(row.mac)
        found = node_id is not None
        node_id = node_id or "host:" + row.mac
        old = existing.get(node_id)
        if old and old["source"] == "manual":
            skipped.append({"line": row.line, "mac": row.mac, "node_id": node_id})
            continue
        writes.append({
            "line": row.line, "mac": row.mac, "node_id": node_id, "found": found,
            "label": row.label if row.label is not None else (old["label"] if old else ""),
            "note": row.note if row.note is not None else (old["note"] if old else ""),
        })
    return {"writes": writes, "skipped_manual": skipped}


def import_report(plan: dict, errors: list[dict], warnings: list[dict], dry_run: bool) -> dict:
    writes = plan["writes"]
    return {
        "dry_run": dry_run,
        "written": len(writes),
        "on_map": sum(1 for w in writes if w["found"]),
        "not_found": sum(1 for w in writes if not w["found"]),
        "skipped_manual": plan["skipped_manual"],
        "errors": errors,
        "warnings": warnings,
    }


def export_csv(notes: dict[str, dict], switch_macs: dict[str, str]) -> tuple[bytes, int]:
    """(the file, how many notes it could not hold). A note goes in by
    the MAC of its node: a device, a switch by its bridge MAC, an LLDP
    bridge whose chassis id is a MAC. A switch without SNMP, an external
    network and a group have no MAC and are left out — counted, so the
    page can say so."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(["mac", "label", "note"])
    left_out = 0
    for node_id, row in sorted(notes.items()):
        kind, _, rest = node_id.partition(":")
        mac = (mac_key(rest) if kind in ("host", "bridge")
               else mac_key(switch_macs.get(rest)) if kind == "sw" else None)
        if mac is None:
            left_out += 1
            continue
        writer.writerow([mac, row["label"] or "", row["note"] or ""])
    return ("﻿" + out.getvalue()).encode("utf-8"), left_out


# ---------- the console: python -m moonlan.notes ----------


def _service(cfg) -> tuple[str, dict]:
    """The running service's address, and the headers that let this
    tool in: the token it wrote next to its database (as diag uses),
    and an Origin, as every browser sends with a write"""
    host = cfg.listen_host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    base = f"http://{host}:{cfg.listen_port}"
    headers = {"Origin": base}
    try:
        token = Path(cfg.users_db_path() + ".console").read_text(encoding="ascii").strip()
    except OSError:
        token = ""
    if token:
        headers["X-MoonLan-Console"] = token
    return base, headers


def _call(cfg, path: str, data: bytes | None = None) -> bytes:
    base, headers = _service(cfg)
    if data is not None:
        headers["Content-Type"] = "text/csv"
    request = urllib.request.Request(base + path, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def print_report(report: dict, out=sys.stdout) -> None:
    """The same report the page shows, as lines"""
    head = "would write" if report["dry_run"] else "written"
    print(f"{head}: {report['written']} (on the map {report['on_map']}, "
          f"not found yet {report['not_found']})", file=out)
    print(f"skipped, a note written by hand is there: {len(report['skipped_manual'])}", file=out)
    for row in report["skipped_manual"]:
        print(f"  line {row['line']}: {row['mac']} -> {row['node_id']}", file=out)
    print(f"errors: {len(report['errors'])}", file=out)
    for row in report["errors"]:
        print(f"  line {row['line']}: {row['error']}"
              + (f" ({row['mac']})" if row.get("mac") else ""), file=out)
    if report["warnings"]:
        print(f"remarks: {len(report['warnings'])}", file=out)
    for row in report["warnings"]:
        print(f"  line {row['line']}: {row['error']} {row.get('mac', '')}"
              + (f", line {row['first']} not used" if row.get("first") else ""), file=out)


def main(argv: list[str] | None = None) -> int:
    from .config import load_config

    parser = argparse.ArgumentParser(
        prog="python -m moonlan.notes",
        description="Import and export names and notes by MAC, through the "
                    "running service — the same import and the same report "
                    "as on the map.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="import a CSV: mac;label;note")
    imp.add_argument("file")
    imp.add_argument("--dry-run", action="store_true",
                     help="say what would be written, write nothing")
    exp = sub.add_parser("export", help="export the notes that have a MAC")
    exp.add_argument("file")
    args = parser.parse_args(argv)
    cfg = load_config()
    try:
        if args.command == "import":
            data = Path(args.file).read_bytes()
            answer = _call(cfg, "/api/notes/import" + ("?dry_run=1" if args.dry_run else ""), data)
            print_report(json.loads(answer))
        else:
            Path(args.file).write_bytes(_call(cfg, "/api/notes/export"))
            summary = json.loads(_call(cfg, "/api/notes/export?preview=1"))
            print(f"exported: {summary['exported']} note(s) to {args.file}")
            if summary["left_out"]:
                print(f"not exported: {summary['left_out']} note(s) of nodes without a MAC")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"the service refused: {exc.code} {body}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"could not reach the service ({exc}). Notes are imported and "
              "exported through the running MoonLan: start it first.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
