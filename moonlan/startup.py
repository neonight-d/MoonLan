"""What the start says, and the port that only redirects (v0.8.12).

The lines the log opens with — the configuration, the settings, the
public address, the connection, the certificate — and the
listen.http_redirect_port listener. Moved out of server.py word for
word, the first stage of taking it apart (docs/ARCHITECTURE.md): what a
function needs from the process it is given, under the name of the
server.py global it stands for, so that no body changed. Nothing here
keeps state, and nothing here imports moonlan.server.
"""

from __future__ import annotations

import logging
import sys
import time
from collections import Counter
from pathlib import Path

from . import https, oui, passkeys, settings
from .textio import use_utf8

log = logging.getLogger("moonlan")

CERT_WARN_DAYS = 30


def _config_missing(*, config) -> dict | None:
    """Where config.yaml should be and the example to copy, when it is
    not there; None when it is"""
    report = config.report
    if report is None or report.exists:
        return None
    path = Path(report.path)
    return {
        "path": str(path),
        "dir": str(path.parent if str(path.parent) != "." else Path.cwd()),
        "example": str(Path(__file__).resolve().parent.parent / "config.example.yaml"),
    }


def _log_config(
    *, config, db, tools, vendors, settings_loaded, public_url,
    public_url_problems, trusted_proxies, trusted_proxy_problems,
    transport,
) -> None:
    """One line on how much of the configuration is actually yours —
    a config.yaml left over from an older version silently overrides
    settings whose defaults have changed since.

    Both paths are absolute on purpose. With MOONLAN_CONFIG in play
    there can be two instances running out of the same directory, and
    the log was the one place that could not say which file and which
    database each of them had opened.
    """
    report = config.report
    if report is None:
        return
    log.info(
        "Config file: %s%s", report.path,
        "" if report.exists else "  (not found — every setting is a default)",
    )
    if not report.exists:
        # in a container this is the first thing anyone sees (v0.8.3):
        # say where the file goes and what to start it from
        missing = _config_missing(config=config)
        log.warning(
            "No config.yaml: MoonLan runs on its defaults and polls no "
            "switch. Put one at %s — %s is the template to copy.",
            missing["path"], missing["example"],
        )
    log.info(
        "Database:    %s%s", Path(db.path).resolve() if db.path != ":memory:"
        else "in memory (demo mode)",
        "" if not config.demo else "  — the real inventory is not touched",
    )
    # the keys that are Settings now are counted there, not here
    read = [v for v in report.values if v[0] not in settings.MOVED_PATHS]
    summary = (
        f"Config: {sum(1 for v in read if v[2] == 'config.yaml')} from "
        f"{report.path}, {sum(1 for v in read if v[2] == 'default')} defaults"
    )
    if report.unknown:
        log.warning(
            "%s; unknown keys ignored: %s — check them with "
            "python -m moonlan.diag --config",
            summary, ", ".join(report.unknown),
        )
    else:
        log.info("%s", summary)
    for problem in report.problems:
        log.warning("config.yaml switches: %s", problem)
    for problem in report.menu_problems:
        log.warning("config.yaml context_menu: %s", problem)
    _log_settings(settings_loaded=settings_loaded, db=db)
    _log_public_url(
        config=config, public_url=public_url,
        public_url_problems=public_url_problems,
        trusted_proxies=trusted_proxies,
        trusted_proxy_problems=trusted_proxy_problems,
        transport=transport,
    )
    passkeys.log_state(public_url)
    oui.log_state(vendors)
    links = config.context_menu.links
    if links:
        log.info(
            "Node menu: %d link(s) of your own: %s", len(links),
            ", ".join(link.label for link in links),
        )
    if config.demo:
        log.info("Node menu: ping and traceroute are simulated in demo mode")
    else:
        trace = tools.get("traceroute")
        log.info(
            "Node menu: ping %s, traceroute %s",
            f"at {tools['ping']}" if tools.get("ping")
            else "NOT FOUND — the Ping item is disabled",
            f"({trace[0]}) at {trace[1]}" if trace
            else "NOT FOUND (neither traceroute nor tracepath) — the "
                 "Traceroute item is disabled",
        )
    starved = config.starved_counters()
    if starved:
        log.warning(
            "Poll budget at or above twice counters_interval_seconds "
            "(%d s) on %d switch(es): %s. While a scan holds one of "
            "these, its counters cycle is skipped, so its rates will "
            "visibly age between scans — the panel shows them with their "
            "age rather than hiding them. Lower host_budget_seconds for "
            "those devices, or raise counters_interval_seconds.",
            config.counters_interval_seconds, len(starved),
            ", ".join(f"{ip} ({budget} s)" for ip, budget in starved),
        )
    custom = config.custom_switches()
    if custom:
        log.info(
            "SNMP settings of their own on %d of %d switch(es): %s. "
            "Everything else is inherited from the snmp: section; "
            "python -m moonlan.diag --config prints which is which.",
            len(custom), len(config.switches),
            ", ".join(
                f"{ip} ("
                + ", ".join(sorted(config.switch_snmp[ip].explicit))
                + ")"
                for ip in custom
            ),
        )


