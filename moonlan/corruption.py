"""Recognizing frame corruption by the MAC addresses it invents.

A port with a failing cable, patch cord or transmitter delivers frames
with flipped bits. The switch believes the damaged source address and
learns it: next to the real 20:7b:d5:1a:31:8d the MAC table grows
20:7b:d5:1a:31:9d (one bit away), 20:7b:d5:7a:34:07, 20:77:b5:7c:37:87.
Such an address never gets an IP, and used to settle in the database
as a device for the whole grace window.

The signature: an IP-less address the database has not accepted as a
device, a few bits away from one it had accepted — on the same switch.
Two devices with neighboring factory MACs do happen (a batch from one
vendor), but they answer ARP, and an IP takes an address out of the
candidates for good.

Until v0.8.5 the candidates were the addresses that would STILL be
unconfirmed after the poll. A copy seen for the second time was not
among them — it was about to be confirmed — so it was measured as a
sample instead of as a copy, and confirmed. Every copy that lingered in
the MAC table for two polls became a device, and copies of copies
became samples. The two sets are now taken apart: what may be a copy,
and what was real before this poll began.
"""

from __future__ import annotations

from collections import Counter

# A copy confirmed before v0.8.5 was seen in a poll or two: the damaged
# frame is gone, and so is the address. Past this many sightings a
# record is something that keeps coming back — not what this is about.
MAX_COPY_SIGHTINGS = 10

def mac_number(mac: str) -> int | None:
    """A MAC as a 48-bit number; None if it cannot be parsed."""
    try:
        parts = [int(part, 16) for part in mac.split(":")]
    except ValueError:
        return None
    if len(parts) != 6 or any(not 0 <= p <= 255 for p in parts):
        return None
    return int.from_bytes(bytes(parts), "big")


def hamming_distance(a: str, b: str) -> int:
    """Bits differing between two MACs; -1 if either cannot be parsed."""
    left, right = mac_number(a), mac_number(b)
    if left is None or right is None:
        return -1
    return (left ^ right).bit_count()


def _real(mac: str, row: dict | None, with_ip: set[str]) -> bool:
    """Accepted before this poll: confirmed, or holding an IP. A record
    marked as a damaged copy is not, whatever its confirmed flag says."""
    if mac in with_ip:
        return True
    if row is None or row.get("corrupt_of"):
        return False
    return bool(row.get("confirmed") or row.get("ip"))


def candidates(
    macs, db_rows: dict[str, dict], with_ip: set[str] | None = None
) -> set[str]:
    """The addresses of this poll that may be damaged copies.

    Unconfirmed in the database and without an IP, or not in it at
    all — whether or not this poll is about to confirm them. `with_ip`
    are the MACs this poll's ARP tied to an address: an IP vouches for
    a device even before it reaches the database.
    """
    with_ip = with_ip or set()
    return {
        mac for mac in macs
        if not _real(mac, db_rows.get(mac), with_ip)
    }


def samples_by_switch(
    hosts: list[dict], db_rows: dict[str, dict],
    with_ip: set[str] | None = None, grace_seconds: float = 0.0,
    now: float = 0.0,
) -> dict[str, set[str]]:
    """Switch ip -> the real addresses a copy on it may be made from.

    Real is decided by the database as it was BEFORE this poll (or by
    an IP). The address counts on any port of the switch, and not only
    while it is in this poll's table: a computer switched off leaves
    its copies behind for a while, and a device moved from port 3 to
    port 5 leaves them on port 3. The database's location counts for
    host_grace_hours — the same window that keeps a quiet device on
    the map.

    A copy on another switch is not measured against it: the switch
    that learns a damaged source address is the one the damaged frame
    arrived at, which is the switch of the sample itself.
    """
    with_ip = with_ip or set()
    samples: dict[str, set[str]] = {}
    for host in hosts:
        mac = host["mac"]
        if host.get("switch") and _real(mac, db_rows.get(mac), with_ip):
            samples.setdefault(host["switch"], set()).add(mac)
    for mac, row in db_rows.items():
        switch = row.get("switch_ip")
        if not switch or not _real(mac, row, with_ip):
            continue
        if now - row.get("last_seen", 0) <= grace_seconds:
            samples.setdefault(switch, set()).add(mac)
    return samples


def _nearest(mac: str, real: set[str], max_bits: int) -> tuple[int, str]:
    """(distance, sample) of the closest real address within max_bits,
    nearest first and then by address; (-1, "") when there is none."""
    number = mac_number(mac)
    if number is None:
        return -1, ""
    best = (-1, "")
    for sample in real:
        other = mac_number(sample)
        if other is None or other == number:
            continue
        distance = (number ^ other).bit_count()
        if distance > max_bits:
            continue
        if best[0] < 0 or (distance, sample) < best:
            best = (distance, sample)
    return best


