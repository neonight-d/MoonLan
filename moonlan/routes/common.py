"""What more than one module of routes needs (v0.8.13).

The refusal the routes answer with — a code the page translates, and
the HTTP status — and the samples of MAC copies that both the alarms
and the ports panel show. Moved out of server.py word for word.
Nothing here imports moonlan.server.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from ..runtime import rt


def _copy_samples(counts: dict[str, int]) -> list[dict]:
    """[{mac, vendor, n}] for the page, which words the conclusion"""
    return [
        {"mac": mac, "vendor": rt.vendors.lookup(mac).name or "", "n": n}
        for mac, n in counts.items()
    ]


def _refuse(error: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": error, **extra}, status_code=status)
