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

from .snmp_collector import SnmpCollector

log = logging.getLogger(__name__)

OID_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"    # ifHCInOctets (64-bit)
OID_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"  # ifHCOutOctets (64-bit)
OID_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"         # ifInOctets (32-bit fallback)
OID_OUT_OCTETS = "1.3.6.1.2.1.2.2.1.16"        # ifOutOctets (32-bit fallback)
OID_IN_ERRORS = "1.3.6.1.2.1.2.2.1.14"         # ifInErrors
OID_OUT_ERRORS = "1.3.6.1.2.1.2.2.1.20"        # ifOutErrors
OID_IN_DISCARDS = "1.3.6.1.2.1.2.2.1.13"       # ifInDiscards
OID_OUT_DISCARDS = "1.3.6.1.2.1.2.2.1.19"      # ifOutDiscards

HISTORY_POINTS = 60  # ring buffer length per port
WRAP32 = 2 ** 32
# A wrap-corrected delta after a reboot can look like an absurd rate;
# nothing in a LAN legitimately exceeds this
MAX_SANE_MBPS = 100_000


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
    hc: bool = True  # octet counters are 64-bit (no wraparound possible)


@dataclass
class PortRates:
    """Rates computed from the delta between two samples."""

    ts: float
    in_mbps: float
    out_mbps: float
    errors_per_min: float    # ifInErrors + ifOutErrors
    discards_per_min: float  # ifInDiscards + ifOutDiscards


async def collect_samples(collector: SnmpCollector, host: str) -> dict[int, Sample]:
    """One counters poll of a switch: ifIndex -> Sample.

    Prefers 64-bit ifHC* octet counters; if the switch has none,
    falls back to the 32-bit ones (marked hc=False for wraparound
    handling). Uses the collector's low-level walk — the counters loop
    deliberately shares the SNMP transport with the topology scan.
    """
    ts = time.time()
    samples: dict[int, Sample] = {}

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

    return samples


def _octet_delta(prev: int, cur: int, hc: bool) -> int | None:
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

    def update(self, ip: str, samples: dict[int, Sample]) -> dict[int, PortRates]:
        """Applies a fresh poll; returns the rates computed this cycle."""
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
            if max(in_mbps, out_mbps) > MAX_SANE_MBPS:
                continue  # a reboot disguised as a 32-bit wraparound
            rates = PortRates(
                ts=cur.ts,
                in_mbps=in_mbps,
                out_mbps=out_mbps,
                errors_per_min=(error_deltas[0] + error_deltas[1]) * 60 / dt,
                discards_per_min=(error_deltas[2] + error_deltas[3]) * 60 / dt,
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
