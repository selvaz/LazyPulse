"""Thin wrapper around the Telegram Bot API.

``httpx`` is imported lazily inside :meth:`TelegramClient.from_token`, so this
module imports cleanly without the ``telegram`` extra. ``TelegramInbox`` and
``TelegramTools`` depend only on the duck-typed :class:`TelegramService`
surface defined here, which means tests inject a fake client and never touch
the network.

Why not aiogram / python-telegram-bot? Those ship a *dispatcher* that runs its
own polling loop — a second loop competing with the PulseAgent's tick loop.
LazyPulse only needs two Bot API methods (``getUpdates`` + ``sendMessage``),
which map cleanly onto the adapter's pull-based ``drain()``, so a small HTTP
wrapper keeps the dependency surface minimal. Swap in your own
``TelegramService`` (e.g. aiogram-backed) if you prefer.
"""

from __future__ import annotations

from typing import Any, Protocol


class TelegramService(Protocol):
    """The subset of a Telegram Bot API client that LazyPulse uses."""

    def get_updates(self, *, offset: int, timeout: int = 0, limit: int = 100) -> list[dict[str, Any]]: ...
    def send_message(self, *, chat_id: int | str, text: str) -> dict[str, Any]: ...


class TelegramClient:
    """Production :class:`TelegramService` backed by the Bot API over HTTPS."""

    def __init__(self, token: str, *, http: Any | None = None, base_url: str = "https://api.telegram.org") -> None:
        # ``http`` is an ``httpx.Client`` (or any object exposing
        # ``post(url, json=...) -> response`` with ``raise_for_status`` + ``json``).
        self._token = token
        self._base = f"{base_url}/bot{token}"
        self._http = http

    @classmethod
    def from_token(cls, token: str, *, timeout: float = 30.0) -> TelegramClient:
        """Build a client from a bot token (obtained from @BotFather).

        Imports ``httpx`` lazily; raises a friendly ``ImportError`` if the
        ``telegram`` extra is not installed.
        """
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover — exercised only without the extra
            raise ImportError(
                "TelegramClient.from_token requires the 'telegram' extra. "
                "Install it with: pip install 'lazypulse[telegram]'"
            ) from exc
        return cls(token, http=httpx.Client(timeout=timeout))

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        if self._http is None:
            raise RuntimeError("TelegramClient has no HTTP client; use from_token() or inject http=")
        resp = self._http.post(f"{self._base}/{method}", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok", False):
            raise RuntimeError(f"Telegram API error on {method}: {data.get('description', data)}")
        return data.get("result")

    # ------------------------------------------------------------------ #
    # TelegramService
    # ------------------------------------------------------------------ #
    def get_updates(self, *, offset: int, timeout: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        result = self._call("getUpdates", {"offset": offset, "timeout": timeout, "limit": limit})
        return list(result or [])

    def send_message(self, *, chat_id: int | str, text: str) -> dict[str, Any]:
        return dict(self._call("sendMessage", {"chat_id": chat_id, "text": text}) or {})
