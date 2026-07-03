#!/usr/bin/env python3
"""Точка входа MoonLan: python run.py"""

import uvicorn

from moonlan.config import load_config


def main() -> None:
    cfg = load_config()
    uvicorn.run(
        "moonlan.server:app",
        host=cfg.listen_host,
        port=cfg.listen_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
