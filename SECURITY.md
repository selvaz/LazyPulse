# Security Policy

## Reporting Vulnerabilities

**Please do not open public issues for security vulnerabilities.**

The preferred channel is **GitHub Private Vulnerability Reporting**, which is
enabled on this repository. Open
<https://github.com/selvaz/LazyPulse/security/advisories/new> (Security tab →
"Report a vulnerability") to file a private report visible only to the
maintainers. This keeps the disclosure confidential while we triage and ship a
fix.

If you cannot use GitHub's reporting flow, email the maintainers at
**security@lazypulse.dev** instead.

Please include a description of the issue, the affected version(s), and a
minimal reproduction if you have one. We aim to acknowledge reports within a
few business days and will coordinate a disclosure timeline with you.

## Scope

LazyPulse is a trust/security-sensitive runtime: it ingests untrusted inbound
messages (Gmail, Telegram, webhooks) and decides — **before** any engine runs —
whether each message is authorized, queued for human review, or rejected. The
threat model and the trust matrix that backs those decisions are documented in
[`docs/security.md`](docs/security.md); this file covers **how to report**
problems, not the model itself.

Reports we especially want to hear about:

- Ways an untrusted sender can elevate their effective trust level or bypass
  `PulsePolicy.authorize` / `classify`.
- Prompt-injection paths that influence authorization (authorization must be
  based on the *sender*, never the message text).
- Adapter idempotency / replay-protection weaknesses (webhook nonce, Gmail/
  Telegram dedupe) that let an action run more than once or run unauthorized.
- Secret leakage through Store records, `Session` events, or adapter logs.

## Supported Versions

LazyPulse is pre-1.0; security fixes target the latest released `0.x` line.
