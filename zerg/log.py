"""Tiny logger."""

from __future__ import annotations

from typing import Any


def zlog(spider: str, message: str, *args: Any) -> None:
    """Print ``[异虫:<spider>] ...``."""
    if args:
        try:
            message = message % args
        except Exception:
            message = f"{message} {args}"
    print(f"[异虫:{spider}] {message}")
