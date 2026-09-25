"""Command modules. Each module defines register(registry) and adds its commands through it.

Modules are loaded in name order, so the resulting CLI is the same on every run.
"""

import importlib
import pkgutil


def load_command_modules(registry):
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        module.register(registry)
