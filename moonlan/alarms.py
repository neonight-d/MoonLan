"""Stateful alarm engine.

Consumes the results of the ping, scan and counters cycles, keeps the
consecutive-failure state in memory and the alarms themselves in
SQLite. Every raise/clear is mirrored into the journal (alarm_raised /
alarm_cleared) so the journal stays the full chronicle, and handed to
the notifier.

Rules:
- host_down (warning): a monitored host with an IP misses 3
  consecutive pings; cleared by the first successful ping. The
  journal's own host_down / host_up events (immediate, in
  db.update_ping) are left as is — the alarm is the debounced version
  of the same signal. Stale hosts (drawn from the grace window rather
  than from a fresh FDB) are never alarmed on.
- switch_down (critical): a configured switch fails 2 consecutive SNMP
  polls; cleared by a successful poll.
- port_errors (warning): damaged frames (ifInErrors+ifOutErrors) above
  errors_per_minute AND, where packet counters exist, above
  error_ratio_percent of all frames, for port_alarm_cycles consecutive
  counter cycles; cleared after the same number of cycles below. A
  counter the agent did not answer for is unknown, not zero: it neither
  raises nor clears anything. The same holds for port_discards and
  port_util.
- port_discards (info): ifInDiscards+ifOutDiscards above
  discards_per_minute. Discards are usually normal filtering (VLAN
  rules, storm control, a burst filling a buffer), so they are a
  separate, quieter alarm and go to syslog only.
- port_util (warning): port load above the threshold percent of the
  link speed (of the total speed for a LAG); same hysteresis.
- new_mac (info): instant auto-cleared alarm for every new MAC after
  the initial inventory scan.
- port_hosts_down (critical): >= thresholds.mass_down_hosts previously
  answering hosts on one switch port went silent within a single ping
  cycle — one alarm per port instead of a burst of host_down; cleared
  when at least half of the affected hosts answer again. Independent
  of the monitored flag: a mass outage is an infrastructure problem.
- lag_degraded (warning): fewer active LAG members than the total for
  2 consecutive counter cycles; cleared as soon as all members are up.
- port_frame_corruption (warning, critical with port_errors): the MAC
  table of one port grew corruption_macs_threshold distorted copies of
  confirmed addresses inside the flap window; cleared when the window
  passes without a new one.
- unmanaged_bridge_detected (warning): LLDP reports a device with the
  bridge capability behind a port that is not a trunk — a switch
  nobody put on the map and nobody polls. Cleared when the neighbour
  has been gone from LLDP for the flap window. Bridges listed in
  config.known_bridges (by chassis id or management IP) never raise
  it, nor do the ones behind a port in config.uplink_ports. A bridge
  inferred rather than self-declared (cap_assumed: the switch reports
  no capabilities for anyone) raises the same alarm at severity info.
- stp_root_changed (critical) / stp_topology_change (warning) /
  stp_fragmented (warning): see moonlan/stp.py. Raised ONLY for
  switches whose spanning tree is actually operating — a switch with
  STP disabled answers every dot1dStp* object and names itself root.
- port_flapping (warning): a port changed link state
  thresholds.flaps_per_window times inside
  thresholds.flap_window_minutes; cleared by a window without a single
  transition.
"""

from __future__ import annotations

import asyncio
import logging
import time

from collections import deque

from .config import NotificationsConfig, Thresholds
from .db import Database
from .notify import Notifier

log = logging.getLogger(__name__)

SEVERITIES = {
    "host_down": "warning",
    "switch_down": "critical",
    "port_errors": "warning",
    "port_discards": "info",
    "port_util": "warning",
    "new_mac": "info",
    "port_hosts_down": "critical",
    "lag_degraded": "warning",
    "port_frame_corruption": "warning",
    "unmanaged_bridge_detected": "warning",
    "stp_root_changed": "critical",
    "stp_topology_change": "warning",
    "stp_fragmented": "warning",
    "port_flapping": "warning",
}

