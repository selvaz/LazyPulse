"""Bot Telegram conversazionale *always-on* — LazyPulse + LazyTools + DeepSeek.

Polla un bot Telegram (`getUpdates`), classifica il mittente con
``TelegramPolicy`` (solo l'``OWNER_ID`` verificato attiva il worker), fa girare
un agente DeepSeek su LazyBridge e rimanda la risposta nella chat di origine.
Opzionalmente dà all'agente i tool di **LazyCrawler** (ricerca/crawl web).

Tutta la configurazione arriva da variabili d'ambiente, così su Railway/Render
si imposta dal pannello web senza toccare il codice:

    BOT_TOKEN          (obbligatoria)  token di @BotFather
    OWNER_ID           (obbligatoria)  il tuo user_id Telegram (message.from.id)
    DEEPSEEK_API_KEY   (obbligatoria)  chiave da platform.deepseek.com
    MODEL              (opz.)          default: deepseek-v4-flash
    STORE_DB           (opz.)          default: /data/pulse.db  (montare un Volume!)
    TICK_SECONDS       (opz.)          default: 3
    REPLY_MIN_INTERVAL (opz.)          default: 2   (anti-loop per chat)
    RETENTION_SECONDS  (opz.)          default: 604800 (7g) — pota record terminali + rate-bucket
    STALE_AFTER        (opz.)          default: 600 — recovery dei task "running" crashati
    SYSTEM_PROMPT      (opz.)          istruzioni di sistema dell'assistente

    # --- human-in-the-loop (HITL) ---
    # Un task parcheggiato per approvazione manda un messaggio all'owner; l'owner
    # risponde "/approve <id>" o "/reject <id> [motivo]". Attivo di default (il
    # reviewer è sempre cablato); scatta quando un'azione risulta rischiosa.
    REVIEW_KEYWORDS    (opz.)          parole che mandano un messaggio in review
                                        (default: invia,manda,cancella,paga,...;
                                        REVIEW_KEYWORDS="" per disattivare l'HITL)

    # --- crawler (LazyCrawler) ---
    ENABLE_CRAWLER     (opz.)          "1" (default) per dare i tool web all'agente; "0" per spegnerlo
    CRAWL_CONTENT      (opz.)          "pure" (default, leggero, zero token) | "ml" (modelli locali)
    CRAWL_MAX_PAGES    (opz.)          default: 5
    CRAWL_MAX_DEPTH    (opz.)          default: 1
    CRAWLER_DB         (opz.)          default: /data/crawler.db  (cache-first, sul Volume)
"""

from __future__ import annotations

import asyncio
import os

from lazybridge import LLMEngine, Store
from lazytools.connectors.telegram import TelegramClient

from lazypulse import PulseAgent
from lazypulse.adapters.telegram import (
    TelegramInbox,
    TelegramInboxConfig,
    TelegramPolicy,
    TelegramReviewer,
)
from lazypulse.models import ActionClass, InboundMessage

#: Parole che fanno considerare "rischioso" un messaggio → l'azione viene
#: rietichettata EXTERNAL_SEND, così un owner verificato deve confermarla
#: (REQUIRE_OWNER_CONFIRMATION → awaiting_review → messaggio HITL). Override con
#: la env REVIEW_KEYWORDS (lista separata da virgole); REVIEW_KEYWORDS="" spegne.
_DEFAULT_REVIEW_KEYWORDS = [
    "invia", "inviare", "manda", "mandare", "spedisci", "email", "mail",
    "cancella", "elimina", "rimuovi", "paga", "pagamento", "bonifico",
    "trasferisci", "send", "delete", "remove", "pay", "transfer", "wire",
]


def _build_action_classifier():
    """Euristica opt-out: mappa i messaggi che chiedono azioni rischiose a
    EXTERNAL_SEND (→ review). Restituisce ``None`` se disabilitata.

    Nota: è un MVP a parole-chiave — rietichetta *l'intento*, non confina i
    tool del worker. Per un confinamento reale, gattare il tool di invio."""
    raw = os.environ.get("REVIEW_KEYWORDS")
    keywords = (
        [w.strip().lower() for w in raw.split(",") if w.strip()]
        if raw is not None
        else _DEFAULT_REVIEW_KEYWORDS
    )
    if not keywords:
        return None

    def classify(msg: InboundMessage) -> ActionClass:
        text = (msg.text or "").lower()
        if any(kw in text for kw in keywords):
            return ActionClass.EXTERNAL_SEND
        return ActionClass.READ_PUBLIC

    return classify


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Variabile d'ambiente obbligatoria mancante: {name}")
    return value


