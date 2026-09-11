"""Loop Detection (LBD) read from the vendors' private MIBs.

There is no standard MIB for this. Every vendor keeps loopback
detection in its own branch under `1.3.6.1.4.1.<enterprise>`, with its
own structure, its own enumerations and its own idea of what "no loop"
looks like. So the OIDs are not written into the code: a *profile*
describes one model family — which sysObjectID it answers with, where
its branch starts, the suffixes of the four scalars and the three
columns, and which raw status value means the port is fine. Profiles
ship built in and can be added or replaced from config.yaml.

The one rule worth stating on its own: **the normal value is known,
everything else is a loop**. Only "no loop" has been observed on live
hardware (`1` on the DGS/DES-1210 family, the string `None` on the
DES-3526); what these agents report during a real loop was never seen,
and nobody is going to make a loop in a working network to find out.
Treating an unknown value as "probably fine" would mean the first real
loop passes in silence — so it raises the alarm instead, with the raw
value in the text. The first loop then documents itself.

A port whose status did not arrive is *unknown*, never "normal": that
is the same discipline the counters and the STP verdict follow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .snmpval import as_octets, is_octets

log = logging.getLogger(__name__)

OID_SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"

STATUS_INTEGER = "integer"
STATUS_STRING = "string"


@dataclass(frozen=True)
class LoopProfile:
    """How to read loop detection off one family of switches.

    `roots` maps a sysObjectID prefix to the branch root to use for it.
    One profile can cover several roots when the structure below them
    is identical — which is exactly the case for the D-Link 1210
    family: the DGS-1210-26 and the DES-1210-28/ME keep the same seven
    objects under two different branches.
    """

    name: str
    roots: dict[str, str]
    # scalars, as suffixes of the root
    enabled: str
    mode: str
    interval: str
    recover_time: str
    # columns, as suffixes of the root; the row index is the port number
    col_index: str
    col_enabled: str
    col_status: str
    status_type: str = STATUS_INTEGER
    # raw status values that mean "this port is fine"
    normal: tuple[str, ...] = ("1",)

    def scalar(self, root: str, suffix: str) -> str:
        return f"{root}.{suffix}"

    def column(self, root: str, suffix: str) -> str:
        return f"{root}.{suffix}"


# The three families found by walking the live network on 2026-09-11
# (docs/diag/walk-*-full-2026-09-11.txt). The port layout each one
# reports was checked against the operator's own list of ports with
# LBD switched off, on all four switches, before any of it was
# written down here.
BUILTIN_PROFILES: tuple[LoopProfile, ...] = (
    LoopProfile(
        name="dlink-1210",
        roots={
            # DGS-1210-26 Rev.F1
            "1.3.6.1.4.1.171.11.153.1000": "1.3.6.1.4.1.171.11.153.1000.17",
            # DES-1210-28/ME
            "1.3.6.1.4.1.171.10.75.15.2": "1.3.6.1.4.1.171.10.75.15.2.17",
        },
        enabled="1.0", mode="2.0", interval="3.0", recover_time="4.0",
        col_index="5.1.1", col_enabled="5.1.2", col_status="5.1.3",
        status_type=STATUS_INTEGER, normal=("1",),
    ),
    LoopProfile(
        name="dlink-des3526",
        roots={"1.3.6.1.4.1.171.11.64.1": "1.3.6.1.4.1.171.11.64.1.2.12"},
        # the scalars sit one level deeper here, and interval and mode
        # are the other way round
        enabled="1.1.0", mode="1.4.0", interval="1.2.0",
        recover_time="1.3.0",
        col_index="2.1.1.1", col_enabled="2.1.1.2", col_status="2.1.1.3",
        status_type=STATUS_STRING, normal=("None",),
    ),
)

ENABLED = 1    # both families: 1 = enabled, 2 = disabled
DISABLED = 2


@dataclass
class LoopPortState:
    """One row of the vendor's per-port loop table."""

    port: int                      # the index the vendor table uses
    if_index: int | None = None    # …resolved through the port table
    lbd_enabled: bool | None = None
    status_raw: str = ""
    looped: bool | None = None     # None — the status never arrived

    @property
    def known(self) -> bool:
        return self.looped is not None


@dataclass
class LoopDetectionData:
    """Loop detection of one switch, or the honest absence of it."""

    supported: bool = False
    profile: str = ""
    matched_by: str = ""       # "sysObjectID" | "probe"
    sys_object_id: str = ""
    root: str = ""
    enabled: bool | None = None
    mode: int | None = None
    interval: int | None = None
    recover_time: int | None = None
    ports: dict[int, LoopPortState] = field(default_factory=dict)
    # the vendor table and the interface table disagree on how many
    # ports this switch has, so the mapping cannot be trusted
    index_mismatch: bool = False
    rows: int = 0              # rows the vendor table returned
    unmapped: list[int] = field(default_factory=list)
    error: str = ""
    truncated: bool = False

    @property
    def status(self) -> str:
        """One word for the UI and the log."""
        if not self.supported:
            return "unsupported"
        if self.looped_ports():
            return "loop"
        if self.enabled is False:
            return "disabled"
        if self.truncated or self.index_mismatch:
            return "partial"
        return "ok"

    def looped_ports(self) -> list[LoopPortState]:
        return [
            p for p in self.ports.values()
            if p.looped and p.lbd_enabled is not False
        ]

    def port(self, if_index: int) -> LoopPortState | None:
        return self.ports.get(if_index)


