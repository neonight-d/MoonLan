"""Ping and traceroute from the MoonLan machine, on request.

A browser cannot ping, and the point is to see the network from where
MoonLan sees it anyway. These run as the node menu's built-in actions:

- only against an address the service found itself — the caller hands
  in a node id, never an address (see server.py);
- as an argument list, with no shell in between, the way pinger.py
  has always run ping;
- under a timeout, and no more than `max_running` requests at a time —
  one more is refused with a reason rather than queued without end;
- without holding the HTTP request: a request starts a job and gets
  its id, and the page asks how it is going.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

PING_COUNT = 4
PING_TIMEOUT = 10.0
TRACE_TIMEOUT = 60.0
# Pings of one request running side by side, when it names several
PARALLEL_PINGS = 8
# How long a finished job can still be asked about
KEEP_FINISHED_SECONDS = 600
# Raw output kept per target, so one chatty tool cannot fill memory
OUTPUT_LIMIT = 16_000

ACTIONS = ("ping", "traceroute")


def find_tools() -> dict:
    """Which of the tools this machine has: {"ping": path|None,
    "traceroute": (name, path)|None}. traceroute first, tracepath if
    there is no traceroute."""
    trace = None
    for name in ("traceroute", "tracepath"):
        path = shutil.which(name)
        if path:
            trace = (name, path)
            break
    return {"ping": shutil.which("ping"), "traceroute": trace}


def ping_argv(ip: str) -> list[str]:
    # four echoes, two seconds for each reply, eight for the lot
    return ["ping", "-c", str(PING_COUNT), "-W", "2", "-w", "8", ip]


def trace_argv(tool: str, ip: str) -> list[str]:
    if tool == "tracepath":
        return ["tracepath", "-n", "-m", "20", ip]
    # one probe per hop, two seconds each, twenty hops: under the minute
    return ["traceroute", "-n", "-q", "1", "-w", "2", "-m", "20", ip]


def checked_address(value) -> str | None:
    """The address in canonical form, or None if it is not one.

    It comes from the service's own data, but it goes into a command
    line: nothing that is not an IP address gets that far, and nothing
    that could be read as an option ever does.
    """
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return None


async def run(argv: list[str], timeout: float) -> tuple[int | None, str, bool]:
    """(exit code, output, timed out). No shell: argv goes to exec as is."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return None, f"{argv[0]}: {exc}", False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        out, _ = await proc.communicate()
        return None, out.decode("utf-8", "replace")[:OUTPUT_LIMIT], True
    return proc.returncode, out.decode("utf-8", "replace")[:OUTPUT_LIMIT], False


_SENT = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
# iputils: "rtt min/avg/max/mdev = 0.041/0.052/0.063/0.009 ms",
# busybox: "round-trip min/avg/max = 0.041/0.052/0.063 ms"
_RTT = re.compile(r"min/avg/max\S* = ([\d.]+)/([\d.]+)/([\d.]+)")


def parse_ping(output: str) -> dict | None:
    """Loss and round-trip times out of ping's summary; None if there
    is no summary to read."""
    sent = _SENT.search(output)
    if not sent:
        return None
    transmitted, received = int(sent.group(1)), int(sent.group(2))
    result = {
        "sent": transmitted,
        "received": received,
        "loss": round(100 * (transmitted - received) / transmitted)
        if transmitted else 100,
        "min": None, "avg": None, "max": None,
    }
    rtt = _RTT.search(output)
    if rtt:
        result["min"], result["avg"], result["max"] = (
            float(rtt.group(1)), float(rtt.group(2)), float(rtt.group(3))
        )
    return result


class Busy(Exception):
    """Every slot is taken; the caller should say so, not wait."""


@dataclass
class Target:
    node: str
    name: str
    ip: str  # "" when the node has no address
    kind: str
    monitor: dict = field(default_factory=dict)
    status: str = "queued"  # queued|running|done|failed|timeout|no_ip
    result: dict | None = None
    output: str = ""

    def as_dict(self) -> dict:
        return {
            "node": self.node, "name": self.name, "ip": self.ip,
            "kind": self.kind, "monitor": self.monitor,
            "status": self.status, "result": self.result,
            "output": self.output,
        }


@dataclass
class Job:
    id: str
    action: str
    targets: list[Target]
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    tool: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id, "action": self.action, "tool": self.tool,
            "started": self.started, "finished": self.finished,
            "done": bool(self.finished),
            "targets": [t.as_dict() for t in self.targets],
        }


# (argv, timeout) -> (exit code, output, timed out); swapped in tests
# and in demo mode, where the addresses do not exist
Runner = Callable[[list[str], float], Awaitable[tuple[int | None, str, bool]]]


class Jobs:
    """The running and recently finished requests of this process."""

    def __init__(self, tools: dict, runner: Runner = run):
        self.tools = tools
        self.runner = runner
        self._jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()

    def running(self) -> int:
        return sum(1 for job in self._jobs.values() if not job.finished)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def start(self, action: str, targets: list[Target], limit: int) -> Job:
        """Starts a job in the background; Busy when `limit` are running."""
        self._prune()
        if self.running() >= limit:
            raise Busy()
        tool = "ping"
        if action == "traceroute":
            tool = self.tools["traceroute"][0]
        job = Job(id=uuid.uuid4().hex[:12], action=action,
                  targets=targets, tool=tool)
        self._jobs[job.id] = job
        task = asyncio.get_running_loop().create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job) -> None:
        try:
            if job.action == "ping":
                gate = asyncio.Semaphore(PARALLEL_PINGS)
                await asyncio.gather(
                    *(self._one(job, target, gate) for target in job.targets)
                )
            else:
                await self._one(job, job.targets[0], asyncio.Semaphore(1))
        finally:
            job.finished = time.time()

    async def _one(self, job: Job, target: Target, gate) -> None:
        address = checked_address(target.ip) if target.ip else None
        if not address:
            target.status = "no_ip"
            return
        async with gate:
            target.status = "running"
            if job.action == "ping":
                argv, timeout = ping_argv(address), PING_TIMEOUT
            else:
                argv, timeout = trace_argv(job.tool, address), TRACE_TIMEOUT
            code, output, timed_out = await self.runner(argv, timeout)
        target.output = output
        if job.action == "ping":
            target.result = parse_ping(output)
        # ping exits 1 when nothing answered: that is a result, not a
        # failure of the tool. 2 and up is the tool itself giving up.
        worked = (0, 1) if job.action == "ping" else (0,)
        if timed_out:
            target.status = "timeout"
        elif code not in worked:
            target.status = "failed"
        else:
            target.status = "done"

    def _prune(self) -> None:
        cutoff = time.time() - KEEP_FINISHED_SECONDS
        for job_id in [
            j.id for j in self._jobs.values()
            if j.finished and j.finished < cutoff
        ]:
            del self._jobs[job_id]
