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


def test_pass_inside_comment_does_not_verify() -> None:
    # The authoritative result is fail; a "pass" buried in a reason comment
    # must NOT flip it to verified.
    header = "mx.google.com; dkim=fail header.d=evil.com (forwarder note: dkim=pass earlier)"
    assert parse_authentication_results(header)["dkim"] is False


def test_spf_pass_in_comment_ignored() -> None:
    header = "spf=fail (google.com: spf=pass for a different hop) smtp.mailfrom=x@y.com"
    assert parse_authentication_results(header)["spf"] is False


def test_extension_field_does_not_impersonate_method() -> None:
    # x-dkim / reason-spf are not real dkim/spf results.
    header = "x-dkim=pass; reason-spf=pass; arc-dmarc=pass"
    assert parse_authentication_results(header) == {"dkim": False, "spf": False, "dmarc": False}


def test_method_at_start_of_value_matches() -> None:
    assert parse_authentication_results("dkim=pass; spf=pass; dmarc=pass")["dkim"] is True


# ------------------------------------------------------------------ #
# Authserv-id pinning
# ------------------------------------------------------------------ #


def test_authserv_id_pinning_accepts_matching_id() -> None:
    header = "mx.google.com; dkim=pass; spf=pass; dmarc=pass"
    result = parse_authentication_results(header, trusted_authserv_id="mx.google.com")
    assert result == {"dkim": True, "spf": True, "dmarc": True}


def test_authserv_id_pinning_rejects_foreign_id() -> None:
    # A forged header from a different relay must not count as pass.
    header = "attacker-relay.test; dkim=pass; spf=pass; dmarc=pass"
    result = parse_authentication_results(header, trusted_authserv_id="mx.google.com")
    assert result == {"dkim": False, "spf": False, "dmarc": False}


def test_authserv_id_pinning_rejects_no_id() -> None:
    # A header with no authserv-id is also rejected when pinning is active.
    header = "dkim=pass; spf=pass; dmarc=pass"
    result = parse_authentication_results(header, trusted_authserv_id="mx.google.com")
    assert result == {"dkim": False, "spf": False, "dmarc": False}


def test_no_pinning_accepts_any_authserv_id() -> None:
    # Without trusted_authserv_id, backward-compat: any (or no) authserv-id works.
    assert parse_authentication_results("dkim=pass; spf=pass; dmarc=pass")["dkim"] is True
    assert parse_authentication_results("attacker.test; dkim=pass; spf=pass; dmarc=pass")["dkim"] is True


def test_authserv_id_match_is_exact() -> None:
    # The pinned authserv-id must match exactly — not as a prefix or suffix.
    header = "mx.google.com; dkim=pass; dmarc=pass"
    assert parse_authentication_results(header, trusted_authserv_id="mx.google.com")["dkim"] is True

    # A different host that the trusted id is a substring of must NOT match.
    header2 = "evil-mx.google.com; dkim=pass; dmarc=pass"
    assert parse_authentication_results(header2, trusted_authserv_id="mx.google.com")["dkim"] is False

    # A look-alike that *starts with* the trusted id (the classic prefix-match
    # bypass) must NOT match: "mx.google.com.evil.com".startswith("mx.google.com").
    header3 = "mx.google.com.evil.com; dkim=pass; dmarc=pass"
    assert parse_authentication_results(header3, trusted_authserv_id="mx.google.com")["dkim"] is False

    # A subdomain of the trusted host is also not the trusted host.
    header4 = "relay.mx.google.com; dkim=pass; dmarc=pass"
    assert parse_authentication_results(header4, trusted_authserv_id="mx.google.com")["dkim"] is False
