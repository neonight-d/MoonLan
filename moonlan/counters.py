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
    """Raw counter values of one port at one moment.

    Every counter is `int | None`, and None means the agent did not
    answer for that column — which is not the same thing as zero. The
    DGS-1210 implements ifHCInOctets and not ifHCOutOctets, and until
    v0.6.4 that came out of the panel as a confident "0.0 Mbit/s out"
    on a trunk carrying traffic in both directions.
    """

    ts: float
    in_octets: int | None = None
    out_octets: int | None = None
    in_errors: int | None = None
    out_errors: int | None = None
    in_discards: int | None = None
    out_discards: int | None = None
    in_pkts: int | None = None
    out_pkts: int | None = None
    # 64-bit counters cannot wrap; the 32-bit fallbacks can, and the
    # two directions fall back independently
    hc_in: bool = True
    hc_out: bool = True
    # ifLastChange, TimeTicks since the agent's epoch. Two polls that
    # show the same oper status but a moved ifLastChange mean the link
    # bounced in between — which is exactly what a flapping port does.
    last_change: int = 0


@dataclass
class ColumnStatus:
    """Whether a counter column answered at all, and with what.

    "no answer" and "all zeros" look identical in a rate table and mean
    opposite things, so the distinction is carried out of the poll
    rather than reconstructed later.
    """

    oid: str
    rows: int = 0
    error: str = ""

    @property
    def answered(self) -> bool:
        return self.rows > 0


@dataclass
class PortRates:
    """Rates computed from the delta between two samples.

    Errors and discards are kept apart on purpose: errors mean damaged
    frames (a physical problem), discards are usually normal filtering
    — VLAN rules, storm control, a full buffer during a burst.

    A None rate means the agent never answered for that column. It is
    shown as "—" and raises no alarm, in either direction.
    """

    ts: float
    in_mbps: float | None
    out_mbps: float | None
    in_errors_per_min: float | None
    out_errors_per_min: float | None
    in_discards_per_min: float | None
    out_discards_per_min: float | None
    # errors as a share of all frames on the port (0.01 = 1%); None
    # when the switch exposes no packet counters
    error_ratio: float | None = None

    @staticmethod
    def _total(a: float | None, b: float | None) -> float | None:
        """Both halves or nothing: half a total is not a total."""
        if a is None or b is None:
            return None
        return a + b

    @property
    def errors_per_min(self) -> float | None:
        return self._total(self.in_errors_per_min, self.out_errors_per_min)

    @property
    def discards_per_min(self) -> float | None:
        return self._total(self.in_discards_per_min, self.out_discards_per_min)


async def collect_samples(
    collector: SnmpCollector, host: str
) -> tuple[dict[int, Sample], dict[int, bool], dict[str, ColumnStatus]]:
    """One counters poll: (ifIndex -> Sample, ifIndex -> oper up, columns).

    Each column is walked on its own and its answer recorded, because
    on real hardware they disagree. The DGS-1210 Rev.F1 answers
    ifHCInOctets and returns nothing at all for ifHCOutOctets, while
    the 32-bit ifOutOctets works fine. Until v0.6.4 the fallback to
    32-bit counters was decided once, by whether the FIRST column
    answered, so a switch like that lost its outbound traffic
    permanently: the fallback branch only ran when nothing came back
    for the inbound column either.

    A column that answers nothing leaves its fields None. That is
    reported as "—" rather than as zero, and raises no alarm.
    """
    ts = time.time()
    samples: dict[int, Sample] = {}
    oper: dict[int, bool] = {}
    columns: dict[str, ColumnStatus] = {}

    def sample(if_index: int) -> Sample:
        return samples.setdefault(if_index, Sample(ts=ts))

    async def walk_into(column: str, oid: str, attr: str) -> int:
        """Fills one counter column; returns how many rows answered."""
        status = ColumnStatus(oid=oid)
        columns[column] = status
        async for suffix, value in collector._walk(host, oid):
            setattr(sample(suffix[0]), attr, int(value))
            status.rows += 1
        status.error = collector.last_walk_error(host, oid)
        return status.rows

    # Octets, each direction falling back on its own
    for column, hc_oid, oid32, attr, hc_flag in (
        ("in_octets", OID_HC_IN_OCTETS, OID_IN_OCTETS, "in_octets", "hc_in"),
        ("out_octets", OID_HC_OUT_OCTETS, OID_OUT_OCTETS, "out_octets", "hc_out"),
    ):
        if await walk_into(column, hc_oid, attr):
            continue
        if await walk_into(column, oid32, attr):
            for s in samples.values():
                setattr(s, hc_flag, False)

    for column, oid in (
        ("in_errors", OID_IN_ERRORS),
        ("out_errors", OID_OUT_ERRORS),
        ("in_discards", OID_IN_DISCARDS),
        ("out_discards", OID_OUT_DISCARDS),
    ):
        await walk_into(column, oid, column)

    # Unicast packet counters turn the error count into a share of the
    # traffic — a port doing millions of frames a minute and a quiet
    # one need very different error counts to be worth an alarm
    for column, hc_oid, oid32 in (
        ("in_pkts", OID_HC_IN_PKTS, OID_IN_PKTS),
        ("out_pkts", OID_HC_OUT_PKTS, OID_OUT_PKTS),
    ):
        if not await walk_into(column, hc_oid, column):
            await walk_into(column, oid32, column)

    async for suffix, value in collector._walk(host, OID_IF_OPER_STATUS):
        oper[suffix[0]] = int(value) == 1
    async for suffix, value in collector._walk(host, OID_IF_LAST_CHANGE):
        sample(suffix[0]).last_change = int(value)

    silent = [c for c, st in columns.items() if not st.answered]
    if silent:
        log.info(
            "%s: no answer on %d counter column(s): %s — they are reported "
            "as unknown, not as zero",
            host, len(silent),
            ", ".join(f"{c} ({columns[c].oid})" for c in sorted(silent)),
        )
    return samples, oper, columns


