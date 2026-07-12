# Contributing to LazyPulse

Welcome. This page is the minimum you need to run the test suite, lint,
type-check, and respect the lazybridge boundary locally. See
[`docs/architecture.md`](docs/architecture.md) for the design and
[`SECURITY.md`](SECURITY.md) for how to report vulnerabilities (and
[`docs/security.md`](docs/security.md) for the threat model).

## Bootstrap

```bash
# Editable install with the dev extra (pytest, pytest-asyncio, pytest-cov,
# httpx, starlette, ruff, mypy, lazytoolkit).
python -m pip install -e '.[dev]'
```

`lazytoolkit` (the Gmail/Telegram clients + tools) is **GitHub-only** — only
LazyBridge is on PyPI. The `[dev]` extra already pins it to the same release
tag CI uses, so the editable install above pulls it in. To install it on its
own:

```bash
python -m pip install \
  "lazytoolkit @ git+https://github.com/selvaz/LazyTools.git"
```

> Note: the distribution is named `lazytoolkit` but imports as `lazytools`
> (e.g. `from lazytools.connectors.gmail import GmailClient`).

## Run the checks

| Command | Closes which CI job |
|---|---|
| `python -m pytest -q` | unit tests (run `pytest --collect-only -q \| tail -1` for the live count) |
| `python -m pytest --cov=lazypulse --cov-report=term-missing` | coverage (CI enforces a floor) |
| `python -m ruff check src tests` | lint |
| `python -m mypy src/lazypulse` | type-check |
| `python -m pytest tests/unit/test_no_private_imports.py -q` | lazybridge boundary |

All must be green before a PR merges.

## The lazybridge boundary

Shipped LazyPulse source may import only the **public** lazybridge surface —
the `lazybridge` top level and `lazybridge.ext.*`. Never import lazybridge's
private internals. The `boundary` CI job (and
`tests/unit/test_no_private_imports.py`) enforces this statically. Importing the
`lazytools` sibling is allowed and not policed.

## Code style

Default to writing no comments. Add one only when the WHY is non-obvious (a
hidden constraint, a subtle concurrency invariant, a workaround for a specific
upstream bug). Keep changes surgical — the runtime is concurrency-sensitive and
well-tested, so prefer minimal diffs that keep every existing test green.

## PR checklist

- [ ] `pytest -q` green locally
- [ ] `ruff check src tests` clean
- [ ] `mypy src/lazypulse` clean
- [ ] boundary test (`test_no_private_imports.py`) green
- [ ] CHANGELOG entry under the active version section
