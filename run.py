#!/usr/bin/env python3
"""MoonLan entry point: python run.py"""

import errno
import socket
import sys

import uvicorn

from moonlan import https
from moonlan.config import load_config


def _ensure_port_free(host: str, port: int) -> None:
    """Trial bind: uvicorn hides the bind OSError inside itself."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # uvicorn binds with SO_REUSEADDR; without it the trial bind
            # fails on lingering TIME_WAIT sockets of a just-stopped server
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        sys.exit(
            f"Port {port} is already in use. Check whether MoonLan is "
            f"already running: ss -ltnp | grep {port}"
        )


def _tls_options(cfg) -> dict:
    """ssl_certfile / ssl_keyfile for uvicorn, checked first: a missing
    file or a key that does not fit the certificate is one line and an
    exit here, not a traceback from inside uvicorn."""
    if not cfg.listen_tls_cert and not cfg.listen_tls_key:
        return {}
    try:
        https.check_tls_files(cfg.listen_tls_cert, cfg.listen_tls_key)
    except https.TlsProblem as problem:
        sys.exit(str(problem))
    return {
        "ssl_certfile": cfg.listen_tls_cert,
        "ssl_keyfile": cfg.listen_tls_key,
    }


def main() -> None:
    # MOONLAN_CONFIG points at an alternative config.yaml, so a second
    # instance can be started in the project directory without taking
    # the running service's database and port with it
    cfg = load_config()
    tls = _tls_options(cfg)
    _ensure_port_free(cfg.listen_host, cfg.listen_port)
    if cfg.listen_http_redirect_port:
        if cfg.listen_http_redirect_port == cfg.listen_port:
            sys.exit(
                "listen.http_redirect_port must be another port than "
                "listen.port: the redirect listens beside the service"
            )
        _ensure_port_free(cfg.listen_host, cfg.listen_http_redirect_port)
    uvicorn.run(
        "moonlan.server:app",
        host=cfg.listen_host,
        port=cfg.listen_port,
        log_level="info",
        **tls,
    )


if __name__ == "__main__":
    main()
