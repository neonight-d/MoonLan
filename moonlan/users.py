"""Accounts, from the server's shell: python -m moonlan.users

The first administrator is made here and nowhere else. A setup page in
the browser would make an administrator of whoever opened the map
first after the install — and in a school that is not necessarily the
teacher. Whoever has a shell on the MoonLan machine is its
administrator already; a pupil in the classroom is not.

The same goes for getting back in: a forgotten password, a lost phone
with the second factor on it, a lock after wrong passwords — all of it
is undone from here, with the service running (the database is in WAL
mode, and the service reads accounts from it on every request).

Passwords are asked for, never taken as arguments: a command line
stays in the shell history and is visible in `ps` to every user of the
machine.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from pathlib import Path

from . import auth
from .config import load_config
from .db import AccountError, Database

# The journal's "who" for what was done here. Account names cannot
# contain "@", so it is never mistaken for one.
CONSOLE = "@console"

PASSWORD_PROBLEMS = {
    "too_short": f"Too short: at least {auth.PASSWORD_MIN} characters.",
    "too_long": f"Too long: at most {auth.PASSWORD_MAX} characters.",
    "same_as_name": "The password cannot be the name.",
}


def explain(error: AccountError) -> str:
    if error.code == "exists":
        return (
            f"An account named {error.name!r} already exists (names are "
            f"the same in any letter case)."
        )
    if error.code == "unknown":
        return f"There is no account named {error.name!r}."
    if error.code == "unknown_key":
        return (f"{error.name} has no key with that number — see "
                f"python -m moonlan.users passkeys {error.name}")
    if error.code == "last_factor":
        return (
            f"This is the last second factor of {error.name}, an "
            f"administrator without TOTP. Bind TOTP or add another key "
            f"first — or, if every key is lost, remove them all:\n"
            f"  python -m moonlan.users reset-passkeys {error.name}\n"
            f"and a new second factor is bound at the next sign-in."
        )
    if error.code == "last_admin":
        return (
            f"{error.name} is the last enabled administrator. Without one, "
            f"sign-in switches off and the map is open to everyone on the "
            f"network. Make another administrator first:\n"
            f"  python -m moonlan.users role <name> admin"
        )
    return str(error)


def ask_password(name: str) -> str | None:
    """Asked twice, checked against the rules; three tries."""
    for _ in range(3):
        first = getpass.getpass(f"New password for {name}: ")
        problem = auth.password_problem(name, first)
        if problem:
            print(PASSWORD_PROBLEMS[problem])
            continue
        if getpass.getpass("Once more: ") != first:
            print("The two did not match.")
            continue
        return first
    return None


def journal(db: Database, event: str, name: str, **details) -> None:
    db.add_event(
        time.time(), event, name,
        json.dumps(details) if details else "", user=CONSOLE,
    )


def fmt_when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "never"


def _sessions_line(closed: int) -> str:
    return f" {closed} session(s) closed." if closed else ""


def cmd_add(db: Database, args) -> int:
    problem = auth.name_problem(args.name)
    if problem:
        print(
            "A name is letters, digits, '.', '_' and '-', up to 32 "
            "characters, no spaces."
        )
        return 1
    if db.user(args.name):
        raise AccountError("exists", args.name)
    password = ask_password(args.name)
    if password is None:
        return 1
    was_open = db.active_admins() == 0
    db.add_user(
        args.name, args.role, auth.hash_password(password),
        must_change=args.temporary,
    )
    journal(db, "user_added", args.name, role=args.role,
            temporary=args.temporary)
    print(f"{args.name}: added as {args.role}.")
    if args.temporary:
        print("  The password is temporary: a new one at the first sign-in.")
    if was_open and args.role == "admin":
        print(
            "\nSign-in is ON from now: every open map asks to sign in at "
            "its next request,\nno restart needed. An administrator binds "
            "a second factor (TOTP) at the first\nsign-in — or here, "
            "without sending the secret over the network:\n"
            f"  python -m moonlan.users totp {args.name}"
        )
    elif args.role == "admin":
        print(
            "  An administrator binds TOTP at the first sign-in, or here: "
            f"python -m moonlan.users totp {args.name}"
        )
    return 0


def cmd_list(db: Database, args) -> int:
    users = db.users()
    if not users:
        print(
            "No accounts. Sign-in is off: the map is open to everyone.\n"
            "Create the first administrator:\n"
            "  python -m moonlan.users add <name> --role admin"
        )
        return 0
    sessions = db.session_counts()
    now = time.time()
    keys = db.passkey_counts()
    print(
        f"{'name':<20} {'role':<7} {'state':<28} {'TOTP':<5} {'keys':>4} "
        f"{'codes':>5}  {'last sign-in':<16} {'sessions':>8}"
    )
    for row in users:
        if row["disabled"]:
            state = "disabled"
        elif row["locked_until"] > now:
            state = "locked until " + time.strftime(
                "%H:%M", time.localtime(row["locked_until"])
            )
        elif row["must_change"]:
            state = "new password at sign-in"
        else:
            state = "active"
        totp = "on" if row["totp_enabled"] else "off"
        if row["role"] == "admin" and not row["totp_enabled"] \
                and not keys.get(row["id"]):
            totp = "off!"
        print(
            f"{row['name']:<20} {row['role']:<7} {state:<28} {totp:<5} "
            f"{keys.get(row['id'], 0):>4} "
            f"{auth.recovery_left(row['recovery']):>5}  "
            f"{fmt_when(row['last_login']):<16} "
            f"{sessions.get(row['id'], 0):>8}"
        )
    if any(r["role"] == "admin" and not r["totp_enabled"]
           and not keys.get(r["id"]) for r in users):
        print(
            "\noff! = an administrator without a second factor (TOTP or a "
            "key): they bind one at\nthe next sign-in before anything "
            "else, or TOTP here with python -m moonlan.users totp <name>"
        )
    if db.active_admins() == 0:
        print(
            "\nNo enabled administrator: sign-in is OFF and the map is open "
            "to everyone."
        )
    return 0


def cmd_passwd(db: Database, args) -> int:
    row = db.user(args.name)
    if row is None:
        raise AccountError("unknown", args.name)
    password = ask_password(row["name"])
    if password is None:
        return 1
    closed = db.set_password(
        row["name"], auth.hash_password(password), must_change=args.temporary
    )
    journal(db, "user_password", row["name"], temporary=args.temporary,
            sessions=closed)
    print(f"{row['name']}: password changed.{_sessions_line(closed)}")
    if args.temporary:
        print("  The password is temporary: a new one at the next sign-in.")
    return 0


def cmd_role(db: Database, args) -> int:
    row = db.user(args.name)
    if row is None:
        raise AccountError("unknown", args.name)
    before, closed = db.set_role(row["name"], args.role)
    journal(db, "user_role", row["name"], role=args.role, was=before,
            sessions=closed)
    print(f"{row['name']}: {before} -> {args.role}.{_sessions_line(closed)}")
    return 0


def _set_disabled(db: Database, args, disabled: bool) -> int:
    row = db.user(args.name)
    if row is None:
        raise AccountError("unknown", args.name)
    closed = db.set_disabled(row["name"], disabled)
    journal(db, "user_disabled" if disabled else "user_enabled",
            row["name"], sessions=closed)
    print(
        f"{row['name']}: {'disabled' if disabled else 'enabled'}."
        f"{_sessions_line(closed)}"
    )
    return 0


def cmd_disable(db: Database, args) -> int:
    return _set_disabled(db, args, True)


def cmd_enable(db: Database, args) -> int:
    return _set_disabled(db, args, False)


def cmd_reset_totp(db: Database, args) -> int:
    row = db.user(args.name)
    if row is None:
        raise AccountError("unknown", args.name)
    closed = db.reset_totp(row["name"])
    journal(db, "user_totp_reset", row["name"], sessions=closed)
    print(
        f"{row['name']}: TOTP and recovery codes removed."
        f"{_sessions_line(closed)}"
    )
    if db.passkeys(row["id"]):
        print("  Their keys stay; python -m moonlan.users passkeys "
              f"{row['name']} lists them.")
    elif row["role"] == "admin":
        print(
            "  An administrator binds a new one at the next sign-in, or "
            f"here: python -m moonlan.users totp {row['name']}"
        )
    return 0


def cmd_totp(db: Database, args) -> int:
    """Binds TOTP here, on the server: the secret never crosses the
    network, which over plain HTTP is the safer way to do it."""
    row = db.user(args.name)
    if row is None:
        raise AccountError("unknown", args.name)
    secret = auth.new_totp_secret()
    codes = auth.new_recovery_codes()
    closed = db.enable_totp(
        row["name"], secret, auth.hash_recovery_codes(codes)
    )
    journal(db, "user_totp_enabled", row["name"], sessions=closed)
    uri = auth.otpauth_uri(row["name"], secret)
    grouped = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
    print(f"{row['name']}: TOTP is on.{_sessions_line(closed)}")
    if row["totp_enabled"]:
        print("  The previous secret stopped working just now.")
    print(
        f"\nSecret (base32):  {grouped}\n"
        f"otpauth line:     {uri}\n"
        f"\nAn app: add an account by hand with the secret, or turn the "
        f"otpauth line into a QR code.\nAn OATH hardware key:\n"
        f"  ykman oath accounts uri \"{uri}\"\n"
        f"\nRecovery codes — each works once, instead of a TOTP code. "
        f"Keep them somewhere safe;\nthey are not shown again:"
    )
    for code in codes:
        print(f"  {code}")
    if sys.stdin.isatty():
        answer = input(
            "\nThe code the app or key shows now, to check it "
            "(Enter to skip): "
        ).strip()
        if answer:
            if auth.match_totp(secret, answer, time.time()) is not None:
                print("The code matches.")
            else:
                print(
                    "The code does NOT match. Check that the clock of the "
                    "phone or the key is right,\nor bind again: "
                    f"python -m moonlan.users totp {row['name']}"
                )
    return 0


def cmd_passkeys(db: Database, args) -> int:
    """An account's keys, by the numbers the other commands take."""
    row = db.user(args.name)
    if row is None:
        raise AccountError("unknown", args.name)
    keys = db.passkeys(row["id"])
    if not keys:
        print(f"{row['name']} has no keys.")
        return 0
    print(f"{row['name']}: {len(keys)} key(s)")
    for key in keys:
        kind = ("signs in without a password" if key["passwordless"]
                else "a second factor after the password")
        kept = {1: "kept on the key", 0: "not kept on the key"}.get(
            key["discoverable"], "kept on the key: unknown")
        print(
            f"  {key['number']:>2}. {key['label'] or '(no name)'} — {kind}\n"
            f"      added {fmt_when(key['created_at'])}, last used "
            f"{fmt_when(key['last_used'])}, counter {key['sign_count']}\n"
            f"      {kept}; PIN or finger when added: "
            f"{'yes' if key['user_verified'] else 'no'}; transports: "
            f"{key['transports'] or '—'}; AAGUID: {key['aaguid'] or '—'}"
        )
    return 0


