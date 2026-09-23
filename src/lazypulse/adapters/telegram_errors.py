"""Classifying Telegram Bot API errors raised during intake.

``lazytools.connectors.telegram.client.TelegramClient`` wraps every failed
HTTP call — including a synchronous ``send_message`` a ``command_filter``
fires while ``PulseAgent._intake`` is still running — in a bare
``RuntimeError`` (see its ``_call``/``_call_multipart``), with no typed status
code attached. That is a deliberate choice on the client's side: it keeps the
client's public surface a duck-typed ``TelegramService`` Protocol that tests
can satisfy with a plain fake, with nothing Telegram-specific leaking into
callers that only need ``send_message``/``get_updates``.

Until the client exposes a typed exception, this module parses the
httpx-formatted message it produces —
``"...failed: Client error 'NNN <reason>' for url '...'"`` (httpx's own
``HTTPStatusError.__str__``) — to recover the status code. This is
best-effort by design: a message that does not match the pattern is treated
as *not* a permanent error, i.e. still retried. Silently dropping a message
we failed to classify would be worse than retrying one that could have been
salvaged.
"""

from __future__ import annotations

import re

#: Matches httpx's own "Client error 'NNN Reason' for url '...'" /
#: "Server error 'NNN Reason' for url '...'" message. Only "Client error"
#: (4xx) is ever treated as permanent below; "Server error" (5xx) stays
#: retryable regardless of the code it carries.
_STATUS_RE = re.compile(r"(Client|Server) error '(\d{3})\b")

#: 429 (Too Many Requests) is a 4xx that *is* meant to be retried — the Bot
#: API uses it for rate limiting, and the caller is expected to back off and
#: try again, not give up on the message.
_RETRYABLE_4XX = {429}


def telegram_error_status(exc: BaseException) -> int | None:
    """The HTTP status code embedded in a Telegram client error, if any.

    Looks at ``str(exc)`` (and, since the client raises with ``from None``,
    there is no chained cause to inspect instead). Returns ``None`` when the
    message does not carry a recognizable ``"Client/Server error 'NNN ...'"``
    fragment — e.g. Telegram's own ``ok: false`` error body, a plain timeout,
    or a connection failure, none of which carry an HTTP status at all.
    """
    match = _STATUS_RE.search(str(exc))
    if match is None:
        return None
    return int(match.group(2))


def is_permanent_telegram_error(exc: BaseException) -> bool:
    """True for a Telegram delivery failure that retrying will not fix.

    A 4xx *client* error other than 429 means the request itself was
    rejected (bad chat id, blocked bot, message format Telegram will never
    accept, ...); sending the exact same request again gets the exact same
    rejection. 429, every 5xx, and anything that raised without an
    HTTP-status-shaped message (timeouts, connection errors, Telegram's own
    ``ok: false`` errors) are left as retryable — unchanged from today.
    """
    match = _STATUS_RE.search(str(exc))
    if match is None:
        return False
    kind, code_str = match.group(1), match.group(2)
    if kind != "Client":
        return False
    code = int(code_str)
    return code not in _RETRYABLE_4XX
