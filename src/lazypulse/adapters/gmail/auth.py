"""Parse the ``Authentication-Results`` email header.

DKIM / SPF / DMARC are the only signals that let us distinguish a genuinely
owner-sent email from a spoof. The parser is deliberately conservative: a
method counts as verified **only** when an *authoritative* result token for
it is exactly ``pass``. Anything else — ``fail``, ``none``, ``neutral``,
``softfail``, a missing header, or an unparseable one — is ``False``.

Two hardening rules guard against forged "pass" tokens:

1. **Comments are stripped first.** RFC 8601 headers carry CFWS comments and
   reason strings, e.g. ``spf=fail (sender note: spf=pass)``. Without
   stripping, the ``spf=pass`` inside the comment would be read as a result.
2. **The method must be a standalone token.** Matching is anchored to the
   start of the value or a ``;`` / whitespace boundary, so an extension field
   like ``x-dkim=pass`` or ``reason-spf=pass`` cannot impersonate a real
   ``dkim`` / ``spf`` result.

Pure-Python: importable without the Gmail extra.
"""

from __future__ import annotations

import re

_METHODS = ("dkim", "spf", "dmarc")
# A CFWS comment / reason string. Stripped before parsing so a "pass" buried
# inside one is never read as a result.
_COMMENT_RE = re.compile(r"\([^()]*\)")
# Anchor each method to the start of the value or a ``;`` / whitespace
# boundary so ``x-dkim=pass`` (preceded by ``-``) does not match.
_RESULT_RE = {m: re.compile(rf"(?:^|[;\s]){m}\s*=\s*([a-zA-Z]+)", re.IGNORECASE) for m in _METHODS}


def parse_authentication_results(header: str | None) -> dict[str, bool]:
    """Return ``{"dkim": bool, "spf": bool, "dmarc": bool}``.

    ``True`` means an authoritative result token for the method was ``pass``.
    A missing or empty header yields all-``False``.
    """
    result = {m: False for m in _METHODS}
    if not header:
        return result

    # Collapse comments (a few passes handles the rare nested case).
    cleaned = header
    for _ in range(5):
        stripped = _COMMENT_RE.sub(" ", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped

    for method, pattern in _RESULT_RE.items():
        # After comment-stripping + anchoring, every match is a genuine result
        # token. A message may carry several (multiple DKIM signatures); one
        # authoritative ``pass`` is enough, matching standard DKIM semantics.
        for match in pattern.finditer(cleaned):
            if match.group(1).lower() == "pass":
                result[method] = True
                break
    return result
