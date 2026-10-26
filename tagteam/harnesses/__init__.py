"""Registry of supported agent CLIs. Each harness bundles how to be an author (hooks) and a reviewer (headless run)."""
import dataclasses
import shutil
from typing import Callable

from . import agy, claude, codex, copilot
from .base import Author, Reviewer


@dataclasses.dataclass(frozen=True)
class Harness:
    name: str
    detect: Callable
    author: Author | None
    reviewer: Reviewer | None


def _on_path(cli):
    return lambda: shutil.which(cli) is not None


ALL = {name: Harness(name, _on_path(name), mod.AUTHOR, mod.REVIEWER)
       for name, mod in (("claude", claude), ("codex", codex), ("copilot", copilot), ("agy", agy))}


def get(name):
    if name not in ALL:
        raise ValueError(f"unknown agent {name}")
    return ALL[name]


def detected():
    return [name for name, h in ALL.items() if h.detect()]
