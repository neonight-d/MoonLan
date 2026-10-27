"""Is the service up: GET /api/health on this machine, where config.yaml
says it listens — Docker's HEALTHCHECK (v0.8.3), with no curl in the image.

    python -m moonlan.healthcheck

Exit 0 when the service answers {"status": "ok"}, 1 otherwise. Over
HTTPS the certificate is not checked: it is the service's own, often
self-signed, and made out for the name people use, not for localhost.
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.request

from .config import load_config


def health_url(cfg) -> str:
    """The address to ask: a service listening everywhere is asked on
    the loopback, one bound to an address on that address"""
    host = cfg.listen_host or "0.0.0.0"
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    scheme = "https" if cfg.listen_tls_cert else "http"
    return f"{scheme}://{host}:{cfg.listen_port}/api/health"


def healthy(url: str, timeout: float = 5.0) -> bool:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as answer:
            return answer.status == 200 and json.load(answer).get("status") == "ok"
    except (OSError, ValueError):
        return False


def main() -> int:
    return 0 if healthy(health_url(load_config())) else 1


if __name__ == "__main__":
    sys.exit(main())
