"""Webhook intake: drive a PulseAgent from HTTP POSTs.

Requires the webhook extra:  pip install 'lazypulse[webhook]'

Run:
    python examples/02_webhook_intake.py

Then in another terminal:
    curl -X POST http://127.0.0.1:8099/inbound \\
         -H 'content-type: application/json' \\
         -d '{"message_id": "abc", "text": "what is on my calendar today?"}'

With HMAC enabled (shared_secret="topsecret"), sign the raw body:
    BODY='{"message_id":"abc","text":"hi"}'
    SIG=$(printf "%s" "$BODY" | openssl dgst -sha256 -hmac topsecret | awk '{print $2}')
    curl -X POST http://127.0.0.1:8099/inbound \\
         -H "content-type: application/json" -H "X-Pulse-Signature: $SIG" -d "$BODY"
"""

from __future__ import annotations

from lazybridge import Session, Store

from lazypulse import PulseAgent
from lazypulse.adapters.webhook import WebhookAdapter
from lazypulse.testing import MockEngine


def main() -> None:
    store = Store()
    adapter = WebhookAdapter(host="127.0.0.1", port=8099, store=store)  # add shared_secret="..." for HMAC

    pulse = PulseAgent(
        name="webhook-pulse",
        engine=MockEngine(["handled"]),  # swap for LLMEngine("claude-opus-4-8")
        store=store,
        session=Session(),
        adapters=[adapter],
        unsafe_allow_all=True,  # demo only — use policy=... in production
        tick_seconds=1.0,
    )

    # The tick loop runs in the background; uvicorn owns the main thread.
    pulse.start()
    print(f"POST messages to http://{adapter.host}:{adapter.port}{adapter.path}  (Ctrl-C to stop)")
    try:
        import uvicorn

        uvicorn.run(adapter.asgi_app(), host=adapter.host, port=adapter.port, log_level="info")
    finally:
        pulse.stop()


if __name__ == "__main__":
    main()
