"""Verify the built wheel's metadata matches what the source declares.

Guards against the failure mode where a published artifact carries different
``Requires-Dist`` entries than ``pyproject.toml`` (stale build, wrong branch,
or a bare name that can never resolve). Run after ``python -m build``:

    python tools/check_wheel_metadata.py [dist/]

Exits non-zero with a diagnostic when a check fails.
"""

from __future__ import annotations

import email.parser
import pathlib
import sys
import tomllib
import zipfile

from packaging.requirements import Requirement

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Requirements that MUST appear (as parsed name + specifier set) in the
# wheel's Requires-Dist.
REQUIRED = [
    "lazybridge>=1.0.1,<2.0",
    "pydantic>=2.0.0,<3.0.0",
]

# Package names that must never appear as bare (non-URL) requirements: they
# are not on PyPI, so a bare name is unresolvable and a dependency-confusion
# exposure. They may only appear as direct references (``name @ git+https...``).
GITHUB_ONLY = {"lazytoolkit", "lazycrawler", "market-data-hub", "market_data_hub"}


def main() -> int:
    dist = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "dist"
    wheels = sorted(dist.glob("*.whl"))
    if not wheels:
        print(f"ERROR: no wheel found in {dist}")
        return 1
    wheel = wheels[-1]

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected_version = pyproject["project"]["version"]

    with zipfile.ZipFile(wheel) as zf:
        meta_name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        metadata = email.parser.Parser().parsestr(zf.read(meta_name).decode("utf-8"))

    errors: list[str] = []

    version = metadata["Version"]
    if version != expected_version:
        errors.append(f"wheel Version {version!r} != pyproject version {expected_version!r}")

    requires = metadata.get_all("Requires-Dist") or []
    parsed = [Requirement(r) for r in requires]

    for req in REQUIRED:
        want = Requirement(req)
        # Compare parsed name + specifier set: metadata writers normalize
        # specifier order, so string equality would spuriously fail.
        if not any(p.name == want.name and set(p.specifier) == set(want.specifier) for p in parsed):
            errors.append(f"missing required Requires-Dist entry: {req!r}")

    for p in parsed:
        if p.name.lower().replace("_", "-") in {n.replace("_", "-") for n in GITHUB_ONLY} and p.url is None:
            errors.append(f"bare requirement on GitHub-only package (dependency confusion risk): {str(p)!r}")

    if errors:
        print(f"FAIL: {wheel.name}")
        for e in errors:
            print(f"  - {e}")
        print("\nRequires-Dist in wheel:")
        for r in requires:
            print(f"  {r}")
        return 1

    print(f"OK: {wheel.name} — version {version}, {len(requires)} Requires-Dist entries verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
