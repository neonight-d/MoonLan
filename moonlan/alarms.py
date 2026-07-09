"""Stateful alarm engine.

Consumes the results of the ping, scan and counters cycles, keeps the
consecutive-failure state in memory and the alarms themselves in
SQLite. Every raise/clear is mirrored into the journal (alarm_raised /
alarm_cleared) so the journal stays the full chronicle, and handed to
the notifier.

Rules:
- host_down (warning): a host with an IP misses 3 consecutive pings;
  cleared by the first successful ping. The journal's own host_down /
  host_up events (immediate, in db.update_ping) are left as is — the
  alarm is the debounced version of the same signal.
- switch_down (critical): a configured switch fails 2 consecutive SNMP
  polls; cleared by a successful poll.
- port_errors (warning): (errors+discards)/min above the threshold for
  2 consecutive counter cycles; cleared after 2 cycles below.
- port_util (warning): port load above the threshold percent of the
  link speed (of the total speed for a LAG) for 2 consecutive cycles;
  cleared after 2 cycles below.
- new_mac (info): instant auto-cleared alarm for every new MAC after
  the initial inventory scan.
- port_hosts_down (critical): >= thresholds.mass_down_hosts previously
  answering hosts on one switch port went silent within a single ping
  cycle — one alarm per port instead of a burst of host_down; cleared
  when at least half of the affected hosts answer again. Independent
  of the monitored flag: a mass outage is an infrastructure problem.
- lag_degraded (warning): fewer active LAG members than the total for
  2 consecutive counter cycles; cleared as soon as all members are up.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import Thresholds
from .db import Database
from .notify import Notifier

log = logging.getLogger(__name__)

SEVERITIES = {
    "host_down": "warning",
    "switch_down": "critical",
    "port_errors": "warning",
    "port_util": "warning",
    "new_mac": "info",
    "port_hosts_down": "critical",
    "lag_degraded": "warning",
}

HOST_DOWN_AFTER = 3    # consecutive failed pings
SWITCH_DOWN_AFTER = 2  # consecutive failed SNMP polls
PORT_CYCLES = 2        # consecutive counter cycles over/under the threshold


class AlarmEngine:
    def __init__(self, db: Database, notifier: Notifier, thresholds: Thresholds):
        self._db = db
        self._notifier = notifier
        self._thresholds = thresholds
        self._active: set[tuple[str, str]] = set()  # (type, subject)
        self._ping_fails: dict[str, int] = {}       # mac -> consecutive misses
        self._snmp_fails: dict[str, int] = {}       # ip -> consecutive misses
        self._over: dict[tuple[str, str], int] = {}   # port rule hysteresis
        self._under: dict[tuple[str, str], int] = {}
        self._last_ping: dict[str, bool] = {}       # previous cycle results
        # port_hosts_down: subject -> affected MACs (for the clear rule)
        self._mass_sets: dict[str, set[str]] = {}
        self._lag_over: dict[str, int] = {}          # subject -> degraded cycles

    async def load(self) -> None:
        """Restores the active set from the DB after a restart."""
        for row in await asyncio.to_thread(self._db.alarms, True, 1000):
            self._active.add((row["type"], row["subject"]))
        if self._active:
            log.info("Restored %d active alarms from the DB", len(self._active))

    # ---------- inputs ----------

    async def on_ping(
        self, results: dict[str, bool], meta: dict[str, dict]
    ) -> None:
        """One ping cycle: mac -> replied; meta = DB rows for labels.

        host_down is raised only for hosts with meta["monitored"] set —
        the journal keeps recording host_up/host_down for everyone
        (that happens in db.update_ping, not here).
        """
        for mac, up in results.items():
            row = meta.get(mac, {})
            label = row.get("name") or row.get("ip") or mac
            if up:
                self._ping_fails.pop(mac, None)
                await self._clear("host_down", mac, f"{label} answers ping again")
            else:
                misses = self._ping_fails.get(mac, 0) + 1
                self._ping_fails[mac] = misses
                if misses >= HOST_DOWN_AFTER and row.get("monitored"):
                    await self._raise(
                        "host_down", mac,
                        f"{label} missed {misses} pings in a row",
                    )
        await self._mass_down(results, meta)
        self._last_ping.update(results)

    async def _mass_down(
        self, results: dict[str, bool], meta: dict[str, dict]
    ) -> None:
        """port_hosts_down: many hosts of one port went silent at once."""
        threshold = self._thresholds.mass_down_hosts
        if threshold <= 0:
            return

        def label(mac: str) -> str:
            row = meta.get(mac, {})
            return row.get("name") or row.get("ip") or mac

        # Hosts that answered on the previous cycle and are silent now,
        # grouped by the switch port they live on
        newly_down: dict[str, list[str]] = {}
        for mac, up in results.items():
            if up or not self._last_ping.get(mac):
                continue
            row = meta.get(mac, {})
            if row.get("switch_ip") and row.get("port"):
                subject = f"{row['switch_ip']}:{row['port']}"
                newly_down.setdefault(subject, []).append(mac)
        for subject, macs in newly_down.items():
            if len(macs) < threshold:
                continue
            self._mass_sets.setdefault(subject, set()).update(macs)
            names = ", ".join(label(m) for m in macs[:5])
            await self._raise(
                "port_hosts_down", subject,
                f"{len(macs)} hosts went silent at once: {names}",
            )

        # An active alarm that predates a restart has no affected set:
        # rebuild it from the hosts currently silent on that port
        for alarm_type, subject in list(self._active):
            if alarm_type != "port_hosts_down" or subject in self._mass_sets:
                continue
            ip, _, port = subject.partition(":")
            self._mass_sets[subject] = {
                mac for mac, row in meta.items()
                if row.get("switch_ip") == ip and row.get("port") == port
                and not results.get(mac, True)
            }

        # Clear when at least half of the affected hosts answer again
        for subject, macs in list(self._mass_sets.items()):
            if ("port_hosts_down", subject) not in self._active:
                del self._mass_sets[subject]
                continue
            if not macs:
                continue
            answering = sum(1 for m in macs if results.get(m))
            if answering * 2 >= len(macs):
                del self._mass_sets[subject]
                await self._clear(
                    "port_hosts_down", subject,
                    f"{answering} of {len(macs)} hosts answer again",
                )

    async def on_scan(
        self, reachable: dict[str, bool], names: dict[str, str]
    ) -> None:
        """One scan cycle: switch ip -> answered SNMP."""
        for ip, ok in reachable.items():
            if ok:
                self._snmp_fails.pop(ip, None)
                await self._clear("switch_down", ip, "SNMP polling restored")
            else:
                misses = self._snmp_fails.get(ip, 0) + 1
                self._snmp_fails[ip] = misses
                if misses >= SWITCH_DOWN_AFTER:
                    await self._raise(
                        "switch_down", ip,
                        f"{names.get(ip, ip)} missed {misses} SNMP polls in a row",
                    )

    async def on_counters(self, ip: str, metrics: list[dict]) -> None:
        """One counters cycle. Each metric describes one logical port:
        port (name), speed_mbps (0 = skip the utilization rule), in/out
        Mbit/s, errors_per_min, discards_per_min.
        """
        for m in metrics:
            subject = f"{ip}:{m['port']}"
            total = m["errors_per_min"] + m["discards_per_min"]
            await self._hysteresis(
                "port_errors", subject,
                total > self._thresholds.errors_per_minute,
                f"{total:.1f} errors+discards per minute",
            )
            speed = m["speed_mbps"]
            if speed:
                util = max(m["in_mbps"], m["out_mbps"]) / speed * 100
                await self._hysteresis(
                    "port_util", subject,
                    util > self._thresholds.port_utilization_percent,
                    f"utilization {util:.0f}% of {speed} Mbit/s",
                )

    async def on_new_macs(self, new_macs: list[str], details: dict[str, str]) -> None:
        for mac in new_macs:
            await self._raise("new_mac", mac, details.get(mac, ""), auto_clear=True)

    # ---------- transitions ----------

    async def _hysteresis(
        self, alarm_type: str, subject: str, over: bool, message: str
    ) -> None:
        key = (alarm_type, subject)
        if over:
            self._over[key] = self._over.get(key, 0) + 1
            self._under[key] = 0
        else:
            self._under[key] = self._under.get(key, 0) + 1
            self._over[key] = 0
        if self._over.get(key, 0) >= PORT_CYCLES:
            await self._raise(alarm_type, subject, message)
        elif key in self._active and self._under.get(key, 0) >= PORT_CYCLES:
            await self._clear(alarm_type, subject, "back below the threshold")

    async def _raise(
        self, alarm_type: str, subject: str, message: str, auto_clear: bool = False
    ) -> None:
        if not auto_clear and (alarm_type, subject) in self._active:
            return
        ts = time.time()
        severity = SEVERITIES[alarm_type]
        inserted = await asyncio.to_thread(
            self._db.raise_alarm, alarm_type, subject, severity, message, ts,
            auto_clear,
        )
        if not inserted:
            # someone already raised it (e.g. before a restart)
            self._active.add((alarm_type, subject))
            return
        if not auto_clear:
            self._active.add((alarm_type, subject))
        await asyncio.to_thread(
            self._db.add_event, ts, "alarm_raised", subject,
            f"{severity} {alarm_type}: {message}",
        )
        log.warning("Alarm raised: %s %s — %s", alarm_type, subject, message)
        await self._notifier.notify(alarm_type, subject, severity, message)

    async def _clear(self, alarm_type: str, subject: str, message: str) -> None:
        if (alarm_type, subject) not in self._active:
            return
        ts = time.time()
        self._active.discard((alarm_type, subject))
        cleared = await asyncio.to_thread(
            self._db.clear_alarm, alarm_type, subject, ts
        )
        if not cleared:
            return
        severity = SEVERITIES[alarm_type]
        await asyncio.to_thread(
            self._db.add_event, ts, "alarm_cleared", subject,
            f"{severity} {alarm_type}: {message}",
        )
        log.info("Alarm cleared: %s %s — %s", alarm_type, subject, message)
        await self._notifier.notify(
            alarm_type, subject, severity, message, cleared=True
        )
