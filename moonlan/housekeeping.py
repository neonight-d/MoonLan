"""The cleanups, the end of the certificate, the leak watch (v0.8.12).

What the start and the daily cleanup do to the database, the check of
the certificate this process serves, and the numbers of the leak watch.
Moved out of server.py word for word, the first stage of taking it
apart (docs/ARCHITECTURE.md): what a function needs from the process it
reads from rt (runtime.py) when it is called. The loops that run these
are in lifecycle.py. Nothing here keeps state, and nothing here imports
moonlan.server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from . import corruption
from .journal import JOURNAL_GROUPS
from .runtime import rt
from .snmp_collector import is_valid_mac

log = logging.getLogger("moonlan")

# how long before the end tls_cert_expiring is raised, and how often
# that is looked at
CERT_ALARM_DAYS = 14
CERT_CHECK_SECONDS = 86400


def resource_usage() -> tuple[int, int]:
    """(open file descriptors, RSS in kB) — leak watch, Linux only."""
    fds = len(os.listdir("/proc/self/fd"))
    rss = 0
    with open("/proc/self/status", encoding="ascii", errors="replace") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
                break
    return fds, rss


RESOURCE_LOG_SECONDS = 600


# The cleanup: hosts past host_retention_days and saved positions past
# layout_keep_days. At the start, and once a day after it (v0.8.4):
# before, only a restart applied a changed retention.
CLEANUP_SECONDS = 86400


async def check_certificate_end() -> None:
    """tls_cert_expiring: raised two weeks before the end of the
    certificate this process serves, cleared at the start of a process
    serving one that is not about to end."""
    if rt.certificate is None:
        return
    left = rt.certificate.not_after - time.time()
    ends = time.strftime("%Y-%m-%d", time.localtime(rt.certificate.not_after))
    subject = rt.public_url.host if rt.public_url else rt.config.listen_tls_cert
    if left < CERT_ALARM_DAYS * 86400:
        await rt.alarm_engine.service_alarm(
            "tls_cert_expiring", subject, True,
            f"the certificate MoonLan serves expires on {ends}. Replace "
            f"{rt.config.listen_tls_cert} and restart MoonLan: the certificate "
            f"is read at startup, so a new file alone changes nothing",
        )
    else:
        await rt.alarm_engine.service_alarm(
            "tls_cert_expiring", subject, False,
            f"a certificate valid until {ends} is being served",
        )


async def drop_unpinned_positions() -> None:
    """Startup: the layout keeps what a person placed, and nothing else.

    One line either way, so the first start after the upgrade says how
    many of the old positions went — and a later one says so too if a
    page still running an older script wrote some since.
    """
    removed = await asyncio.to_thread(rt.db.drop_unpinned_positions)
    kept = sum(
        1 for pos in (await asyncio.to_thread(rt.db.layout)).values()
        if pos["pinned"]
    )
    log.info(
        "Map layout: %d pinned position(s) kept; %d position(s) "
        "nobody pinned removed — they were only where the layout engine "
        "once left a node, and went stale as soon as what it hangs off "
        "moved. Those nodes are laid out from the pinned ones on every "
        "load.", kept, removed,
    )


async def purge_invalid_macs() -> None:
    """Startup cleanup: drop records whose MAC cannot be real.

    Before the FDB rows were validated, malformed table entries were
    stored as devices; those records are still in the database and on
    the map.
    """
    rows = await asyncio.to_thread(rt.db.hosts_by_mac)
    bad = [mac for mac in rows if not is_valid_mac(mac)]
    if not bad:
        return
    removed = await asyncio.to_thread(rt.db.delete_hosts, bad)
    log.info(
        "Removed %d host records with impossible MAC addresses "
        "(multicast, reserved or malformed)", removed,
    )
    await asyncio.to_thread(
        rt.db.add_event, time.time(), "hosts_purged", "",
        f"{removed} records with impossible MAC addresses",
    )


async def purge_old_hosts() -> None:
    """Startup cleanup: drop hosts nothing has seen for retention days.

    LLDP rows follow the same retention: a neighbour that has not been
    seen since a cable was moved is history nobody asked to keep.
    """
    days = rt.config.host_retention_days
    if days <= 0:
        return
    now = time.time()
    dropped = await asyncio.to_thread(rt.db.purge_lldp, now - days * 86400)
    if dropped:
        log.info("Removed %d LLDP records older than %g days", dropped, days)
    removed = await asyncio.to_thread(rt.db.purge_old_hosts, now - days * 86400)
    if removed:
        log.info(
            "Removed %d hosts not seen for %g days", removed, days
        )
        await asyncio.to_thread(
            rt.db.add_event, now, "hosts_purged", "",
            f"{removed} hosts not seen for {days:g} days",
        )


async def purge_journal() -> None:
    """The journal past its terms (v0.8.7), at the start and once a day.

    It was never cleaned: two thousand events a day, nine in ten of them
    pings. The ping group goes after journal_ping_keep_days, the rest
    after journal_keep_days; 0 keeps everything, and a ping term of 0
    is the general one. Batches of 10 000 rows, a transaction each.
    One INFO line; the cleanup is not written to the journal itself,
    and the file is not vacuumed — SQLite reuses the pages.
    """
    days = rt.config.journal_keep_days
    ping_days = rt.config.journal_ping_keep_days or days
    now = time.time()
    ping_events = list(JOURNAL_GROUPS["ping"])
    pings = others = 0
    if ping_days > 0:
        pings, _ = await asyncio.to_thread(
            rt.db.purge_journal, ping_events, now - ping_days * 86400
        )
    if days > 0:
        others, _ = await asyncio.to_thread(
            rt.db.purge_journal, ping_events, now - days * 86400, True
        )
    rows, oldest = await asyncio.to_thread(rt.db.journal_extent)
    log.info(
        "Journal cleanup: %d ping event(s) older than %s and %d other(s) "
        "older than %s removed; %d left, the oldest from %s",
        pings, f"{ping_days:g} days" if ping_days > 0 else "ever",
        others, f"{days:g} days" if days > 0 else "ever", rows,
        time.strftime("%Y-%m-%d %H:%M", time.localtime(oldest))
        if oldest else "nothing",
    )


async def hide_confirmed_copies(vouched: set[str] | None = None) -> None:
    """Damaged copies the database confirmed before v0.8.5 held them
    back: marked, off the map from this moment, and deleted at the next
    daily cleanup. One journal entry per batch — the copies of one
    address — with the addresses in its data, not a line for each.

    At the start and after every scan. Cheap: confirmed records that
    never had an address are few. With filter_suspect_macs off nothing
    is marked: that is the choice to draw the damage. `vouched` are the
    MACs routers reported as their own: never marked.
    """
    if not rt.config.filter_suspect_macs:
        return
    rows = await asyncio.to_thread(rt.db.hosts_by_mac)
    notes = await asyncio.to_thread(rt.db.notes)
    noted = {
        node_id[len("host:"):] for node_id, note in notes.items()
        if node_id.startswith("host:")
        and (note.get("label") or note.get("note"))
    }
    # a router's own MACs are never copies (v0.8.11)
    noted |= vouched or set()
    batches = corruption.confirmed_copies(
        rows, noted, rt.config.thresholds.corruption_hamming_bits,
        rt.config.thresholds.corruption_macs_threshold,
    )
    for batch in batches:
        marked = await asyncio.to_thread(
            rt.db.mark_copies, batch["sample"], batch["macs"]
        )
        if not marked:
            continue
        log.info(
            "Damaged copies of %s taken off the map: %d address(es) on %s "
            "%s, confirmed as devices before v0.8.5 held copies back. "
            "They are deleted at the next daily cleanup.",
            batch["sample"], marked, batch["switch"],
            ", ".join(batch["ports"]),
        )
        await asyncio.to_thread(
            rt.db.add_event, time.time(), "copies_removed", batch["sample"],
            json.dumps({
                "switch": batch["switch"],
                "ports": batch["ports"],
                "n": marked,
                "macs": batch["macs"],
            }),
        )


async def purge_copies() -> None:
    """Cleanup: the records marked as damaged copies go. Not while the
    filter is off — they are what is drawn then."""
    if not rt.config.filter_suspect_macs:
        return
    removed = await asyncio.to_thread(rt.db.delete_copies)
    if removed:
        log.info("Removed %d records of damaged MAC copies", removed)