def _setting_problem(error: dict) -> str:
    """An error of settings.check() in words, for the log"""
    code = error.get("code")
    if code == "range":
        return f"out of range {error['min']}…{error['max']}"
    if code == "type":
        return f"not a {error['kind']}"
    if code in ("email", "chat_id"):
        return f"{error['value']!r} is not an address"
    if code == "channel":
        return f"no channel {error['value']!r} (email, telegram, syslog)"
    if code == "too_many":
        return f"more than {error['max']} addresses"
    return code or "not valid"


def _log_settings(*, settings_loaded, db) -> None:
    """What the start did with the settings config.yaml still has
    (v0.8.4): moved into the database, or no longer read."""
    loaded = settings_loaded
    counts = Counter(
        "database" if key in db.settings() else "default"
        for key in settings.BY_KEY
    )
    log.info(
        "Settings: %d changed (in the database), %d defaults — the Actions "
        "menu → Settings", counts["database"], counts["default"],
    )
    if loaded.moved:
        log.info(
            "Settings: moved from config.yaml into the database: %s",
            ", ".join(loaded.moved),
        )
    for key, value, error in loaded.invalid:
        log.error(
            "config.yaml %s = %s: %s — not moved into Settings; the default "
            "applies", key, value, _setting_problem(error),
        )
    for key, value, error in loaded.stale:
        log.error(
            "Setting %s = %s in the database: %s — the default applies; set "
            "it again in Settings", key, value, _setting_problem(error),
        )
    if loaded.unused:
        log.warning(
            "In Settings now, so config.yaml's values are not used — remove "
            "them from config.yaml: %s", ", ".join(loaded.unused),
        )


def _log_public_url(
    *, config, public_url, public_url_problems, trusted_proxies,
    trusted_proxy_problems, transport,
) -> None:
    """Where people are expected to open the map, and what follows."""
    for problem in public_url_problems + trusted_proxy_problems:
        log.warning("config.yaml %s — ignored", problem)
    _log_transport(
        config=config, public_url=public_url,
        trusted_proxies=trusted_proxies, transport=transport,
    )
    if public_url is None:
        log.info(
            "Public address: listen.public_url is not set — signing in with "
            "a key is off; everything else works as before"
        )
        return
    why = https.passkey_problem(public_url)
    if why == "public_url_ip":
        log.warning(
            "Public address: %s is an IP address. A passkey is bound to a "
            "host name, never to an address: signing in with a key is off. "
            "Open the map by name (and set that name here) to use one.",
            public_url.origin,
        )
    elif why == "public_url_http":
        log.warning(
            "Public address: %s is plain http. Browsers offer WebAuthn only "
            "to a secure page (https, or localhost): signing in with a key "
            "is off.", public_url.origin,
        )
    else:
        log.info(
            "Public address: %s — passkeys are bound to %s",
            public_url.origin, public_url.rp_id,
        )