def _octet_delta(prev: int | None, cur: int | None, hc: bool) -> int | None:
    """Octet delta between samples; None = unusable (unknown or a reset).

    The +2^32 wraparound correction applies to 32-bit counters only;
    whether the corrected value is plausible is checked by the caller
    against the link speed — an implausible one resets the baseline.
    """
    if prev is None or cur is None:
        return None  # the agent did not answer for this column
    d = cur - prev
    if d < 0 and not hc:
        d += WRAP32  # Counter32 wraparound
    return d if d >= 0 else None


def _counter_delta(prev: int | None, cur: int | None) -> float | None:
    """Delta of an error/discard counter; None when it cannot be read.

    A negative delta means the counters were reset (a reboot), which is
    not a rate — it is the absence of one until the next cycle.
    """
    if prev is None or cur is None:
        return None
    delta = cur - prev
    return None if delta < 0 else float(delta)


def _error_ratio(prev: Sample, cur: Sample) -> float | None:
    """Errors as a share of the frames the port carried, or None.

    None when either the packet counters or the error counters are
    missing: a share of an unknown total is not a share.
    """
    d_pkts = _counter_delta(prev.in_pkts, cur.in_pkts)
    d_out_pkts = _counter_delta(prev.out_pkts, cur.out_pkts)
    d_in_err = _counter_delta(prev.in_errors, cur.in_errors)
    d_out_err = _counter_delta(prev.out_errors, cur.out_errors)
    if None in (d_pkts, d_out_pkts, d_in_err, d_out_err):
        return None
    packets = d_pkts + d_out_pkts
    errors = d_in_err + d_out_err
    frames = packets + errors
    return errors / frames if packets > 0 and frames > 0 else None


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

        Columns are independent: a switch that answers ifHCInOctets and
        not ifHCOutOctets gets an inbound rate and None outbound, not a
        zero that reads as "no traffic".
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
            d_in = _octet_delta(prev.in_octets, cur.in_octets, cur.hc_in)
            d_out = _octet_delta(prev.out_octets, cur.out_octets, cur.hc_out)
            in_mbps = None if d_in is None else d_in * 8 / dt / 1e6
            out_mbps = None if d_out is None else d_out * 8 / dt / 1e6
            link_speed = speeds.get(if_index, 0)
            cap = link_speed * LINK_SPEED_MARGIN if link_speed > 0 else MAX_SANE_MBPS
            known = [r for r in (in_mbps, out_mbps) if r is not None]
            if known and max(known) > cap:
                # wraparound misfire or a reboot disguised as one
                log.debug(
                    "%s ifIndex %d: implausible rate %s/%s Mbit/s "
                    "(cap %.0f), raw octets in %s->%s out %s->%s — "
                    "baseline reset",
                    ip, if_index, in_mbps, out_mbps, cap,
                    prev.in_octets, cur.in_octets,
                    prev.out_octets, cur.out_octets,
                )
                continue

            per_min: list[float | None] = []
            for attr in (
                "in_errors", "out_errors", "in_discards", "out_discards"
            ):
                delta = _counter_delta(
                    getattr(prev, attr), getattr(cur, attr)
                )
                per_min.append(None if delta is None else delta * 60 / dt)
            sane = [r for r in per_min if r is not None]
            if sane and max(sane) > MAX_SANE_ERRORS_PER_MIN:
                log.debug(
                    "%s ifIndex %d: implausible error rate %s per minute "
                    "— baseline reset", ip, if_index, per_min,
                )
                continue

            rates = PortRates(
                ts=cur.ts,
                in_mbps=in_mbps,
                out_mbps=out_mbps,
                in_errors_per_min=per_min[0],
                out_errors_per_min=per_min[1],
                in_discards_per_min=per_min[2],
                out_discards_per_min=per_min[3],
                error_ratio=_error_ratio(prev, cur),
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
