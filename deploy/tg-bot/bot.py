"""Bot Telegram conversazionale *always-on* — LazyPulse + LazyTools + DeepSeek.

Polla un bot Telegram (`getUpdates`), classifica il mittente con
``TelegramPolicy`` (solo l'``OWNER_ID`` verificato attiva il worker), fa girare
un agente DeepSeek su LazyBridge e rimanda la risposta nella chat di origine.

Tutta la configurazione arriva da variabili d'ambiente, così su Railway/Render
si imposta dal pannello web senza toccare il codice:

    BOT_TOKEN          (obbligatoria)  token di @BotFather
    OWNER_ID           (obbligatoria)  il tuo user_id Telegram (message.from.id)
    DEEPSEEK_API_KEY   (obbligatoria)  chiave da platform.deepseek.com
    MODEL              (opz.)          default: deepseek-v4-flash
    STORE_DB           (opz.)          default: /data/pulse.db  (montare un Volume!)
    TICK_SECONDS       (opz.)          default: 3
    REPLY_MIN_INTERVAL (opz.)          default: 2   (anti-loop per chat)
    SYSTEM_PROMPT      (opz.)          istruzioni di sistema dell'assistente
"""

from __future__ import annotations

import os

from lazybridge import LLMEngine, Store
from lazytools.connectors.telegram import TelegramClient

from lazypulse import PulseAgent
from lazypulse.adapters.telegram import TelegramInbox, TelegramInboxConfig, TelegramPolicy


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Variabile d'ambiente obbligatoria mancante: {name}")
    return value


def main() -> None:
    token = _require("BOT_TOKEN")
    owner_id = int(_require("OWNER_ID"))
    _require("DEEPSEEK_API_KEY")  # letta internamente dal provider DeepSeek di LazyBridge

    model = os.environ.get("MODEL", "deepseek-v4-flash")
    db_path = os.environ.get("STORE_DB", "/data/pulse.db")
    tick_seconds = float(os.environ.get("TICK_SECONDS", "3"))
    reply_min_interval = float(os.environ.get("REPLY_MIN_INTERVAL", "2"))
    system = os.environ.get(
        "SYSTEM_PROMPT",
        "Sei un assistente personale conciso. Rispondi direttamente all'utente.",
    )

    client = TelegramClient.from_token(token)

    pulse = PulseAgent(
        name="tg-deepseek",
        engine=LLMEngine(model, system=system),
        store=Store(db=db_path),                       # persistente: serve un Volume montato
        policy=TelegramPolicy(owner_ids=[owner_id]),   # solo l'owner verificato attiva il worker
        adapters=[
            TelegramInbox(
                client,
                TelegramInboxConfig(
                    bot_id="tg-deepseek",
                    reply_with_output=True,            # rimanda l'output nella chat (due vie)
                    reply_min_interval_seconds=reply_min_interval,  # circuit breaker anti-loop
                ),
            )
        ],
        tick_seconds=tick_seconds,
    )

    print(
        f"[tg-deepseek] avvio | model={model} db={db_path} "
        f"tick={tick_seconds}s owner={owner_id}",
        flush=True,
    )
    pulse.serve()   # loop infinito: questo è il "sempre acceso"


if __name__ == "__main__":
    main()