HOST_DOWN_AFTER = 3    # consecutive failed pings
SWITCH_DOWN_AFTER = 2  # consecutive failed SNMP polls
# A host must have answered this many consecutive pings before its
# silence counts toward a mass outage — freshly discovered and already
# flickering records are not evidence that a port went down
MASS_DOWN_MIN_UP_STREAK = 2

# Stale-alarm janitor: types whose subjects can disappear from the
# observed state (a port/group/switch is gone) and the cycles a subject
# must stay missing before its alarm is auto-cleared
JANITOR_TYPES = {
    "lag_degraded", "port_errors", "port_discards", "port_util",
    "port_hosts_down", "port_frame_corruption", "port_flapping",
}
JANITOR_CYCLES = 5
JANITOR_NOTE = "auto-cleared: subject no longer present"

# Flap damping applies to alarms that can oscillate on their own
FLAP_TYPES = {
    "host_down", "port_hosts_down", "port_errors", "port_discards",
    "port_util", "lag_degraded", "stp_topology_change",
}


def _total(a: float | None, b: float | None) -> float | None:
    """Both halves or nothing — a rule cannot judge half a counter."""
    if a is None or b is None:
        return None
    return a + b


def _peak(a: float | None, b: float | None) -> float | None:
    """The busier direction, out of the ones the agent answered for.

    Utilization is a ceiling test, so one known direction is enough to
    trip it; two unknowns are not.
    """
    known = [v for v in (a, b) if v is not None]
    return max(known) if known else None


def _fmt_window(seconds: float) -> str:
    seconds = int(seconds)
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def display_subject(subject: str) -> str | None:
    """Human-readable form of composite subjects for notifications:
    "10.0.0.10:lag[Slot0/1+Slot0/2]" -> "10.0.0.10 LAG Slot0/1+Slot0/2".
    None means the raw subject is already readable."""
    ip, sep, rest = subject.partition(":")
    if sep and rest.startswith("lag[") and rest.endswith("]"):
        return f"{ip} LAG {rest[4:-1]}"
    return None