def find_suspects(
    hosts: list[dict], candidates: set[str],
    samples: dict[str, set[str]], max_bits: int,
) -> dict[tuple[str, str], list[dict]]:
    """Groups the damaged copies by the port they are seen on.

    hosts are this poll's bound devices (switch, port, mac);
    candidates the addresses that may be copies (candidates());
    samples what was real on each switch (samples_by_switch()).
    Returns (switch ip, port) -> [{mac, sample, distance}], nearest
    first, only for ports that have at least one such address. The
    port is where the copies are — the alarm and the ports panel are
    about the port that hands the switch damaged frames.
    """
    if max_bits <= 0:
        return {}
    suspects: dict[tuple[str, str], list[dict]] = {}
    for host in hosts:
        mac = host["mac"]
        if mac not in candidates:
            continue
        distance, sample = _nearest(
            mac, samples.get(host["switch"], set()), max_bits
        )
        if distance > 0:
            suspects.setdefault((host["switch"], host["port"]), []).append(
                {"mac": mac, "sample": sample, "distance": distance}
            )
    for found in suspects.values():
        found.sort(key=lambda s: (s["distance"], s["mac"]))
    return suspects


def likely_copies(
    tables: dict[str, set[str]], candidates: set[str],
    samples: dict[str, set[str]], max_bits: int,
) -> set[str]:
    """The candidates that will most likely be found to be copies,
    known before the topology binds anything to a port.

    tables are switch ip -> the MACs in its table. A copy seen for the
    second time is not in the projection of unconfirmed addresses (it
    would have been confirmed), so without this it would vote on its
    port: three of them and the port behind a real device becomes "a
    switch without SNMP". Measured on every switch whose table has the
    address, against what the database knows on that switch; a wrong
    guess here costs one poll of not voting, nothing more.
    """
    if max_bits <= 0:
        return set()
    likely: set[str] = set()
    for switch, macs in tables.items():
        real = samples.get(switch)
        if not real:
            continue
        for mac in macs:
            if mac in candidates and mac not in likely:
                if _nearest(mac, real, max_bits)[0] > 0:
                    likely.add(mac)
    return likely


def _never_addressed(mac: str, row: dict, noted: set[str]) -> bool:
    """A confirmed record nothing but the MAC tables ever vouched for,
    seen in a few polls, and nobody's business: no IP, no ARP entry, no
    ping reply, no name; not watched, no label and no note. With no
    ARP entry ever, it was confirmed by sightings — ARP had nothing to
    confirm it with."""
    return (
        bool(row.get("confirmed"))
        and bool(row.get("switch_ip"))
        and not row.get("ip")
        and not row.get("last_arp")
        and not row.get("ip_confirmed")
        and not row.get("last_ping_ok")
        and not row.get("name")
        and not row.get("monitored")
        and row.get("seen_count", 0) <= MAX_COPY_SIGHTINGS
        and mac not in noted
    )


def confirmed_copies(
    rows: dict[str, dict], noted: set[str], max_bits: int, threshold: int,
) -> list[dict]:
    """The damaged copies the database confirmed before v0.8.5 held
    them back, in batches: one per address they are copies of.

    rows are the hosts table by MAC; noted the MACs with a label or a
    note on their node. A record counts if it never had an address
    (_never_addressed) and sits within max_bits of an address WITH an
    IP on the same switch — and only in a crowd: at least `threshold`
    of them around one address. Two real machines of one batch may
    differ by a bit, and one of them may have no IP; five IP-less
    neighbours each seen in a poll or two do not happen to a real
    batch.

    Records already marked count towards the crowd of their address,
    so a straggler joins its batch. Returns [{sample, switch, macs,
    ports}] of the records to mark now, `ports` the ports they were
    seen on, the most crowded first.
    """
    if max_bits <= 0 or threshold <= 0:
        return []
    addressed: dict[str, set[str]] = {}
    for mac, row in rows.items():
        if row.get("ip") and row.get("switch_ip"):
            addressed.setdefault(row["switch_ip"], set()).add(mac)
    crowds: dict[tuple[str, str], list[str]] = {}
    for mac, row in rows.items():
        if row.get("corrupt_of"):
            crowds.setdefault(
                (row["switch_ip"], row["corrupt_of"]), []
            ).append(mac)
        elif _never_addressed(mac, row, noted):
            distance, sample = _nearest(
                mac, addressed.get(row["switch_ip"], set()), max_bits
            )
            if distance > 0:
                crowds.setdefault((row["switch_ip"], sample), []).append(mac)
    batches: list[dict] = []
    for (switch, sample), macs in sorted(crowds.items()):
        if len(macs) < threshold:
            continue
        new = sorted(mac for mac in macs if not rows[mac].get("corrupt_of"))
        if not new:
            continue
        ports = Counter(rows[mac]["port"] for mac in new)
        batches.append({
            "sample": sample,
            "switch": switch,
            "macs": new,
            "ports": sorted(ports, key=lambda port: (-ports[port], port)),
        })
    return batches


def sample_mac(suspects: list[dict]) -> str:
    """The confirmed address most of the copies are distortions of."""
    counts: dict[str, int] = {}
    for s in suspects:
        counts[s["sample"]] = counts.get(s["sample"], 0) + 1
    return max(counts, key=lambda mac: (counts[mac], mac)) if counts else ""
