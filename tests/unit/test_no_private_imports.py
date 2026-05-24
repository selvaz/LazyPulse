"""Boundary test: LazyPulse may only import lazybridge's public surface.

Allowed: ``lazybridge`` top-level and ``lazybridge.ext.*``. Reaching into
private internals (``lazybridge.core.*``, ``lazybridge.engines._*``,
underscore-prefixed submodules) couples LazyPulse to lazybridge's
implementation and breaks on minor releases — CI fails fast on it.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "lazypulse"

#: Import prefixes that are forbidden in shipped source.
FORBIDDEN_PREFIXES = (
    "lazybridge.core",
    "lazybridge.engines._",
    "lazybridge.engines.plan._",
    "lazybridge.agent",
    "lazybridge.envelope",
    "lazybridge.store.",
    "lazybridge.session.",
    "lazybridge.tools.",
    "lazybridge._",
)


def _is_forbidden(module: str) -> bool:
    # ``lazybridge.ext.*`` is explicitly part of the public surface.
    if module == "lazybridge" or module.startswith("lazybridge.ext"):
        return False
    return any(module == p.rstrip(".") or module.startswith(p) for p in FORBIDDEN_PREFIXES)


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_no_private_lazybridge_imports() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for module in _imported_modules(tree):
            if module.startswith("lazybridge") and _is_forbidden(module):
                offenders.append(f"{path.relative_to(SRC)}: {module}")
    assert not offenders, "Forbidden lazybridge private imports:\n" + "\n".join(offenders)


def test_boundary_detects_bad_import() -> None:
    # Guards the guard: a deliberately-bad import must be flagged.
    bad = ast.parse("from lazybridge.core.executor import run\nimport lazybridge.engines._x\n")
    mods = _imported_modules(bad)
    assert any(_is_forbidden(m) for m in mods)
    assert not _is_forbidden("lazybridge")
    assert not _is_forbidden("lazybridge.ext.hil")