def _build_crawler_tools() -> list:
    """Costruisce i tool di LazyCrawler con limiti prudenti. Restituisce [] se disabilitato."""
    if os.environ.get("ENABLE_CRAWLER", "1") != "1":
        return []
    from lazycrawler import CrawlerDB
    from lazycrawler.config import CrawlerConfig, DBConfig
    from lazycrawler.tools import CrawlerTools

    content = os.environ.get("CRAWL_CONTENT", "pure")   # "pure" (zero token) | "ml" (modelli locali)
    crawler = CrawlerTools(
        db=CrawlerDB(DBConfig(db_path=os.environ.get("CRAWLER_DB", "/data/crawler.db"), ttl_hours=48.0)),
        crawler_cfg=CrawlerConfig(
            max_depth=int(os.environ.get("CRAWL_MAX_DEPTH", "1")),
            max_pages=int(os.environ.get("CRAWL_MAX_PAGES", "5")),
            respect_robots=True,        # rispetta robots.txt + Crawl-delay: NON disattivare
        ),
        content=content,
        links="pure",                   # selezione link euristica: niente modello semantico (più leggero)
        # enforce_ssrf_guard=True è il default: blocca gli host privati/interni.
    )
    print(f"[tg-deepseek] crawler attivo | content={content} (links=pure)", flush=True)
    return crawler.as_tools()


def main() -> None:
    token = _require("BOT_TOKEN")
    owner_id = int(_require("OWNER_ID"))
    _require("DEEPSEEK_API_KEY")  # letta internamente dal provider DeepSeek di LazyBridge

    model = os.environ.get("MODEL", "deepseek-v4-flash")
    db_path = os.environ.get("STORE_DB", "/data/pulse.db")
    tick_seconds = float(os.environ.get("TICK_SECONDS", "3"))
    reply_min_interval = float(os.environ.get("REPLY_MIN_INTERVAL", "2"))
    # Store persistente sempre acceso: senza retention cresce all'infinito
    # (record terminali + marker) e gli scan per-tick rallentano. 7 giorni di
    # default; i contatori di rate-limit chiusi vengono potati insieme.
    retention_seconds = float(os.environ.get("RETENTION_SECONDS", str(7 * 24 * 3600)))
    # Un task "running" più vecchio di così è dato per crashato e recuperato.
    # I task in attesa di approvazione HITL sono ``awaiting_review`` (non
    # ``running``), quindi non sono toccati da questo valore.
    stale_after = float(os.environ.get("STALE_AFTER", "600"))
    system = os.environ.get(
        "SYSTEM_PROMPT",
        "Sei un assistente personale conciso. Rispondi direttamente all'utente. "
        "Quando serve, usa i tool web per cercare e leggere pagine.",
    )

    client = TelegramClient.from_token(token)
    store = Store(db=db_path)                            # persistente: serve un Volume montato
    tools = _build_crawler_tools()   # aggiungi qui altri tool (Gmail, MCP, funzioni tue): vanno tutti in tools=[...]

    # Human-in-the-loop via Telegram: quando un task viene parcheggiato per
    # approvazione (``awaiting_review``), il reviewer manda un messaggio
    # all'owner ("/approve <id>" o "/reject <id>"). Le risposte dell'owner sono
    # intercettate come comandi (``command_filter``) invece di diventare task.
    # NB: perché un task venga parcheggiato serve che un'azione risulti
    # rischiosa — oggi, owner-only + READ_PUBLIC, tutto è auto-consentito.
    # Attiva l'HITL dando all'agente un tool di invio gattato, oppure impostando
    # un ``action_classifier`` che marchi i messaggi rischiosi (vedi sotto).
    reviewer = TelegramReviewer(client, store, owner_id=owner_id)

    pulse = PulseAgent(
        name="tg-deepseek",
        engine=LLMEngine(model, system=system),
        store=store,                                   # persistente: serve un Volume montato
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
        tools=tools,
        tick_seconds=tick_seconds,
        terminal_retention=retention_seconds,          # non far crescere lo Store all'infinito
        stale_after=stale_after,                       # recovery dei task crashati
        command_filter=reviewer.handle_command,        # intercetta /approve /reject dell'owner
        action_classifier=_build_action_classifier(),  # messaggi rischiosi → review HITL
    )

    print(
        f"[tg-deepseek] avvio | model={model} db={db_path} "
        f"tick={tick_seconds}s owner={owner_id} tools={len(tools)} "
        f"retention={retention_seconds:.0f}s hitl=on",
        flush=True,
    )

    # Loop "sempre acceso" + notifier HITL: il tick loop gira in un thread di
    # sfondo; qui annunciamo all'owner i task in attesa di approvazione. Un loop
    # asincrono separato dal tick loop — condividono solo lo Store (thread-safe).
    async def _notifier() -> None:
        while pulse.is_running():
            try:
                await reviewer.notify_pending()
            except Exception as exc:  # best-effort: non far cadere il bot
                print(f"[tg-deepseek] notifier error: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(tick_seconds)

    with pulse.running():
        try:
            asyncio.run(_notifier())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
