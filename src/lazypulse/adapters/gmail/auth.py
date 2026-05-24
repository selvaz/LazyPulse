"""Parse the ``Authentication-Results`` email header.

DKIM / SPF / DMARC are the only signals that let us distinguish a genuinely
owner-sent email from a spoof. The parser is deliberately conservative: a
method counts as verified **only** when an *authoritative* result token for
it is exactly ``pass``. Anything else — ``fail``, ``none``, ``neutral``,
``softfail``, a missing header, or an unparseable one — is ``False``.

Three hardening rules guard against forged "pass" tokens:

1. **Authserv-id pinning.** When ``trusted_authserv_id`` is set, only
   headers whose leading authserv-id (the hostname before the first ``;``)
   starts with that value are parsed. Gmail prepends its own
   ``Authentication-Results: mx.google.com; …`` header and, per RFC 8601
   §5.7, strips any inbound copy claiming its own authserv-id. A forged
   header carried inside the message body must therefore use a *different*
   authserv-id — and is rejected outright when pinning is active.
2. **Comments are stripped first.** RFC 8601 headers carry CFWS comments and
   reason strings, e.g. ``spf=fail (sender note: spf=pass)``. Without
   stripping, the ``spf=pass`` inside the comment would be read as a result.
3. **The method must be a standalone token.** Matching is anchored to the
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


def _extract_authserv_id(header: str) -> str | None:
    """Return the lowercased authserv-id from an Authentication-Results header.

    The authserv-id is the text before the first ``;`` that contains no ``=``
    sign (i.e. it looks like a hostname, not a method result like
    ``dkim=pass``). Returns ``None`` when no authserv-id prefix is present.
    """
    semi = header.find(";")
    if semi < 0:
        return None
    candidate = header[:semi].strip()
    if "=" in candidate:
        return None  # first segment is a method result, not a hostname
    return candidate.lower()


def parse_authentication_results(
    header: str | None,
    *,
    trusted_authserv_id: str | None = None,
) -> dict[str, bool]:
    """Return ``{"dkim": bool, "spf": bool, "dmarc": bool}``.

    ``True`` means an authoritative result token for the method was ``pass``.
    A missing or empty header yields all-``False``.

    When ``trusted_authserv_id`` is set (e.g. ``"mx.google.com"``), the
    header is accepted **only** if its leading authserv-id starts with that
    value. A forged header with a different authserv-id (or no authserv-id at
    all) is rejected as all-``False``. The caller is responsible for passing
    the first / top-most ``Authentication-Results`` header from the message —
    the one prepended by the receiving MTA — rather than a later one.
    """
    result = {m: False for m in _METHODS}
    if not header:
        return result

    if trusted_authserv_id is not None:
        authserv_id = _extract_authserv_id(header)
        if authserv_id is None or not authserv_id.startswith(trusted_authserv_id.lower()):
            return result  # authserv-id absent or does not match: reject

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
