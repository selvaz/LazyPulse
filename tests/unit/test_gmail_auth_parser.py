"""parse_authentication_results: DKIM/SPF/DMARC extraction."""

from __future__ import annotations

from lazypulse.adapters.gmail.auth import parse_authentication_results


def test_all_pass() -> None:
    header = (
        "mx.google.com; dkim=pass header.i=@example.com header.s=sel; "
        "spf=pass smtp.mailfrom=foo@example.com; dmarc=pass header.from=example.com"
    )
    assert parse_authentication_results(header) == {"dkim": True, "spf": True, "dmarc": True}


def test_all_fail() -> None:
    header = "mx.google.com; dkim=fail; spf=softfail; dmarc=fail"
    assert parse_authentication_results(header) == {"dkim": False, "spf": False, "dmarc": False}


def test_missing_header_is_all_false() -> None:
    assert parse_authentication_results(None) == {"dkim": False, "spf": False, "dmarc": False}
    assert parse_authentication_results("") == {"dkim": False, "spf": False, "dmarc": False}


def test_partial_pass() -> None:
    header = "mx.google.com; dkim=pass; spf=pass; dmarc=none"
    assert parse_authentication_results(header) == {"dkim": True, "spf": True, "dmarc": False}


def test_case_insensitive_result() -> None:
    assert parse_authentication_results("dkim=PASS; spf=Pass; dmarc=pass")["dkim"] is True


def test_whitespace_around_equals() -> None:
    assert parse_authentication_results("dkim = pass ; spf=pass; dmarc=pass")["dkim"] is True


def test_neutral_and_none_not_verified() -> None:
    header = "spf=neutral; dkim=none; dmarc=none"
    assert parse_authentication_results(header) == {"dkim": False, "spf": False, "dmarc": False}


def test_multiple_dkim_one_pass_counts() -> None:
    header = "dkim=fail header.i=@a.com; dkim=pass header.i=@b.com; spf=pass; dmarc=pass"
    assert parse_authentication_results(header)["dkim"] is True
