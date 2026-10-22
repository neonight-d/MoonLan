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

import unicodedata

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