def cmd_remove_passkey(db: Database, args) -> int:
    row = db.user(args.name)
    if row is None:
        raise AccountError("unknown", args.name)
    key = next((k for k in db.passkeys(row["id"])
                if k["number"] == args.number), None)
    if key is None:
        raise AccountError("unknown_key", row["name"])
    removed, closed = db.remove_passkey(row["name"], key["id"])
    journal(db, "user_passkey_removed", row["name"], label=removed["label"],
            sessions=closed)
    print(f"{row['name']}: key {args.number} "
          f"({removed['label'] or 'no name'}) removed."
          f"{_sessions_line(closed)}")
    return 0


def cmd_reset_passkeys(db: Database, args) -> int:
    row = db.user(args.name)
    if row is None:
        raise AccountError("unknown", args.name)
    removed, closed = db.reset_passkeys(row["name"])
    journal(db, "user_passkeys_reset", row["name"], keys=removed,
            sessions=closed)
    print(f"{row['name']}: {removed} key(s) removed.{_sessions_line(closed)}")
    if row["role"] == "admin" and not row["totp_enabled"]:
        print(
            "  An administrator without TOTP binds a new second factor at "
            "the next sign-in,\n  or TOTP here: python -m moonlan.users "
            f"totp {row['name']}"
        )
    return 0


