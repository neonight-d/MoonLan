"""The standard streams in UTF-8, whatever the locale (v0.8.9).

The log said `switch(es) \\u2014 on the map` and `RFC 3416 \\xa7` in the
systemd journal. Python opens stdout and stderr in the locale's
encoding, with `backslashreplace` on stderr: under an 8-bit locale —
`LANG=en_US`, which is ISO-8859-1 — an em dash cannot be written at all
and comes out as the text `\\u2014`, and a section sign goes out as the
single byte 0xA7, which journalctl, expecting UTF-8, shows as `\\xa7`.

PEP 538 does not help there: it coerces only the C and POSIX locales to
C.UTF-8, and PEP 540's UTF-8 mode switches itself on only for those two
as well. `en_US` is a real locale, just not a UTF-8 one, so both leave
it alone. The unit file sets no locale, and the service inherits the
system manager's.

So the service and diag reconfigure their streams to UTF-8 before the
first line goes out — with `backslashreplace` still, for whatever
cannot be encoded even then. `Environment=PYTHONUTF8=1` in the unit
does the same from outside; the code does not depend on it.
"""

from __future__ import annotations

import codecs


def _is_utf8(encoding: str | None) -> bool:
    try:
        return codecs.lookup(encoding or "").name == "utf-8"
    except LookupError:
        return False


def use_utf8(*streams) -> list[str]:
    """Each stream that is not UTF-8 is made so; the encodings it had are
    given back (for a log line or a test), empty when nothing changed."""
    changed = []
    for stream in streams:
        encoding = getattr(stream, "encoding", None)
        if _is_utf8(encoding):
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # not a text stream of ours: leave it
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):
            continue
        changed.append(str(encoding))
    return changed
