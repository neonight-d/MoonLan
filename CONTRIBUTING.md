# Contributing to MoonLan

Thanks for contributing to MoonLan. The project values small, testable changes that preserve the distinction between observed network state and inferred topology.

## Development setup

~~~bash
git clone https://github.com/ItsWanheda/MoonLan.git
cd MoonLan
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
~~~

## Before changing code

1. Read the relevant module and its tests.
2. Identify the invariant the change must preserve.
3. Prefer a regression test before changing behavior.
4. Keep public class names, API routes, and data shapes stable unless the change is intentionally breaking.

## Testing

Run the complete suite:

~~~bash
python -m unittest discover -s tests -v
~~~

For topology changes, test both the normal case and the ambiguous or partial-data case.

For SNMP changes, remember that successful observation, partial observation, stale saved reading, timeout, unreachable device, and unsupported OID are different states.

## Documentation

Update documentation when you change configuration, API behavior, topology rules, operator-visible UI behavior, alarms, or diagnostic commands. Release-facing behavior belongs in CHANGELOG.md.

## Commit messages

Use short Conventional Commit-style messages:

~~~text
feat: add ...
fix: preserve ...
perf: reduce ...
test: cover ...
docs: clarify ...
refactor: simplify ...
ci: update ...
chore: bump ...
~~~

Describe the actual change, not the intention.

Good:

~~~text
fix: keep stale FDB readings out of host confirmations
~~~

Avoid vague messages such as "update stuff" or "final fix".

## Pull requests

A good PR should contain a short problem statement, implementation summary, tests run, compatibility notes when relevant, and screenshots for meaningful UI changes.

Keep unrelated formatting churn out of code changes.

## Design rule

MoonLan should be conservative when evidence is incomplete.

**Never make an inference look like a measurement.**
