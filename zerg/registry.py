"""Discover spiders under a package."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Type

from zerg.spider import Spider


def _iter_modules(package: str) -> list[str]:
    try:
        pkg = importlib.import_module(package)
    except ImportError:
        return []

    if not hasattr(pkg, "__path__"):
        return [package]

    names: list[str] = []
    for mod in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
        if mod.ispkg:
            continue
        base = mod.name.rsplit(".", 1)[-1]
        if base.startswith("_"):
            continue
        names.append(mod.name)
    return names


def discover(package: str = "spiders") -> dict[str, Type[Spider]]:
    """Return ``{name: SpiderClass}``."""
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    found: dict[str, Type[Spider]] = {}
    for modname in _iter_modules(package):
        try:
            mod: ModuleType = importlib.import_module(modname)
        except Exception as e:
            print(f"[异虫.registry] skip {modname}: {e}")
            continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if not issubclass(obj, Spider) or obj is Spider:
                continue
            if obj.__module__ != mod.__name__:
                continue
            name = getattr(obj, "name", None) or obj.__name__
            if name == "spider":
                continue
            if name in found:
                raise RuntimeError(
                    f"Duplicate spider name {name!r}: "
                    f"{found[name].__module__} vs {obj.__module__}"
                )
            found[name] = obj
    return found


def get(name: str, package: str = "spiders") -> Type[Spider]:
    spiders = discover(package)
    if name not in spiders:
        known = ", ".join(sorted(spiders)) or "(none)"
        raise KeyError(f"Unknown spider {name!r}. Known: {known}")
    return spiders[name]
