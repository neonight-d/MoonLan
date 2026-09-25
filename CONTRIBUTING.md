# Contributing to MoonLan

Thanks for contributing to MoonLan. The project values small, testable changes that preserve the distinction between observed network state and inferred topology.

## Development setup

~~~bash
git clone https://github.com/neonight-d/MoonLan.git
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

Update documentation when you change configuration, API behavior, topology rules, operator-visible UI behavior, alarms, or diagnostic commands.

Do not edit `CHANGELOG.md`, `CHANGELOG_RU.md` or the version number in `moonlan/__init__.py`. The maintainer writes the changelog and sets the version when a release is cut; describe the user-visible effect of your change in the pull request instead, and it will be carried into the changelog from there.

## Commit messages

Any clear message is fine. The project's own history uses a short sentence that says what is now true, for example:

~~~text
A pinned node moves under the mouse
Releasing a node does not erase where it is
~~~

Conventional Commit prefixes (`fix:`, `docs:`, …) are welcome too. Either way, describe the actual change, not the intention — avoid messages such as "update stuff" or "final fix".

## Pull requests

A good PR should contain a short problem statement, implementation summary, tests run, compatibility notes when relevant, and screenshots for meaningful UI changes.

Keep unrelated formatting churn out of code changes.

## Design rule

MoonLan should be conservative when evidence is incomplete.

**Never make an inference look like a measurement.**
