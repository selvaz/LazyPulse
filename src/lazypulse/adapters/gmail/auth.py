"""Parse the ``Authentication-Results`` email header.

DKIM / SPF / DMARC are the only signals that let us distinguish a genuinely
owner-sent email from a spoof. The parser is deliberately conservative: a
method counts as verified **only** when its result is exactly ``pass``.
Anything else — ``fail``, ``none``, ``neutral``, ``softfail``, a missing
header, or an unparseable one — is ``False``.

Pure-Python: importable without the Gmail extra.
"""

from __future__ import annotations

import re

_METHODS = ("dkim", "spf", "dmarc")
# Match e.g. ``dkim=pass``, ``spf=fail``, ``dmarc=none`` — the method name
# followed by ``=`` and a result token, anywhere in the header value.
_RESULT_RE = {m: re.compile(rf"\b{m}\s*=\s*([a-zA-Z]+)") for m in _METHODS}


def parse_authentication_results(header: str | None) -> dict[str, bool]:
    """Return ``{"dkim": bool, "spf": bool, "dmarc": bool}``.

    ``True`` means the method's result was ``pass``. A missing or empty
    header yields all-``False``.
    """
    result = {m: False for m in _METHODS}
    if not header:
        return result
    for method, pattern in _RESULT_RE.items():
        # A header may carry more than one result per method (multiple
        # signing domains). Treat the method as verified if any token passes.
        for match in pattern.finditer(header):
            if match.group(1).lower() == "pass":
                result[method] = True
                break
    return result