class AlarmEngine:
    def __init__(
        self,
        db: Database,
        notifier: Notifier,
        thresholds: Thresholds,
        notif_cfg: NotificationsConfig | None = None,
    ):
        self._db = db
        self._notifier = notifier
        self._thresholds = thresholds
        self._notif_cfg = notif_cfg or NotificationsConfig()
        self._active: set[tuple[str, str]] = set()  # (type, subject)
        self._ping_fails: dict[str, int] = {}       # mac -> consecutive misses
        self._snmp_fails: dict[str, int] = {}       # ip -> consecutive misses
        self._over: dict[tuple[str, str], int] = {}   # port rule hysteresis
        self._under: dict[tuple[str, str], int] = {}
        # consecutive successful pings per MAC (0 = silent right now)
        self._up_streak: dict[str, int] = {}
        # port_hosts_down: subject -> affected MACs (for the clear rule)
        self._mass_sets: dict[str, set[str]] = {}
        self._lag_over: dict[str, int] = {}          # subject -> degraded cycles
        # janitor: (type, subject) -> consecutive cycles missing from
        # the observed state
        self._missing: dict[tuple[str, str], int] = {}
        # flap damping: raise timestamps per key and the muted set
        # ((type, subject) -> ts of the last raise while flapping)
        self._raise_times: dict[tuple[str, str], deque[float]] = {}
        self._flapping: dict[tuple[str, str], float] = {}
        # frame corruption: subject -> {distorted mac: (ts, sample)}
        self._corrupt: dict[str, dict[str, tuple[float, str]]] = {}
        # when port_errors was last active on a subject, so a corruption
        # alarm can say the error counters agree with it
        self._errors_seen: dict[str, float] = {}
        # unmanaged bridges: chassis id -> when LLDP last showed it
        self._bridges_seen: dict[str, float] = {}
        # spanning tree: ip -> last designated root, ip -> last
        # dot1dStpTopChanges, and how long the network has been split
        self._stp_root: dict[str, str] = {}
        self._stp_changes: dict[str, int] = {}
        self._stp_fragmented_cycles: int = 0

    async def load(self) -> None:
        """Restores the active set from the DB after a restart.

        One-time migration: active lag_degraded alarms with a pre-0.5.2
        subject (not the stable "ip:lag[...]" form) can never clear —
        their key was the volatile synthetic bridge-port number — so
        they are auto-cleared right away.
        """
        for row in await asyncio.to_thread(self._db.alarms, True, 1000):
            key = (row["type"], row["subject"])
            self._active.add(key)
            if row["type"] == "lag_degraded" and ":lag[" not in row["subject"]:
                await self._clear(
                    row["type"], row["subject"],
                    "legacy subject key", note=JANITOR_NOTE,
                )
            elif (
                row["type"] == "port_errors"
                and "errors+discards" in row["message"]
            ):
                # Raised before v0.5.4, when discards counted as errors:
                # most such alarms are healthy ports doing normal
                # filtering. Cleared once; the split rule re-raises
                # within a few counter cycles if the errors are real.
                await self._clear(
                    row["type"], row["subject"],
                    "raised on errors+discards combined",
                    note="recalculated: discards are alarmed separately now",
                )
        if self._active:
            log.info("Restored %d active alarms from the DB", len(self._active))

    async def clear_missing_hosts(self, known_macs: set[str]) -> None:
        """Closes host_down alarms whose host is gone from the DB.

        The retention cleanup can delete a host that still has an
        active alarm; nothing would ever clear it afterwards.
        """
        for alarm_type, subject in list(self._active):
            if alarm_type == "host_down" and subject not in known_macs:
                await self._clear(
                    alarm_type, subject, "host removed from the inventory",
                    note=JANITOR_NOTE,
                )

    async def janitor(self, observed: set[tuple[str, str]]) -> None:
        """Auto-clears active alarms whose subject vanished from the
        observed state for JANITOR_CYCLES cycles in a row — insurance
        against any future subject-key changes."""
        for key in list(self._active):
            alarm_type, subject = key
            if alarm_type not in JANITOR_TYPES:
                continue
            if key in observed:
                self._missing.pop(key, None)
                continue
            cycles = self._missing.get(key, 0) + 1
            self._missing[key] = cycles
            if cycles >= JANITOR_CYCLES:
                self._missing.pop(key, None)
                log.warning(
                    "Janitor: %s %s missing for %d cycles — auto-clearing",
                    alarm_type, subject, cycles,
                )
                await self._clear(
                    alarm_type, subject, "subject no longer present",
                    note=JANITOR_NOTE,
                )

    # ---------- inputs ----------

    async def on_ping(
        self, results: dict[str, bool], meta: dict[str, dict]
    ) -> None:
        """One ping cycle: mac -> replied; meta = DB rows for labels.

        host_down is raised only for hosts with meta["monitored"] set —
        the journal keeps recording host_up/host_down for everyone
        (that happens in db.update_ping, not here).
        """
        streak_before = {mac: self._up_streak.get(mac, 0) for mac in results}
        for mac, up in results.items():
            row = meta.get(mac, {})
            label = row.get("name") or row.get("ip") or mac
            if up:
                self._ping_fails.pop(mac, None)
                self._up_streak[mac] = streak_before[mac] + 1
                await self._clear("host_down", mac, f"{label} answers ping again")
            else:
                self._up_streak[mac] = 0
                misses = self._ping_fails.get(mac, 0) + 1
                self._ping_fails[mac] = misses
                if (
                    misses >= HOST_DOWN_AFTER
                    and row.get("monitored")
                    and not row.get("stale")
                ):
                    await self._raise(
                        "host_down", mac,
                        f"{label} missed {misses} pings in a row",
                    )
        await self._mass_down(results, meta, streak_before)
        await self.flap_maintenance()

    async def _mass_down(
        self,
        results: dict[str, bool],
        meta: dict[str, dict],
        streak_before: dict[str, int],
    ) -> None:
        """port_hosts_down: many devices of one port went silent at once.

        Counted per DEVICE, not per host record: the identity is the IP
        when there is one (one address = one device) and the MAC
        otherwise. Only hosts that had been answering steadily count.
        """
        threshold = self._thresholds.mass_down_hosts
        if threshold <= 0:
            return

        def identity(mac: str) -> str:
            return meta.get(mac, {}).get("ip") or mac

        def label(mac: str) -> str:
            row = meta.get(mac, {})
            return row.get("name") or row.get("ip") or mac

        # Hosts that had been answering and are silent now, grouped by
        # the switch port they live on
        newly_down: dict[str, list[str]] = {}
        for mac, up in results.items():
            if up or streak_before.get(mac, 0) < MASS_DOWN_MIN_UP_STREAK:
                continue
            row = meta.get(mac, {})
            if row.get("stale"):
                continue  # its presence on that port is not confirmed
            if row.get("switch_ip") and row.get("port"):
                subject = f"{row['switch_ip']}:{row['port']}"
                newly_down.setdefault(subject, []).append(mac)
        for subject, macs in newly_down.items():
            # one label per device, first occurrence wins
            by_device: dict[str, str] = {}
            for mac in macs:
                by_device.setdefault(identity(mac), label(mac))
            if len(by_device) < threshold:
                continue
            self._mass_sets.setdefault(subject, set()).update(macs)
            names = ", ".join(list(by_device.values())[:5])
            await self._raise(
                "port_hosts_down", subject,
                f"{len(by_device)} devices went silent at once: {names}",
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

        # Clear when at least half of the affected devices answer again
        for subject, macs in list(self._mass_sets.items()):
            if ("port_hosts_down", subject) not in self._active:
                del self._mass_sets[subject]
                continue
            if not macs:
                continue
            devices = {identity(m) for m in macs}
            answering = {identity(m) for m in macs if results.get(m)}
            if len(answering) * 2 >= len(devices):
                del self._mass_sets[subject]
                await self._clear(
                    "port_hosts_down", subject,
                    f"{len(answering)} of {len(devices)} devices answer again",
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
        Mbit/s, in/out errors and discards per minute, error_ratio
        (None when the switch has no packet counters); LAG aggregates
        also carry lag_total / lag_up member counts.
        """
        thresholds = self._thresholds
        for m in metrics:
            subject = f"{ip}:{m['port']}"
            await self._error_rule(subject, m)
            discards = _total(
                m["in_discards_per_min"], m["out_discards_per_min"]
            )
            if discards is not None:
                await self._hysteresis(
                    "port_discards", subject,
                    discards > thresholds.discards_per_minute,
                    f"{discards:.0f} discards per minute "
                    f"(in: {m['in_discards_per_min']:.0f}, "
                    f"out: {m['out_discards_per_min']:.0f})",
                )
            speed = m["speed_mbps"]
            load = _peak(m["in_mbps"], m["out_mbps"])
            if speed and load is not None:
                util = load / speed * 100
                await self._hysteresis(
                    "port_util", subject,
                    util > self._thresholds.port_utilization_percent,
                    f"utilization {util:.0f}% of {speed} Mbit/s",
                )
            if m.get("lag_total"):
                await self._lag_rule(subject, m["lag_up"], m["lag_total"])

    async def _error_rule(self, subject: str, m: dict) -> None:
        """port_errors: damaged frames only, never discards.

        The absolute rate must be over the threshold AND — when the
        switch exposes packet counters — the errors must be a large
        enough share of the traffic. A busy port doing millions of
        frames a minute is not in trouble over a handful of errors.
        """
        thresholds = self._thresholds
        errors = _total(m["in_errors_per_min"], m["out_errors_per_min"])
        if errors is None:
            # The agent did not answer for this column. Neither raising
            # nor clearing is honest: we do not know.
            return
        over = errors > thresholds.errors_per_minute
        ratio = m.get("error_ratio")
        share = ""
        if ratio is not None:
            over = over and ratio * 100 > thresholds.error_ratio_percent
            share = f", {ratio * 100:.3f}% of frames"
        await self._hysteresis(
            "port_errors", subject, over,
            f"{errors:.1f} errors per minute "
            f"(in: {m['in_errors_per_min']:.1f}, "
            f"out: {m['out_errors_per_min']:.1f}{share})",
        )

    async def _lag_rule(self, subject: str, up: int, total: int) -> None:
        """lag_degraded: raised after port_alarm_cycles degraded counter
        cycles, cleared as soon as every member is up again."""
        if up < total:
            self._lag_over[subject] = self._lag_over.get(subject, 0) + 1
            if self._lag_over[subject] >= self._thresholds.port_alarm_cycles:
                await self._raise(
                    "lag_degraded", subject,
                    f"{up} of {total} LAG members are up",
                )
        else:
            self._lag_over.pop(subject, None)
            await self._clear(
                "lag_degraded", subject, f"all {total} LAG members are up"
            )

    async def on_corruption(self, suspects: dict[str, list[dict]]) -> None:
        """One scan's distorted MACs, keyed by "switch ip:port".

        Counted over the flap window rather than per scan: a bad cable
        produces a few invented addresses per poll, and it is their
        accumulation that is the symptom. Every address is counted once
        — an artifact that lingers must not keep pushing the total up.
        """
        threshold = self._thresholds.corruption_macs_threshold
        if threshold <= 0:
            return
        now = time.time()
        window = self._notif_cfg.flap_window_seconds
        for subject, found in suspects.items():
            seen = self._corrupt.setdefault(subject, {})
            for s in found:
                seen.setdefault(s["mac"], (now, s["sample"]))
        for subject in list(self._corrupt):
            seen = self._corrupt[subject]
            for mac, (ts, _sample) in list(seen.items()):
                if now - ts > window:
                    del seen[mac]
            if not seen:
                del self._corrupt[subject]
                await self._clear(
                    "port_frame_corruption", subject,
                    f"no distorted MAC in {_fmt_window(window)}",
                )
                continue
            if len(seen) < threshold:
                continue
            samples: dict[str, int] = {}
            for _ts, sample in seen.values():
                samples[sample] = samples.get(sample, 0) + 1
            sample = max(samples, key=lambda mac: (samples[mac], mac))
            copies = ", ".join(sorted(seen)[:5])
            confirmed = self._errors_recent(subject, now, window)
            message = (
                f"{len(seen)} distorted copies of {sample} in the MAC table "
                f"({copies}) — check the cable, the patch cord and the port"
            )
            if confirmed:
                message += "; confirmed by the port's error counters"
            severity = "critical" if confirmed else None
            if ("port_frame_corruption", subject) in self._active:
                await self._escalate(
                    "port_frame_corruption", subject, severity, message
                )
                continue
            await self._raise(
                "port_frame_corruption", subject, message, severity=severity
            )

    async def _escalate(
        self, alarm_type: str, subject: str, severity: str | None, message: str
    ) -> None:
        """Upgrades a standing alarm when later evidence confirms it."""
        if severity is None or severity == SEVERITIES[alarm_type]:
            return
        upgraded = await asyncio.to_thread(
            self._db.escalate_alarm, alarm_type, subject, severity, message
        )
        if not upgraded:
            return
        ts = time.time()
        await asyncio.to_thread(
            self._db.add_event, ts, "alarm_raised", subject,
            f"{severity} {alarm_type}: {message}",
        )
        log.warning("Alarm escalated: %s %s — %s", alarm_type, subject, message)
        await self._notifier.notify(
            alarm_type, subject, severity, message,
            display=display_subject(subject),
        )

    def _errors_recent(self, subject: str, now: float, window: float) -> bool:
        """Is port_errors active on this port, or was it just cleared?"""
        if ("port_errors", subject) in self._active:
            return True
        last = self._errors_seen.get(subject, 0)
        return bool(last) and now - last <= window

    async def on_flaps(self, ip: str, ports: list[dict]) -> None:
        """One counters cycle's link-state transitions.

        Each entry is {"port": name, "flaps": n, "last": unix time},
        already counted over thresholds.flap_window_minutes by
        counters.FlapTracker. The alarm is raised at the first cycle
        over the threshold — a port bouncing four times in half a
        minute is not a condition to confirm over three cycles, and the
        window itself already does the smoothing.
        """
        threshold = self._thresholds.flaps_per_window
        if threshold <= 0:
            return
        window = self._thresholds.flap_window_minutes
        for entry in ports:
            subject = f"{ip}:{entry['port']}"
            if entry["flaps"] >= threshold:
                when = time.strftime(
                    "%H:%M:%S", time.localtime(entry["last"])
                ) if entry["last"] else "?"
                await self._raise(
                    "port_flapping", subject,
                    f"the link changed state {entry['flaps']} times in "
                    f"{window:g} min, last at {when}",
                )
            elif entry["flaps"] == 0:
                await self._clear(
                    "port_flapping", subject,
                    f"no link transition in {window:g} min",
                )

    async def clear_suppressed(self, suppressed: dict[str, str]) -> None:
        """Closes alarms the configuration has just made pointless.

        A change to the config should close what it makes moot. The
        CE6851 alarm was raised before mb0 Slot0/25 went into
        uplink_ports and then sat in the active list for a day: the
        only clear condition was the neighbour disappearing from LLDP,
        which a provider handover never does. The subject is now
        suppressed by configuration, so the alarm goes, with the reason
        in the journal.
        """
        for subject, reason in suppressed.items():
            self._bridges_seen.pop(subject, None)
            await self._clear("unmanaged_bridge_detected", subject, reason)

    async def on_bridges(
        self, bridges: list[dict], known: set[str]
    ) -> None:
        """LLDP found switches behind our access ports.

        Only devices that actually claim the bridge capability count:
        a neighbour that sends no capabilities TLV is shown on the map
        but never alarmed on, because the absence of the bit proves
        nothing about what the device is.

        A bridge on a trunk port is the network working as designed —
        that is what a trunk is. A bridge on an access port is someone
        plugging a switch in, and that is the whole point of the alarm.
        """
        now = time.time()
        window = self._notif_cfg.flap_window_seconds
        for bridge in bridges:
            if bridge.get("trunk") or bridge.get("unidentified"):
                continue
            if bridge.get("external"):
                continue  # a port that leaves the network: not ours
            chassis_id = bridge["chassis_id"]
            self._bridges_seen[chassis_id] = now
            if chassis_id.lower() in known:
                continue
            if bridge.get("mgmt_ip", "").lower() in known:
                continue
            where = f"{bridge['switch']} port {bridge['port']}"
            name = bridge.get("name") or chassis_id
            address = f", {bridge['mgmt_ip']}" if bridge.get("mgmt_ip") else ""
            if bridge.get("cap_assumed"):
                # The switch fills lldpRemSysCapEnabled for nobody, so
                # this device was called a bridge because it announced a
                # name and an address — which a managed access point or
                # a printer does too. Worth recording, not worth waking
                # anyone.
                await self._raise(
                    "unmanaged_bridge_detected", chassis_id,
                    f"{name} ({chassis_id}{address}) behind {where} is "
                    f"probably a bridge — the switch reports no "
                    f"capabilities for any neighbour, so this is an "
                    f"inference, not its own claim",
                    severity="info",
                )
                continue
            await self._raise(
                "unmanaged_bridge_detected", chassis_id,
                f"{name} ({chassis_id}{address}) announces itself as a "
                f"bridge behind {where} — a switch nobody polls",
            )
        for chassis_id, last in list(self._bridges_seen.items()):
            if now - last <= window:
                continue
            del self._bridges_seen[chassis_id]
            await self._clear(
                "unmanaged_bridge_detected", chassis_id,
                f"gone from LLDP for {_fmt_window(window)}",
            )

    async def on_stp(self, report: dict) -> None:
        """One scan's spanning-tree picture.

        Every rule here applies ONLY to switches the report calls
        operating. That is the whole discipline of this feature: a
        switch with STP disabled reports priority 0, cost 0 and itself
        as root, and alarming on those numbers is how a network with no
        spanning tree at all acquires five root bridges and a stream of
        root-change alarms.
        """
        operating = [s for s in report.get("switches", []) if s["operating"]]
        for entry in operating:
            ip = entry["ip"]
            root = entry["designated_root"]
            previous = self._stp_root.get(ip)
            self._stp_root[ip] = root
            if previous and root and previous != root:
                await self._raise(
                    "stp_root_changed", ip,
                    f"{entry['name']} now follows root {root} "
                    f"(was {previous})",
                )
            elif previous == root:
                await self._clear(
                    "stp_root_changed", ip, f"root {root} is stable again"
                )

            changes = entry["top_changes"]
            before = self._stp_changes.get(ip)
            self._stp_changes[ip] = changes
            if before is None:
                continue  # first scan: a baseline, not a delta
            delta = changes - before
            limit = self._thresholds.stp_changes_per_cycle
            if delta > limit:
                await self._raise(
                    "stp_topology_change", ip,
                    f"{entry['name']}: {delta} topology changes since the "
                    f"previous scan (limit {limit}), "
                    f"{entry['top_changes']} in total",
                )
            elif delta >= 0:
                await self._clear(
                    "stp_topology_change", ip,
                    f"{delta} topology changes since the previous scan",
                )

        # Switches that stopped operating must not keep a stale root in
        # memory: coming back would look like a root change
        alive = {entry["ip"] for entry in operating}
        for ip in list(self._stp_root):
            if ip not in alive:
                del self._stp_root[ip]
                self._stp_changes.pop(ip, None)
                await self._clear(
                    "stp_root_changed", ip, "the switch is no longer in a tree"
                )
                await self._clear(
                    "stp_topology_change", ip,
                    "the switch is no longer in a tree",
                )

        verdict = report.get("verdict", {})
        if verdict.get("verdict") == "fragmented":
            self._stp_fragmented_cycles += 1
            # one scan is not evidence: a converging tree passes through
            # disagreement on its way to agreement
            if self._stp_fragmented_cycles >= 2:
                roots = verdict.get("roots", {})
                detail = "; ".join(
                    f"{root}: {', '.join(ips)}" for root, ips in sorted(roots.items())
                )
                await self._raise(
                    "stp_fragmented", "network",
                    f"{len(roots)} separate spanning trees — {detail}",
                )
        else:
            self._stp_fragmented_cycles = 0
            await self._clear(
                "stp_fragmented", "network",
                "one tree" if verdict.get("verdict") == "single"
                else "no switch is running a spanning tree",
            )

    async def on_new_macs(self, new_macs: list[str], details: dict[str, str]) -> None:
        for mac in new_macs:
            await self._raise("new_mac", mac, details.get(mac, ""), auto_clear=True)

    async def manual_clear(self, alarm_id: int) -> dict | None:
        """Operator-initiated clear of one active alarm by its id.

        Returns the cleared row or None if there is no such active
        alarm. Notifies through the normal routing.
        """
        ts = time.time()
        row = await asyncio.to_thread(
            self._db.clear_alarm_by_id, alarm_id, ts, "cleared manually"
        )
        if row is None:
            return None
        alarm_type, subject = row["type"], row["subject"]
        self._active.discard((alarm_type, subject))
        self._missing.pop((alarm_type, subject), None)
        await asyncio.to_thread(
            self._db.add_event, ts, "alarm_cleared", subject,
            f"{row['severity']} {alarm_type}: cleared manually",
        )
        log.info("Alarm cleared manually: %s %s (id %d)",
                 alarm_type, subject, alarm_id)
        await self._notifier.notify(
            alarm_type, subject, row["severity"], "cleared manually",
            cleared=True, display=display_subject(subject),
        )
        return row

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
        cycles = self._thresholds.port_alarm_cycles
        if self._over.get(key, 0) >= cycles:
            await self._raise(alarm_type, subject, message)
        elif key in self._active and self._under.get(key, 0) >= cycles:
            await self._clear(alarm_type, subject, "back below the threshold")

    async def _raise(
        self, alarm_type: str, subject: str, message: str,
        auto_clear: bool = False, severity: str | None = None,
    ) -> None:
        if not auto_clear and (alarm_type, subject) in self._active:
            return
        ts = time.time()
        severity = severity or SEVERITIES[alarm_type]
        if alarm_type == "port_errors":
            self._errors_seen[subject] = ts
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
        if await self._flap_on_raise(alarm_type, subject, severity):
            return  # muted: the alarm is in the DB/journal, no notification
        await self._notifier.notify(
            alarm_type, subject, severity, message,
            display=display_subject(subject),
        )

    async def _clear(
        self, alarm_type: str, subject: str, message: str, note: str = ""
    ) -> None:
        if (alarm_type, subject) not in self._active:
            return
        ts = time.time()
        self._active.discard((alarm_type, subject))
        cleared = await asyncio.to_thread(
            self._db.clear_alarm, alarm_type, subject, ts, note
        )
        if not cleared:
            return
        if alarm_type == "port_errors":
            # a corruption alarm raised soon after still counts as
            # confirmed by the counters
            self._errors_seen[subject] = ts
        severity = SEVERITIES[alarm_type]
        await asyncio.to_thread(
            self._db.add_event, ts, "alarm_cleared", subject,
            f"{severity} {alarm_type}: {message}",
        )
        log.info("Alarm cleared: %s %s — %s", alarm_type, subject, message)
        if (alarm_type, subject) in self._flapping:
            return  # muted while the subject is flapping
        await self._notifier.notify(
            alarm_type, subject, severity, message, cleared=True,
            display=display_subject(subject),
        )

    # ---------- flap damping ----------

    def flapping_keys(self) -> set[tuple[str, str]]:
        return set(self._flapping)

    def raise_stats(self) -> dict[tuple[str, str], tuple[int, float]]:
        """Per subject: raises inside the flap window and the last one."""
        now = time.time()
        window = self._notif_cfg.flap_window_seconds
        stats: dict[tuple[str, str], tuple[int, float]] = {}
        for key, times in self._raise_times.items():
            recent = [t for t in times if now - t <= window]
            if recent:
                stats[key] = (len(recent), recent[-1])
        return stats

    async def _flap_on_raise(
        self, alarm_type: str, subject: str, severity: str
    ) -> bool:
        """Registers a raise for flap damping; True = mute this raise."""
        if alarm_type not in FLAP_TYPES:
            return False
        cfg = self._notif_cfg
        if cfg.flap_count <= 0:
            return False
        key = (alarm_type, subject)
        now = time.time()
        history = self._raise_times.setdefault(key, deque())
        history.append(now)
        while history and now - history[0] > cfg.flap_window_seconds:
            history.popleft()
        if key in self._flapping:
            self._flapping[key] = now  # the quiet timer restarts
            return True
        if len(history) >= cfg.flap_count:
            self._flapping[key] = now
            log.warning(
                "Flapping detected: %s %s — %d raises in %s, muting",
                alarm_type, subject, len(history),
                _fmt_window(cfg.flap_window_seconds),
            )
            await self._notifier.notify(
                alarm_type, subject, severity,
                f"{len(history)} raises in "
                f"{_fmt_window(cfg.flap_window_seconds)}, notifications muted",
                head="FLAPPING", display=display_subject(subject),
            )
            return True
        return False

    async def flap_maintenance(self) -> None:
        """Ends the flapping state after flap_quiet_seconds without a
        single raise, with a "flapping ended" notification."""
        cfg = self._notif_cfg
        now = time.time()
        for key, last_raise in list(self._flapping.items()):
            if now - last_raise < cfg.flap_quiet_seconds:
                continue
            del self._flapping[key]
            self._raise_times.pop(key, None)
            alarm_type, subject = key
            log.info("Flapping ended: %s %s", alarm_type, subject)
            await self._notifier.notify(
                alarm_type, subject, SEVERITIES[alarm_type],
                "flapping ended", head="FLAPPING",
                display=display_subject(subject),
            )
