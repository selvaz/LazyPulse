"""GmailInbox polling + idempotency, and GmailPolicy classification."""

from __future__ import annotations

import warnings
from typing import Any

import pytest
from lazybridge import Store

from lazypulse import store_keys
from lazypulse.adapters.gmail.inbox import GmailInbox, GmailInboxConfig
from lazypulse.adapters.gmail.policy import GmailPolicy
from lazypulse.models import ActionClass, TrustLevel

_PASS = "dkim=pass; spf=pass; dmarc=pass"
_FAIL = "dkim=fail; spf=fail; dmarc=fail"


def _resource(frm: str, subject: str, snippet: str, auth: str | None = None) -> dict[str, Any]:
    headers = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]
    if auth is not None:
        headers.append({"name": "Authentication-Results", "value": auth})
    return {"payload": {"headers": headers}, "snippet": snippet}


class FakeService:
    def __init__(self, messages: dict[str, dict[str, Any]]) -> None:
        self.messages = messages
        self.list_calls = 0

    def list_message_ids(self, *, query: str | None = None, max_results: int = 25) -> list[str]:
        self.list_calls += 1
        return list(self.messages.keys())

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self.messages[message_id]


def _inbox(messages: dict[str, dict[str, Any]], **cfg: Any) -> GmailInbox:
    config = GmailInboxConfig(account="me@example.com", **cfg)
    return GmailInbox(FakeService(messages), config)


async def test_drain_emits_one_message_each() -> None:
    inbox = _inbox({"a": _resource("x@y.com", "Hi", "snippet body", _PASS)})
    msgs = await inbox.drain(store=Store(), session=None)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.source == "gmail"
    assert m.message_id == "a"
    assert m.sender_raw == "x@y.com"
    assert "Hi" in m.text and "snippet body" in m.text
    assert m.metadata["auth"] == {"dkim": True, "spf": True, "dmarc": True}


async def test_idempotent_second_drain_is_empty() -> None:
    store = Store()
    inbox = _inbox({"a": _resource("x@y.com", "Hi", "b", _PASS), "c": _resource("z@y.com", "Yo", "d", _PASS)})
    first = await inbox.drain(store=store, session=None)
    second = await inbox.drain(store=store, session=None)
    assert len(first) == 2
    assert second == []


async def test_processed_marker_written() -> None:
    store = Store()
    inbox = _inbox({"a": _resource("x@y.com", "Hi", "b", _PASS)})
    await inbox.drain(store=store, session=None)
    assert store.read(store_keys.GMAIL_PROCESSED.format(message_id="a")) is not None


async def test_default_action_propagates() -> None:
    inbox = _inbox({"a": _resource("x@y.com", "Hi", "b", _PASS)}, default_action=ActionClass.WRITE_LOCAL)
    msgs = await inbox.drain(store=Store(), session=None)
    assert msgs[0].requested_action == ActionClass.WRITE_LOCAL


def test_readonly_scope_warns() -> None:
    with pytest.warns(UserWarning, match="readonly"):
        GmailInboxConfig(account="me@x", scope="readonly")


def test_metadata_scope_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        GmailInboxConfig(account="me@x", scope="metadata")  # must not raise


# --- GmailPolicy classification ---------------------------------------- #


async def test_owner_with_passing_auth_is_verified() -> None:
    inbox = _inbox({"a": _resource("me@example.com", "Hi", "b", _PASS)})
    msg = (await inbox.drain(store=Store(), session=None))[0]
    policy = GmailPolicy(owner_emails=["me@example.com"])
    assert policy.classify(msg).trust == TrustLevel.OWNER_VERIFIED_EMAIL


async def test_owner_with_failing_auth_is_unverified_claim() -> None:
    inbox = _inbox({"a": _resource("me@example.com", "Hi", "b", _FAIL)})
    msg = (await inbox.drain(store=Store(), session=None))[0]
    policy = GmailPolicy(owner_emails=["me@example.com"])
    assert policy.classify(msg).trust == TrustLevel.OWNER_CLAIM_UNVERIFIED


async def test_owner_with_missing_auth_header_never_verified() -> None:
    inbox = _inbox({"a": _resource("me@example.com", "Hi", "b", auth=None)})
    msg = (await inbox.drain(store=Store(), session=None))[0]
    policy = GmailPolicy(owner_emails=["me@example.com"])
    assert policy.classify(msg).trust == TrustLevel.OWNER_CLAIM_UNVERIFIED


async def test_allowed_external_with_passing_auth_is_external_verified() -> None:
    inbox = _inbox({"a": _resource("client@partner.com", "Hi", "b", _PASS)})
    msg = (await inbox.drain(store=Store(), session=None))[0]
    policy = GmailPolicy(owner_emails=["me@example.com"], allowed_external_senders=["client@partner.com"])
    assert policy.classify(msg).trust == TrustLevel.EXTERNAL_VERIFIED


async def test_unknown_sender_is_unknown() -> None:
    inbox = _inbox({"a": _resource("stranger@evil.com", "Hi", "b", _PASS)})
    msg = (await inbox.drain(store=Store(), session=None))[0]
    policy = GmailPolicy(owner_emails=["me@example.com"])
    assert policy.classify(msg).trust == TrustLevel.UNKNOWN


def test_config_defaults() -> None:
    cfg = GmailInboxConfig(account="me@x")
    assert cfg.query == "is:unread"
    assert cfg.scope == "metadata"
    assert cfg.max_results == 25