def _log_transport(*, config, public_url, trusted_proxies, transport) -> None:
    """One line: how the map reaches people."""
    if transport == "tls":
        log.info("Connection: HTTPS served by MoonLan itself (%s)",
                 config.listen_tls_cert)
    elif transport == "proxy":
        log.info(
            "Connection: behind a reverse proxy (%s) — it holds the HTTPS; "
            "X-Forwarded-For and X-Forwarded-Proto are believed from it alone",
            ", ".join(trusted_proxies),
        )
    else:
        log.info("Connection: plain HTTP — see docs/HTTPS.md")
        if public_url is not None and public_url.secure:
            log.warning(
                "listen.public_url is %s, but MoonLan neither serves TLS "
                "(listen.tls_cert) nor believes a proxy "
                "(listen.trusted_proxies): every request arrives as plain "
                "http, and signing in with a password is refused on it",
                public_url.origin,
            )
    if config.listen_hsts_max_age:
        log.info("HSTS: max-age=%d on every HTTPS answer",
                 config.listen_hsts_max_age)


def _log_certificate(*, config, public_url) -> https.CertInfo | None:
    """listen.tls_cert at startup: who it is for, until when, and what
    will go wrong with it — before a browser finds out."""
    if not config.listen_tls_cert:
        return
    try:
        certificate = https.read_certificate(config.listen_tls_cert)
    except (OSError, ValueError) as exc:
        log.error("TLS: could not read %s: %s", config.listen_tls_cert, exc)
        return
    days = (certificate.not_after - time.time()) / 86400
    ends = time.strftime("%Y-%m-%d", time.localtime(certificate.not_after))
    log.info(
        "TLS: serving %s — %s; names: %s; valid until %s",
        config.listen_tls_cert, certificate.subject,
        ", ".join(certificate.names) or "none", ends,
    )
    if days < 0:
        log.warning(
            "TLS: the certificate ENDED on %s. Browsers refuse the page; "
            "replace the file and restart MoonLan.", ends,
        )
    elif days < CERT_WARN_DAYS:
        log.warning(
            "TLS: the certificate ends on %s, in %d day(s). Replace the file "
            "and restart MoonLan — it is read at startup.", ends, int(days),
        )
    if public_url and not https.covers(certificate.names, public_url.host):
        log.warning(
            "TLS: the certificate does not name %s (it names: %s). Browsers "
            "will not accept it at listen.public_url, and signing in with a "
            "key will not work there.",
            public_url.host, ", ".join(certificate.names) or "nothing",
        )
    mode = https.key_mode_too_open(config.listen_tls_key)
    if mode is not None:
        log.warning(
            "TLS: the key %s is readable by others (mode %o) — chmod 600 it",
            config.listen_tls_key, mode,
        )
    return certificate


async def start_redirect_listener(*, config, public_url):
    """listen.http_redirect_port: a port that only sends people to the
    public address. Needs one to send them to."""
    port = config.listen_http_redirect_port
    if not port:
        return None
    if public_url is None:
        log.warning(
            "listen.http_redirect_port %d needs listen.public_url to send "
            "people to — not started", port,
        )
        return None
    try:
        listener = await https.serve_redirect(
            config.listen_host, port, public_url.origin
        )
    except OSError as exc:
        log.error("listen.http_redirect_port %d: %s — not started", port, exc)
        return None
    log.info("Port %d redirects every request to %s", port, public_url.origin)
    return listener


def configure_logging() -> None:
    """The service's log: "—" and "§" as they are, not \u2014 and \xa7,
    under any locale (v0.8.9). run.py makes the streams UTF-8 first;
    this covers `uvicorn moonlan.server:app` as well."""
    use_utf8(sys.stdout, sys.stderr)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
