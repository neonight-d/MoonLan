"""Recognizing frame corruption by the MAC addresses it invents.

A port with a failing cable, patch cord or transceiver delivers frames
with flipped bits. The switch believes the damaged source address and
learns it: next to the real 20:7b:d5:1a:31:8d the MAC table grows
20:7b:d5:1a:31:9d (one bit away), 20:7b:d5:7a:34:07, 20:77:b5:7c:37:87.
Such an address is seen for a poll or two, never gets an IP, and used
to settle in the database as a device for the whole grace window.

The signature is unmistakable: an unconfirmed, IP-less address a few
bits away from a confirmed one on the SAME port. Two devices with
neighboring factory MACs do happen (a batch from one vendor), but they
answer ARP and are confirmed, which takes them out of the candidate
set.
"""

from __future__ import annotations

POPCOUNT = bytes(bin(i).count("1") for i in range(256))


def hamming_distance(a: str, b: str) -> int:
    """Bits differing between two MACs; -1 if either cannot be parsed."""
    try:
        left = [int(part, 16) for part in a.split(":")]
        right = [int(part, 16) for part in b.split(":")]
    except ValueError:
        return -1
    if len(left) != 6 or len(right) != 6:
        return -1
    return sum(POPCOUNT[x ^ y] for x, y in zip(left, right))


def find_suspects(
    hosts: list[dict], pending: set[str], max_bits: int
) -> dict[tuple[str, str], list[dict]]:
    """Groups the distorted copies of confirmed MACs by switch port.

    hosts are this poll's bound devices (switch, port, mac); pending
    are the MACs not confirmed as devices — no IP and too few sightings.
    Returns (switch ip, port) -> [{mac, sample, distance}], nearest
    first, only for ports that have at least one such address.
    """
    if max_bits <= 0:
        return {}
    by_port: dict[tuple[str, str], list[dict]] = {}
    for host in hosts:
        by_port.setdefault((host["switch"], host["port"]), []).append(host)
    suspects: dict[tuple[str, str], list[dict]] = {}
    for key, group in by_port.items():
        confirmed = [h["mac"] for h in group if h["mac"] not in pending]
        if not confirmed:
            continue  # nothing on this port to be a distortion of
        found: list[dict] = []
        for host in group:
            mac = host["mac"]
            if mac not in pending:
                continue
            nearest = min(
                (
                    (hamming_distance(mac, real), real)
                    for real in confirmed
                ),
                default=(-1, ""),
            )
            distance, real = nearest
            if 0 < distance <= max_bits:
                found.append({"mac": mac, "sample": real, "distance": distance})
        if found:
            found.sort(key=lambda s: (s["distance"], s["mac"]))
            suspects[key] = found
    return suspects


def sample_mac(suspects: list[dict]) -> str:
    """The confirmed address most of the copies are distortions of."""
    counts: dict[str, int] = {}
    for s in suspects:
        counts[s["sample"]] = counts.get(s["sample"], 0) + 1
    return max(counts, key=lambda mac: (counts[mac], mac)) if counts else ""
