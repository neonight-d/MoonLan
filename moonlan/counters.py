"""Port traffic and error counters.

A separate light polling loop (much cheaper than the full topology
scan) walks octet, error and discard counters of every switch and
turns the deltas between cycles into per-port rates: Mbit/s in/out,
errors and discards per minute.

The first cycle only records a baseline. Negative deltas (switch
reboot, counter reset) are dropped; 32-bit octet counters get
wraparound correction — at 1 Gbit/s a Counter32 wraps in about 34
seconds, so the correction is a routine event, not an edge case.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

from .snmp_collector import (
    OID_IF_LAST_CHANGE,
    OID_IF_OPER_STATUS,
    SnmpCollector,
)

log = logging.getLogger(__name__)

OID_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"    # ifHCInOctets (64-bit)
OID_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"  # ifHCOutOctets (64-bit)
OID_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"         # ifInOctets (32-bit fallback)
OID_OUT_OCTETS = "1.3.6.1.2.1.2.2.1.16"        # ifOutOctets (32-bit fallback)
OID_IN_ERRORS = "1.3.6.1.2.1.2.2.1.14"         # ifInErrors
OID_OUT_ERRORS = "1.3.6.1.2.1.2.2.1.20"        # ifOutErrors
OID_IN_DISCARDS = "1.3.6.1.2.1.2.2.1.13"       # ifInDiscards
OID_OUT_DISCARDS = "1.3.6.1.2.1.2.2.1.19"      # ifOutDiscards
OID_HC_IN_PKTS = "1.3.6.1.2.1.31.1.1.1.7"      # ifHCInUcastPkts (64-bit)
OID_HC_OUT_PKTS = "1.3.6.1.2.1.31.1.1.1.11"    # ifHCOutUcastPkts (64-bit)
OID_IN_PKTS = "1.3.6.1.2.1.2.2.1.11"           # ifInUcastPkts (32-bit)
OID_OUT_PKTS = "1.3.6.1.2.1.2.2.1.17"          # ifOutUcastPkts (32-bit)

HISTORY_POINTS = 60  # ring buffer length per port
WRAP32 = 2 ** 32
# Sanity caps: a rate above 1.2x the known link speed is an artifact
# (usually a Counter32 wraparound misread as traffic); with no known
# speed the absolute ceiling applies. Same idea for error counters.
LINK_SPEED_MARGIN = 1.2
MAX_SANE_MBPS = 100_000        # 100 Gbit/s, when the link speed is unknown
MAX_SANE_ERRORS_PER_MIN = 1e6


@dataclass
class Sample:
    """Raw counter values of one port at one moment."""

    ts: float
    in_octets: int = 0
    out_octets: int = 0
    in_errors: int = 0
    out_errors: int = 0
    in_discards: int = 0
    out_discards: int = 0
    in_pkts: int = 0
    out_pkts: int = 0
    hc: bool = True  # octet counters are 64-bit (no wraparound possible)
    # ifLastChange, TimeTicks since the agent's epoch. Two polls that
    # show the same oper status but a moved ifLastChange mean the link
    # bounced in between — which is exactly what a flapping port does.
    last_change: int = 0


@dataclass
class PortRates:
    """Rates computed from the delta between two samples.

    Errors and discards are kept apart on purpose: errors mean damaged
    frames (a physical problem), discards are usually normal filtering
    — VLAN rules, storm control, a full buffer during a burst.
    """

    ts: float
    in_mbps: float
    out_mbps: float
    in_errors_per_min: float
    out_errors_per_min: float
    in_discards_per_min: float
    out_discards_per_min: float
    # errors as a share of all frames on the port (0.01 = 1%); None
    # when the switch exposes no packet counters
    error_ratio: float | None = None

    @property
    def errors_per_min(self) -> float:
        return self.in_errors_per_min + self.out_errors_per_min

    @property
    def discards_per_min(self) -> float:
        return self.in_discards_per_min + self.out_discards_per_min


async def collect_samples(
    collector: SnmpCollector, host: str
) -> tuple[dict[int, Sample], dict[int, bool]]:
    """One counters poll of a switch: (ifIndex -> Sample, ifIndex -> oper up).

    Prefers 64-bit ifHC* octet counters; if the switch has none,
    falls back to the 32-bit ones (marked hc=False for wraparound
    handling). Also walks ifOperStatus so LAG degradation is noticed
    at the counters cadence, not only on full topology scans. Uses the
    collector's low-level walk — the counters loop deliberately shares
    the SNMP transport with the topology scan.
    """
    ts = time.time()
    samples: dict[int, Sample] = {}
    oper: dict[int, bool] = {}

    def sample(if_index: int) -> Sample:
        return samples.setdefault(if_index, Sample(ts=ts))

    async for suffix, value in collector._walk(host, OID_HC_IN_OCTETS):
        sample(suffix[0]).in_octets = int(value)
    if samples:
        async for suffix, value in collector._walk(host, OID_HC_OUT_OCTETS):
            sample(suffix[0]).out_octets = int(value)
    else:
        async for suffix, value in collector._walk(host, OID_IN_OCTETS):
            sample(suffix[0]).in_octets = int(value)
        async for suffix, value in collector._walk(host, OID_OUT_OCTETS):
            sample(suffix[0]).out_octets = int(value)
        for s in samples.values():
            s.hc = False

    for oid, attr in (
        (OID_IN_ERRORS, "in_errors"),
        (OID_OUT_ERRORS, "out_errors"),
        (OID_IN_DISCARDS, "in_discards"),
        (OID_OUT_DISCARDS, "out_discards"),
    ):
        async for suffix, value in collector._walk(host, oid):
            setattr(sample(suffix[0]), attr, int(value))

    # Unicast packet counters turn the error count into a share of the
    # traffic — a port doing millions of frames a minute and a quiet
    # one need very different error counts to be worth an alarm
    got_pkts = False
    async for suffix, value in collector._walk(host, OID_HC_IN_PKTS):
        sample(suffix[0]).in_pkts = int(value)
        got_pkts = True
    if got_pkts:
        async for suffix, value in collector._walk(host, OID_HC_OUT_PKTS):
            sample(suffix[0]).out_pkts = int(value)
    else:
        async for suffix, value in collector._walk(host, OID_IN_PKTS):
            sample(suffix[0]).in_pkts = int(value)
        async for suffix, value in collector._walk(host, OID_OUT_PKTS):
            sample(suffix[0]).out_pkts = int(value)

    async for suffix, value in collector._walk(host, OID_IF_OPER_STATUS):
        oper[suffix[0]] = int(value) == 1
    async for suffix, value in collector._walk(host, OID_IF_LAST_CHANGE):
        sample(suffix[0]).last_change = int(value)

    return samples, oper


def _octet_delta(prev: int, cur: int, hc: bool) -> int | None:
    """Octet delta between samples; None = invalid (counter reset).

    The +2^32 wraparound correction applies to 32-bit counters only;
    whether the corrected value is plausible is checked by the caller
    against the link speed — an implausible one resets the baseline.
    """
    d = cur - prev
    if d < 0 and not hc:
        d += WRAP32  # Counter32 wraparound
    return d if d >= 0 else None


class CounterStore:
    """Per-port rate history computed from counter deltas.

    Keeps the last raw sample per port as the baseline and a ring
    buffer of HISTORY_POINTS rate points. Not thread-safe on purpose:
    everything runs in the asyncio event loop.
    """

    def __init__(self, history: int = HISTORY_POINTS):
        self._history = history
        self._last: dict[tuple[str, int], Sample] = {}
        self._rates: dict[tuple[str, int], deque[PortRates]] = {}

    def update(
        self,
        ip: str,
        samples: dict[int, Sample],
        speeds: dict[int, int] | None = None,
    ) -> dict[int, PortRates]:
        """Applies a fresh poll; returns the rates computed this cycle.

        speeds (ifIndex -> link Mbit/s) drives the sanity check: a rate
        above 1.2x the link speed is an artifact, not traffic — the
        point is dropped and the fresh sample becomes the new baseline.
        """
        speeds = speeds or {}
        fresh: dict[int, PortRates] = {}
        for if_index, cur in samples.items():
            key = (ip, if_index)
            prev = self._last.get(key)
            self._last[key] = cur
            if prev is None:
                continue  # first cycle: baseline only
            dt = cur.ts - prev.ts
            if dt <= 0:
                continue
            d_in = _octet_delta(prev.in_octets, cur.in_octets, cur.hc)
            d_out = _octet_delta(prev.out_octets, cur.out_octets, cur.hc)
            error_deltas = [
                cur.in_errors - prev.in_errors,
                cur.out_errors - prev.out_errors,
                cur.in_discards - prev.in_discards,
                cur.out_discards - prev.out_discards,
            ]
            # Negative delta = counters were reset (reboot): drop the cycle,
            # the fresh sample above becomes the new baseline
            if d_in is None or d_out is None or min(error_deltas) < 0:
                continue
            in_mbps = d_in * 8 / dt / 1e6
            out_mbps = d_out * 8 / dt / 1e6
            link_speed = speeds.get(if_index, 0)
            cap = link_speed * LINK_SPEED_MARGIN if link_speed > 0 else MAX_SANE_MBPS
            if max(in_mbps, out_mbps) > cap:
                # wraparound misfire or a reboot disguised as one
                log.debug(
                    "%s ifIndex %d: implausible rate %.0f/%.0f Mbit/s "
                    "(cap %.0f), raw octets in %d->%d out %d->%d — "
                    "baseline reset",
                    ip, if_index, in_mbps, out_mbps, cap,
                    prev.in_octets, cur.in_octets,
                    prev.out_octets, cur.out_octets,
                )
                continue
            per_min = [d * 60 / dt for d in error_deltas]
            errors_per_min = per_min[0] + per_min[1]
            discards_per_min = per_min[2] + per_min[3]
            if max(errors_per_min, discards_per_min) > MAX_SANE_ERRORS_PER_MIN:
                log.debug(
                    "%s ifIndex %d: implausible error rate %.0f/%.0f per "
                    "minute, raw errors %d->%d discards %d->%d — "
                    "baseline reset",
                    ip, if_index, errors_per_min, discards_per_min,
                    prev.in_errors + prev.out_errors,
                    cur.in_errors + cur.out_errors,
                    prev.in_discards + prev.out_discards,
                    cur.in_discards + cur.out_discards,
                )
                continue
            # Packet counters may be 32-bit even where octets are not,
            # so a negative delta only costs this cycle's ratio — no
            # wraparound correction is guessed at
            d_pkts = (cur.in_pkts - prev.in_pkts) + (cur.out_pkts - prev.out_pkts)
            errors = error_deltas[0] + error_deltas[1]
            frames = d_pkts + errors
            ratio = errors / frames if d_pkts > 0 and frames > 0 else None
            rates = PortRates(
                ts=cur.ts,
                in_mbps=in_mbps,
                out_mbps=out_mbps,
                in_errors_per_min=per_min[0],
                out_errors_per_min=per_min[1],
                in_discards_per_min=per_min[2],
                out_discards_per_min=per_min[3],
                error_ratio=ratio,
            )
            fresh[if_index] = rates
            self._rates.setdefault(key, deque(maxlen=self._history)).append(rates)
        return fresh

    def current(self, ip: str, max_age: float | None = None) -> dict[int, PortRates]:
        """Latest rates of every port of a switch (fresh enough ones only)."""
        now = time.time()
        out: dict[int, PortRates] = {}
        for (sw_ip, if_index), history in self._rates.items():
            if sw_ip != ip or not history:
                continue
            latest = history[-1]
            if max_age is not None and now - latest.ts > max_age:
                continue
            out[if_index] = latest
        return out

    def history(self, ip: str, if_index: int) -> list[PortRates]:
        return list(self._rates.get((ip, if_index), ()))


FLAP_HISTORY = 64  # transitions remembered per port


@dataclass
class PortFlaps:
    """How often one port changed link state inside the window."""

    count: int
    last: float  # unix time of the most recent transition


class FlapTracker:
    """Link-state transitions per port, counted over a sliding window.

    A counters cycle is a minute apart, and a flapping port can go up
    and down four times in thirty seconds — polling oper status alone
    would see none of it. ifLastChange closes the gap: when it moves
    between two polls that report the SAME status, the link went down
    and came back (or up and went down) while nobody was looking, so
    the pair counts as two transitions rather than none.

    ifLastChange going BACKWARDS means the agent restarted its clock;
    the port's history is dropped rather than reinterpreted.
    """

    def __init__(self, window_seconds: float = 600.0):
        self.window_seconds = window_seconds
        self._oper: dict[tuple[str, int], bool] = {}
        self._last_change: dict[tuple[str, int], int] = {}
        self._events: dict[tuple[str, int], deque[float]] = {}

    def update(
        self,
        ip: str,
        oper: dict[int, bool],
        last_change: dict[int, int],
        now: float | None = None,
    ) -> dict[int, PortFlaps]:
        now = time.time() if now is None else now
        for if_index, up in oper.items():
            key = (ip, if_index)
            changed = last_change.get(if_index, 0)
            previous_up = self._oper.get(key)
            previous_change = self._last_change.get(key)
            self._oper[key] = up
            self._last_change[key] = changed
            if previous_up is None:
                continue  # first cycle: a baseline, not a transition
            if previous_change is not None and changed < previous_change:
                # the agent's uptime restarted: nothing here is comparable
                self._events.pop(key, None)
                continue
            moved = (
                previous_change is not None and changed > previous_change
            )
            if moved and previous_up == up:
                transitions = 2  # down and back up between two polls
            elif moved or previous_up != up:
                transitions = 1
            else:
                transitions = 0
            if not transitions:
                continue
            history = self._events.setdefault(key, deque(maxlen=FLAP_HISTORY))
            for _ in range(transitions):
                history.append(now)
        result: dict[int, PortFlaps] = {}
        for (sw_ip, if_index), history in self._events.items():
            if sw_ip != ip:
                continue
            while history and now - history[0] > self.window_seconds:
                history.popleft()
            result[if_index] = PortFlaps(
                count=len(history), last=history[-1] if history else 0.0
            )
        return result