def cmd_unlock(db: Database, args) -> int:
    row = db.user(args.name)
    if row is None:
        raise AccountError("unknown", args.name)
    if db.unlock(row["name"]):
        journal(db, "user_unlocked", row["name"])
        print(f"{row['name']}: unlocked.")
    else:
        print(f"{row['name']} was not locked.")
    return 0


def cmd_delete(db: Database, args) -> int:
    row = db.user(args.name)
    if row is None:
        raise AccountError("unknown", args.name)
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not deleting without --yes when nobody can be asked.")
            return 1
        answer = input(f"Delete {row['name']} for good? [y/N] ").strip()
        if answer.lower() not in ("y", "yes"):
            print("Nothing deleted.")
            return 1
    closed = db.delete_user(row["name"])
    journal(db, "user_deleted", row["name"], role=row["role"],
            sessions=closed)
    print(f"{row['name']}: deleted.{_sessions_line(closed)}")
    return 0


COMMANDS = {
    "add": cmd_add,
    "list": cmd_list,
    "passwd": cmd_passwd,
    "role": cmd_role,
    "disable": cmd_disable,
    "enable": cmd_enable,
    "reset-totp": cmd_reset_totp,
    "totp": cmd_totp,
    "unlock": cmd_unlock,
    "delete": cmd_delete,
    "passkeys": cmd_passkeys,
    "remove-passkey": cmd_remove_passkey,
    "reset-passkeys": cmd_reset_passkeys,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m moonlan.users",
        description=(
            "MoonLan accounts. Run it where MoonLan runs, with the same "
            "config.yaml (or MOONLAN_CONFIG): it works on the database "
            "that file names, with the service running."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="create an account (asks for a password)")
    add.add_argument("name")
    add.add_argument("--role", choices=auth.ROLES, default="viewer",
                     help="viewer (the default), user or admin")
    add.add_argument("--temporary", action="store_true",
                     help="a new password is required at the first sign-in")
    sub.add_parser("list", help="every account and its state")
    passwd = sub.add_parser("passwd", help="set a new password")
    passwd.add_argument("name")
    passwd.add_argument("--temporary", action="store_true",
                        help="a new password is required at the next sign-in")
    role = sub.add_parser("role", help="change the role")
    role.add_argument("name")
    role.add_argument("role", choices=auth.ROLES)
    for command, text in (
        ("disable", "no sign-in until enabled again"),
        ("enable", "allow sign-in again"),
        ("reset-totp", "remove TOTP and the recovery codes"),
        ("totp", "bind TOTP here and print the secret"),
        ("unlock", "lift a lock after wrong passwords"),
        ("passkeys", "list the keys, with their numbers"),
        ("reset-passkeys", "remove every key (closes sessions)"),
    ):
        one = sub.add_parser(command, help=text)
        one.add_argument("name")
    remove = sub.add_parser("remove-passkey",
                            help="remove one key (closes sessions)")
    remove.add_argument("name")
    remove.add_argument("number", type=int,
                        help="as python -m moonlan.users passkeys shows it")
    delete = sub.add_parser("delete", help="delete an account")
    delete.add_argument("name")
    delete.add_argument("--yes", action="store_true",
                        help="do not ask for confirmation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    path = Path(cfg.users_db_path())
    new = not path.exists()
    print(
        f"Accounts: {path.resolve()}"
        + ("  (demo mode: the demo's accounts, not the real ones)"
           if cfg.demo else "")
    )
    if new:
        print(
            "  This file does not exist yet and will be created. If MoonLan "
            "runs elsewhere,\n  run this in its directory or point "
            "MOONLAN_CONFIG at its config.yaml."
        )
    db = Database(path)
    try:
        return COMMANDS[args.command](db, args)
    except AccountError as error:
        print(explain(error))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
