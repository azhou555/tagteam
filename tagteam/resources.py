"""Bundled data files, read through importlib.resources so they work from any install."""
import atexit
import contextlib
import importlib.resources
import pathlib

_stack = contextlib.ExitStack()
_paths = {}
atexit.register(_stack.close)


def _file(name):
    return importlib.resources.files("tagteam").joinpath("data", name)


def text(name):
    return _file(name).read_text(encoding="utf-8")


def path(name):
    if name not in _paths:
        _paths[name] = pathlib.Path(_stack.enter_context(importlib.resources.as_file(_file(name))))
    return _paths[name]
