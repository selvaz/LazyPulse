#!/usr/bin/env python3
"""Harness di test & stress per il bot Telegram LazyPulse.

Due modalità:

* **offline** (default) — nessun token, nessuna rete. Un client Telegram finto
  alimenta update sintetici attraverso i componenti *reali* (``TelegramInbox`` +
  ``PulseAgent`` + ``TelegramReviewer``) con un ``MockEngine``. Verifica:
  throughput dell'orchestrazione, dedup idempotente, flusso HITL
  (approve/reject via /comando), recovery senza doppia esecuzione, e crescita
  dello Store limitata dalla retention.

* **live** — smoke test *gentile* contro il bot vero (serve ``BOT_TOKEN`` e
  ``OWNER_ID``). Manda un messaggio all'owner e attende una risposta. NON è uno
  stress: Telegram rate-limita/sospende gli account sotto carico (vedi il README
  del progetto), quindi lo stress pesante va fatto solo in offline.

Uso:
    python deploy/tg-bot/loadtest.py                    # stress offline (default)
    python deploy/tg-bot/loadtest.py --messages 5000    # più carico
    python deploy/tg-bot/loadtest.py --live             # smoke test live
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from lazybridge import Store

from lazypulse import PulseAgent, store_keys
from lazypulse.adapters.telegram import (
    TelegramInbox,
    TelegramInboxConfig,
    TelegramPolicy,
    TelegramReviewer,
)
from lazypulse.models import ActionClass, InboundMessage, PulseRecord, TickReport
from lazypulse.tasks import pending_tasks, purge_stale_rate_buckets, purge_terminal_tasks
from lazypulse.testing import FakeClock, MockEngine

OWNER = 123456
_RISKY = ("invia", "manda", "cancella", "paga", "send", "delete")


def _classify(msg: InboundMessage) -> ActionClass:
    """Stessa euristica del bot: messaggi rischiosi → EXTERNAL_SEND (→ review)."""
    text = (msg.text or "").lower()
    return ActionClass.EXTERNAL_SEND if any(k in text for k in _RISKY) else ActionClass.READ_PUBLIC


class FakeTelegramClient:
    """Client Telegram in-process: getUpdates/sendMessage senza rete."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.sent: list[tuple[int, str]] = []
        self.fail_send = False

    def enqueue(self, *, update_id: int, text: str, user_id: int = OWNER, is_bot: bool = False) -> None:
        self.updates.append(
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": int(datetime(2026, 1, 1, tzinfo=UTC).timestamp()),
                    "from": {"id": user_id, "is_bot": is_bot, "username": f"u{user_id}"},
                    "chat": {"id": user_id, "type": "private"},
                    "text": text,
                },
            }
        )

    # --- TelegramService duck type ---------------------------------- #
    def get_updates(self, *, offset: int, timeout: int, limit: int) -> list[dict[str, Any]]:
        # Bot API semantics: return updates with update_id >= offset, up to limit.
        return [u for u in self.updates if u["update_id"] >= offset][:limit]

    def send_message(self, *, chat_id: int, text: str) -> None:
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append((chat_id, text))


def _build(client: FakeTelegramClient, store: Store, clock: FakeClock, *, reviewer: TelegramReviewer | None) -> PulseAgent:
    return PulseAgent(
        name="loadtest",
        engine=MockEngine(["ok"]),
        store=store,
        clock=clock,
        policy=TelegramPolicy(owner_ids=[OWNER]),
        adapters=[TelegramInbox(client, TelegramInboxConfig(bot_id="loadtest"), clock=clock)],
        action_classifier=_classify,
        command_filter=reviewer.handle_command if reviewer else None,
        terminal_retention=3600.0,
        stale_after=600.0,
        tick_seconds=0.01,
    )


def _tasks(store: Store) -> list[PulseRecord]:
    return [PulseRecord.model_validate(store.read(k)) for k in store if k.startswith(store_keys.TASK_PREFIX)]


def _keycount(store: Store) -> int:
    return sum(1 for _ in store)


async def _drain_all(pulse: PulseAgent, *, max_ticks: int = 100000) -> int:
    """Tick finché non resta nulla di schedulabile. Ritorna il numero di tick.

    Il ``TelegramInbox`` prende al più ``max_results`` (100) update per drain e
    fa avanzare l'offset solo *dopo* che i record esistono, quindi tra una
    finestra da 100 e la successiva c'è un tick "vuoto" che avanza soltanto il
    watermark. Un singolo tick vuoto NON significa "finito": serve una coppia di
    tick vuoti di fila (la finestra successiva è già stata scoperta)."""
    empties = 0
    for i in range(max_ticks):
        report = await pulse.tick_once()
        pending_scheduled = any(t.status == "scheduled" for t in _tasks(pulse.store))
        if report.drained == 0 and report.due == 0 and not pending_scheduled:
            empties += 1
            if empties >= 2:
                return i + 1
        else:
            empties = 0
    return max_ticks


