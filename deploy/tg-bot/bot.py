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
    SYSTEM_PROMPT      (opz.)          istruzioni di sistema dell'assistente

    # --- crawler (LazyCrawler) ---
    ENABLE_CRAWLER     (opz.)          "1" (default) per dare i tool web all'agente; "0" per spegnerlo
    CRAWL_CONTENT      (opz.)          "pure" (default, leggero, zero token) | "ml" (modelli locali)
    CRAWL_MAX_PAGES    (opz.)          default: 5
    CRAWL_MAX_DEPTH    (opz.)          default: 1
    CRAWLER_DB         (opz.)          default: /data/crawler.db  (cache-first, sul Volume)
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
    system = os.environ.get(
        "SYSTEM_PROMPT",
        "Sei un assistente personale conciso. Rispondi direttamente all'utente. "
        "Quando serve, usa i tool web per cercare e leggere pagine.",
    )

    client = TelegramClient.from_token(token)
    tools = _build_crawler_tools()   # aggiungi qui altri tool (Gmail, MCP, funzioni tue): vanno tutti in tools=[...]

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
        tools=tools,
        tick_seconds=tick_seconds,
    )

    print(
        f"[tg-deepseek] avvio | model={model} db={db_path} "
        f"tick={tick_seconds}s owner={owner_id} tools={len(tools)}",
        flush=True,
    )
    pulse.serve()   # loop infinito: questo è il "sempre acceso"


if __name__ == "__main__":
    main()