def parse_profiles(entries: list[dict]) -> tuple[list[LoopProfile], list[str]]:
    """config.yaml `loop_detection.profiles` -> profiles and complaints.

    A profile whose name repeats a built-in one replaces it; any other
    name is added. A malformed entry is skipped with its reason
    returned, because a typo here must not look like a switch that
    stopped answering.
    """
    profiles: list[LoopProfile] = []
    problems: list[str] = []
    for n, entry in enumerate(entries or [], 1):
        if not isinstance(entry, dict):
            problems.append(f"profile #{n} is not a mapping")
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            problems.append(f"profile #{n} has no name")
            continue
        roots = entry.get("roots") or {}
        if not isinstance(roots, dict) or not roots:
            problems.append(f"profile {name!r} has no roots")
            continue
        scalars = entry.get("scalars") or {}
        columns = entry.get("columns") or {}
        missing = [
            key for key in ("enabled", "mode", "interval", "recover_time")
            if not scalars.get(key)
        ] + [
            key for key in ("index", "lbd_enabled", "status")
            if not columns.get(key)
        ]
        if missing:
            problems.append(
                f"profile {name!r} is missing {', '.join(missing)}"
            )
            continue
        normal = entry.get("normal")
        if normal is None:
            normal_values: tuple[str, ...] = ("1",)
        elif isinstance(normal, (list, tuple)):
            normal_values = tuple(str(v) for v in normal)
        else:
            normal_values = (str(normal),)
        profiles.append(LoopProfile(
            name=name,
            roots={str(k).strip(): str(v).strip() for k, v in roots.items()},
            enabled=str(scalars["enabled"]),
            mode=str(scalars["mode"]),
            interval=str(scalars["interval"]),
            recover_time=str(scalars["recover_time"]),
            col_index=str(columns["index"]),
            col_enabled=str(columns["lbd_enabled"]),
            col_status=str(columns["status"]),
            status_type=(
                STATUS_STRING
                if str(entry.get("status_type", STATUS_INTEGER)).lower()
                == STATUS_STRING else STATUS_INTEGER
            ),
            normal=normal_values,
        ))
    return profiles, problems


def merge_profiles(
    builtin: tuple[LoopProfile, ...], extra: list[LoopProfile]
) -> list[LoopProfile]:
    """Config profiles first: a repeated name replaces the built-in."""
    by_name = {p.name: p for p in builtin}
    for profile in extra:
        by_name[profile.name] = profile
    # config entries come first so an override is tried before the
    # built-in roots of another family can claim the switch
    names = [p.name for p in extra]
    names += [p.name for p in builtin if p.name not in names]
    return [by_name[name] for name in names]


def candidates(
    profiles: list[LoopProfile], sys_object_id: str
) -> list[tuple[LoopProfile, str, str]]:
    """(profile, root, how) to try, in order.

    The declared way in is sysObjectID, which is what the profiles are
    keyed by. Everything else is tried afterwards as a probe — one GET
    of a scalar that either answers or does not. A vendor branch is
    not a thing another vendor also implements, so an answer from one
    is an identification; and a model whose sysObjectID nobody has
    written down yet still gets its loops watched.
    """
    ordered: list[tuple[LoopProfile, str, str]] = []
    seen: set[tuple[str, str]] = set()
    if sys_object_id:
        for profile in profiles:
            for prefix, root in profile.roots.items():
                if sys_object_id == prefix or sys_object_id.startswith(
                    prefix + "."
                ):
                    ordered.append((profile, root, "sysObjectID"))
                    seen.add((profile.name, root))
    for profile in profiles:
        for root in profile.roots.values():
            if (profile.name, root) in seen:
                continue
            seen.add((profile.name, root))
            ordered.append((profile, root, "probe"))
    return ordered


def status_text(value, status_type: str) -> str:
    """The raw status as text, whatever type the agent used for it.

    Printed into the alarm exactly as it arrived: the value a real loop
    produces has never been observed, and the first one to happen
    should leave its own record.
    """
    if value is None:
        return ""
    if status_type == STATUS_STRING or is_octets(value):
        return as_octets(value).decode("utf-8", "replace").strip("\x00").strip()
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value).strip()


def judge_port(state: LoopPortState, normal: tuple[str, ...]) -> LoopPortState:
    """Fills in `looped` — "the normal value is known, the rest is a loop"."""
    if not state.status_raw:
        state.looped = None
    else:
        state.looped = state.status_raw not in normal
    return state


