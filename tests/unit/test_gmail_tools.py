"""GmailTools: draft is free, send is guarded."""

from __future__ import annotations

from typing import Any

import pytest

from lazypulse.adapters.gmail.tools import GmailSendBlocked, GmailTools


class FakeService:
    def __init__(self) -> None:
        self.drafts: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    def create_draft(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        self.drafts.append({"to": to, "subject": subject, "body": body})
        return {"id": "draft-1"}

    def send_message(self, *, to: str, subject: str, body: str) -> dict[str, Any]:
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"id": "sent-1"}


def _tools(svc: FakeService, **kw: Any) -> tuple[Any, Any]:
    provider = GmailTools(svc, **kw)
    by_name = {t.name: t for t in provider.as_tools()}
    return provider, by_name


def test_as_tools_exposes_both() -> None:
    svc = FakeService()
    _, by_name = _tools(svc)
    assert set(by_name) == {"gmail_create_draft", "gmail_send"}


def test_provider_is_tool_provider() -> None:
    assert GmailTools(FakeService())._is_lazy_tool_provider is True


def test_create_draft_is_not_blocked() -> None:
    svc = FakeService()
    provider, _ = _tools(svc)
    out = provider._create_draft(to="a@x.com", subject="hi", body="b")
    assert "draft created" in out
    assert len(svc.drafts) == 1


def test_send_without_confirmation_blocked() -> None:
    svc = FakeService()
    provider, _ = _tools(svc)
    with pytest.raises(GmailSendBlocked, match="not been confirmed"):
        provider._send(to="a@x.com", subject="hi", body="b")
    assert svc.sent == []


def test_send_after_confirmation_succeeds() -> None:
    svc = FakeService()
    provider, _ = _tools(svc)
    provider.confirm()
    out = provider._send(to="a@x.com", subject="hi", body="b")
    assert "sent" in out
    assert len(svc.sent) == 1


def test_send_respects_recipient_allowlist() -> None:
    svc = FakeService()
    provider, _ = _tools(svc, allowed_recipients=["ok@x.com"], confirmed=True)
    with pytest.raises(GmailSendBlocked, match="allow-list"):
        provider._send(to="evil@y.com", subject="hi", body="b")
    assert svc.sent == []


def test_send_to_allowed_recipient_succeeds() -> None:
    svc = FakeService()
    provider, _ = _tools(svc, allowed_recipients=["ok@x.com"], confirmed=True)
    provider._send(to="ok@x.com", subject="hi", body="b")
    assert len(svc.sent) == 1


def test_require_confirmation_false_allows_send() -> None:
    svc = FakeService()
    provider, _ = _tools(svc, require_confirmation=False)
    provider._send(to="a@x.com", subject="hi", body="b")
    assert len(svc.sent) == 1
