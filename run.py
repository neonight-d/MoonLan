#!/usr/bin/env python3
"""Точка входа MoonLan: python run.py"""

import errno
import socket
import sys

import uvicorn

from moonlan.config import load_config


def _ensure_port_free(host: str, port: int) -> None:
    """Пробное занятие порта: uvicorn прячет OSError при bind внутри себя."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        sys.exit(
            f"Порт {port} занят. Проверьте, не запущен ли MoonLan уже: "
            f"ss -ltnp | grep {port}"
        )


def main() -> None:
    cfg = load_config()
    _ensure_port_free(cfg.listen_host, cfg.listen_port)
    uvicorn.run(
        "moonlan.server:app",
        host=cfg.listen_host,
        port=cfg.listen_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