# ------------------------------------------------------------------ #
# Checks
# ------------------------------------------------------------------ #
async def check_throughput_and_dedup(n: int, dup_rate: float) -> tuple[bool, str]:
    client, store, clock = FakeTelegramClient(), Store(), FakeClock()
    pulse = _build(client, store, clock, reviewer=None)

    unique = 0
    dups = 0
    every = max(2, int(1 / dup_rate)) if dup_rate else 0
    for uid in range(1, n + 1):
        client.enqueue(update_id=uid, text=f"riassumi la nota {uid}")  # benigno → auto-run
        unique += 1
        if every and uid % every == 0:
            client.enqueue(update_id=uid, text="DUPLICATO")  # stesso update_id → deve deduplicare
            dups += 1

    t0 = time.perf_counter()
    ticks = await _drain_all(pulse)
    elapsed = time.perf_counter() - t0

    tasks = _tasks(store)
    completed = sum(1 for t in tasks if t.status == "completed")
    rate = unique / elapsed if elapsed else float("inf")
    ok = len(tasks) == unique and completed == unique
    detail = (
        f"{unique} msg + {dups} duplicati → {len(tasks)} task, {completed} completati "
        f"in {ticks} tick / {elapsed * 1000:.0f} ms  ≈ {rate:,.0f} msg/s orchestrazione"
    )
    return ok, detail


async def check_hitl_flow() -> tuple[bool, str]:
    client, store, clock = FakeTelegramClient(), Store(), FakeClock()
    reviewer = TelegramReviewer(client, store, owner_id=OWNER)
    pulse = _build(client, store, clock, reviewer=reviewer)

    # 1) messaggio rischioso dell'owner → parcheggiato per approvazione
    client.enqueue(update_id=1, text="invia il report trimestrale a Bob")
    await pulse.tick_once()
    parked = pending_tasks(store)
    if len(parked) != 1:
        return False, f"atteso 1 task in review, trovati {len(parked)}"
    task_id = parked[0].task_id

    # 2) il reviewer annuncia all'owner via Telegram
    sent0 = len(client.sent)
    n_notified = await reviewer.notify_pending()
    announced = client.sent[sent0:]
    if n_notified != 1 or not any(task_id in t and c == OWNER for c, t in announced):
        return False, "notifica HITL all'owner non inviata"

    # 3) l'owner approva via comando Telegram → il task gira
    client.enqueue(update_id=2, text=f"/approve {task_id}")
    await pulse.tick_once()
    rec = PulseRecord.model_validate(store.read(store_keys.task_key(task_id)))
    if rec.status != "completed":
        return False, f"dopo /approve lo stato è {rec.status}, atteso completed"

    # 4) reject di un secondo messaggio rischioso
    client.enqueue(update_id=3, text="cancella tutti i backup")
    await pulse.tick_once()
    task2 = pending_tasks(store)[0].task_id
    client.enqueue(update_id=4, text=f"/reject {task2} troppo pericoloso")
    await pulse.tick_once()
    rec2 = PulseRecord.model_validate(store.read(store_keys.task_key(task2)))
    ok = rec2.status == "rejected" and rec2.error == "troppo pericoloso"
    return ok, "approve→run + reject→rejected, notifica e comandi via Telegram OK"


async def check_recovery_no_double_exec() -> tuple[bool, str]:
    client, store, clock = FakeTelegramClient(), Store(), FakeClock()
    pulse = _build(client, store, clock, reviewer=None)

    rec = PulseRecord(text="crawl lento", status="running", created_at=clock.now, run_at=clock.now, started_at=clock.now)
    key = store_keys.task_key(rec.task_id)
    store.write(key, rec.model_dump(mode="json"))
    pulse._inflight.add(key)  # in esecuzione in questo processo

    clock.advance(5000)  # ben oltre stale_after
    report = TickReport(at=clock.now)
    pulse._recover_stale(clock.now, report)
    if report.recovered != 0 or PulseRecord.model_validate(store.read(key)).status != "running":
        return False, "un task in volo è stato resettato → rischio doppia esecuzione"

    pulse._inflight.discard(key)  # ora sì un vero crash
    pulse._recover_stale(clock.now, report)
    ok = report.recovered == 1 and PulseRecord.model_validate(store.read(key)).status == "scheduled"
    return ok, "task in volo protetto; task crashato recuperato"