def map_ports(
    rows: dict[int, LoopPortState], physical: set[int]
) -> tuple[dict[int, LoopPortState], list[int], bool]:
    """Vendor row index -> ifIndex, through the interface table.

    On these models the index IS the ifIndex of the physical port, but
    that is a property of the firmware and not a law; a row whose
    number is not a physical interface is dropped rather than attached
    to whatever port happens to hold that ifIndex. A vendor table of a
    different length than the port table means the assumption itself
    has failed, and the whole mapping is flagged.
    """
    mapped: dict[int, LoopPortState] = {}
    unmapped: list[int] = []
    for index, state in sorted(rows.items()):
        if index in physical:
            state.if_index = index
            mapped[index] = state
        else:
            unmapped.append(index)
    mismatch = bool(unmapped) or len(rows) != len(physical)
    return mapped, unmapped, mismatch


async def collect_loop_detection(
    collector,
    host: str,
    ports: dict,
    profiles: list[LoopProfile] | None = None,
    sys_object_id: str | None = None,
) -> LoopDetectionData:
    """Reads loop detection off one switch, or says it cannot.

    `ports` is the interface table of the last scan (ifIndex ->
    PortInfo); it is what the vendor row numbers are resolved against.
    """
    profiles = list(profiles or BUILTIN_PROFILES)
    data = LoopDetectionData()
    if sys_object_id is None:
        raw = await collector._get(host, OID_SYS_OBJECT_ID)
        sys_object_id = str(raw) if raw is not None else ""
    data.sys_object_id = sys_object_id or ""

    chosen: tuple[LoopProfile, str, str] | None = None
    for profile, root, how in candidates(profiles, data.sys_object_id):
        value = await collector._get(host, profile.scalar(root, profile.enabled))
        if value is None:
            continue
        chosen = (profile, root, how)
        try:
            data.enabled = int(value) == ENABLED
        except (TypeError, ValueError):
            data.enabled = None
        break
    if chosen is None:
        log.debug(
            "%s: no loop-detection profile answers (sysObjectID %s)",
            host, data.sys_object_id or "unknown",
        )
        return data

    profile, root, how = chosen
    data.supported = True
    data.profile = profile.name
    data.matched_by = how
    data.root = root

    for attr, suffix in (
        ("mode", profile.mode),
        ("interval", profile.interval),
        ("recover_time", profile.recover_time),
    ):
        value = await collector._get(host, profile.scalar(root, suffix))
        if value is None:
            continue
        try:
            setattr(data, attr, int(value))
        except (TypeError, ValueError):
            pass

    rows: dict[int, LoopPortState] = {}

    def row(index: int) -> LoopPortState:
        state = rows.get(index)
        if state is None:
            state = rows[index] = LoopPortState(port=index)
        return state

    enabled_oid = profile.column(root, profile.col_enabled)
    status_oid = profile.column(root, profile.col_status)
    index_oid = profile.column(root, profile.col_index)

    # The index column is walked too, even though the row suffix
    # already carries the number: an agent that numbers its rows
    # 1..N while reporting other port numbers in the column would
    # otherwise go unnoticed.
    async for suffix, value in collector._walk(host, index_oid):
        if suffix:
            row(suffix[0])
    async for suffix, value in collector._walk(host, enabled_oid):
        if not suffix:
            continue
        try:
            row(suffix[0]).lbd_enabled = int(value) == ENABLED
        except (TypeError, ValueError):
            pass
    async for suffix, value in collector._walk(host, status_oid):
        if not suffix:
            continue
        row(suffix[0]).status_raw = status_text(value, profile.status_type)

    for state in rows.values():
        judge_port(state, profile.normal)

    status_walk = collector.last_walk_status(host, status_oid)
    data.error = status_walk.error
    data.truncated = status_walk.truncated
    data.rows = len(rows)

    physical = {
        p.if_index for p in ports.values()
        if getattr(p, "is_physical", True) and p.if_index > 0
    }
    data.ports, data.unmapped, data.index_mismatch = map_ports(rows, physical)
    return data


def describe(data: LoopDetectionData) -> str:
    """One line for the log and for `diag --loop`."""
    if not data.supported:
        return (
            f"no profile answers (sysObjectID "
            f"{data.sys_object_id or 'unknown'})"
        )
    parts = [f"profile {data.profile} via {data.matched_by}"]
    if data.enabled is False:
        parts.append("LBD is switched off globally")
    else:
        parts.append(
            f"interval {data.interval}s, recovery {data.recover_time}s"
        )
    on = sum(1 for p in data.ports.values() if p.lbd_enabled)
    parts.append(f"{on} of {len(data.ports)} port(s) watched")
    looped = data.looped_ports()
    if looped:
        parts.append(
            "LOOP on " + ", ".join(
                f"{p.if_index} ({p.status_raw})" for p in looped
            )
        )
    if data.index_mismatch:
        parts.append("port numbering does not match the interface table")
    if data.truncated:
        parts.append("the status column stopped answering partway")
    return "; ".join(parts)