def check_retention_bounds_store(n_terminal: int) -> tuple[bool, str]:
    store = Store()
    old = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(n_terminal):
        rec = PulseRecord(text=f"t{i}", status="completed", created_at=old, run_at=old, completed_at=old)
        store.write(store_keys.task_key(rec.task_id), rec.model_dump(mode="json"))
    cur_bucket = int(old.timestamp()) // 3600
    for i in range(50):
        store.write(f"pulse:rate:s{i}:{cur_bucket - 5}", {"count": 1})  # finestre chiuse

    before = _keycount(store)
    now = old + timedelta(hours=48)
    pruned_tasks = purge_terminal_tasks(store, older_than=timedelta(hours=24), now=now)
    pruned_rate = purge_stale_rate_buckets(store, window_seconds=3600, now=now)
    after = _keycount(store)

    ok = pruned_tasks == n_terminal and pruned_rate == 50 and after < before
    return ok, f"store {before} -> {after} chiavi (-{pruned_tasks} task terminali, -{pruned_rate} rate-bucket)"


# ------------------------------------------------------------------ #
# Runners
# ------------------------------------------------------------------ #
async def run_offline(messages: int, dup_rate: float) -> int:
    print(f"\n=== LazyPulse Telegram — STRESS OFFLINE ({messages} msg) ===\n")
    results: list[tuple[str, bool, str]] = []

    ok, d = await check_throughput_and_dedup(messages, dup_rate)
    results.append(("throughput + dedup", ok, d))
    ok, d = await check_hitl_flow()
    results.append(("HITL approve/reject via Telegram", ok, d))
    ok, d = await check_recovery_no_double_exec()
    results.append(("recovery senza doppia esecuzione", ok, d))
    ok, d = check_retention_bounds_store(n_terminal=max(200, messages // 5))
    results.append(("retention limita lo Store", ok, d))

    print()
    all_ok = True
    for name, passed, detail in results:
        mark = "✅ PASS" if passed else "❌ FAIL"
        all_ok &= passed
        print(f"  {mark}  {name}")
        print(f"          {detail}")
    print()
    print("Risultato:", "TUTTO VERDE ✅" if all_ok else "CI SONO FALLIMENTI ❌")
    return 0 if all_ok else 1


def run_live(timeout_s: float) -> int:
    token = os.environ.get("BOT_TOKEN")
    owner = os.environ.get("OWNER_ID")
    if not token or not owner:
        print("live mode richiede BOT_TOKEN e OWNER_ID nell'ambiente.")
        return 2
    owner_id = int(owner)
    try:
        from lazytools.connectors.telegram import TelegramClient
    except ImportError:
        print("live mode richiede lazytoolkit: pip install 'lazytoolkit[telegram]'")
        return 2

    print("\n=== LazyPulse Telegram — SMOKE TEST LIVE ===")
    print("⚠️  Gentile per definizione: Telegram rate-limita/sospende sotto carico.")
    print("    Lo stress pesante va fatto in offline.\n")

    client = TelegramClient.from_token(token)
    stamp = datetime.now(UTC).strftime("%H:%M:%S")
    client.send_message(chat_id=owner_id, text=f"✅ LazyPulse loadtest {stamp}: rispondi qualsiasi cosa entro {int(timeout_s)}s")
    print(f"→ Inviato messaggio di test all'owner {owner_id}. In attesa di una risposta...")

    # Poll getUpdates for a reply from the owner.
    offset = 0
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        updates = client.get_updates(offset=offset, timeout=1, limit=10)
        for upd in updates:
            offset = max(offset, int(upd.get("update_id", 0)) + 1)
            msg = upd.get("message") or {}
            if (msg.get("from") or {}).get("id") == owner_id and (msg.get("text") or msg.get("caption")):
                client.send_message(chat_id=owner_id, text="👍 Ricevuto. Round-trip live OK.")
                print("← Risposta ricevuta dall'owner. Round-trip live OK ✅")
                return 0
        time.sleep(1)
    print("Nessuna risposta entro il timeout (round-trip non verificato).")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Test & stress per il bot Telegram LazyPulse")
    parser.add_argument("--live", action="store_true", help="smoke test live (serve BOT_TOKEN, OWNER_ID)")
    parser.add_argument("--messages", type=int, default=2000, help="numero di messaggi nello stress offline")
    parser.add_argument("--dup-rate", type=float, default=0.1, help="frazione di duplicati (test dedup)")
    parser.add_argument("--live-timeout", type=float, default=30.0, help="secondi di attesa risposta in live")
    args = parser.parse_args()

    if args.live:
        return run_live(args.live_timeout)
    return asyncio.run(run_offline(args.messages, args.dup_rate))


if __name__ == "__main__":
    raise SystemExit(main())
