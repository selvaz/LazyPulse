"""Bot Telegram *always-on* — il launcher di LazyPulse più i tool dell'ecosistema.

Tutta la meccanica generale (inbox Telegram, policy owner-only, human-in-the-loop,
recovery, retention, calendario ricorrente) vive ora nel pacchetto, in
``lazypulse.launcher``, ed è configurata da variabili d'ambiente. Questo file
resta per l'unica cosa che il pacchetto non deve conoscere: i tool specifici
dell'ecosistema — LazyCrawler e il registry di LazyTools — che vengono costruiti
qui e passati a ``serve(tools=...)``.

Per un bot senza questi tool non serve questo file: basta ``lazypulse serve``.

Variabili d'ambiente: quelle del launcher (``lazypulse serve --help`` e il
docstring di ``lazypulse.launcher``) più quelle qui sotto.

    # --- crawler (LazyCrawler) ---
    ENABLE_CRAWLER     (opz.)  "1" (default) per dare i tool web all'agente; "0" per spegnerlo
    CRAWL_CONTENT      (opz.)  "pure" (default, leggero, zero token) | "ml" (modelli locali)
    CRAWL_MAX_PAGES    (opz.)  default: 5
    CRAWL_MAX_DEPTH    (opz.)  default: 1
    CRAWLER_DB         (opz.)  default: /data/crawler.db  (cache-first, sul Volume)

    # --- registry (LazyTools, catalogo artifact condiviso tra i repo) ---
    ENABLE_REGISTRY      (opz.)  "1" (default); "0" per spegnerlo (si spegne comunque
                                 da sola se lazytools non è installato)
    REGISTRY_ALLOW_WRITE (opz.)  "0" (default, sola lettura) | "1" abilita artifact_register
                                 (nessun gate HITL per-tool-call: vedi _build_registry_tools)
    PULSE_ARTIFACTS_DB   (opz.)  default: /data/pulse_artifacts.db  (sul Volume)
"""

from __future__ import annotations

import os

from lazypulse.launcher import serve


def _build_crawler_tools() -> list:
    """Costruisce i tool di LazyCrawler con limiti prudenti. Restituisce [] se disabilitato."""
    if os.environ.get("ENABLE_CRAWLER", "1") != "1":
        return []
    from lazycrawler import CrawlerDB
    from lazycrawler.config import CrawlerConfig, DBConfig
    from lazycrawler.tools import CrawlerTools

    content = os.environ.get("CRAWL_CONTENT", "pure")  # "pure" (zero token) | "ml" (modelli locali)
    crawler = CrawlerTools(
        db=CrawlerDB(DBConfig(db_path=os.environ.get("CRAWLER_DB", "/data/crawler.db"), ttl_hours=48.0)),
        crawler_cfg=CrawlerConfig(
            max_depth=int(os.environ.get("CRAWL_MAX_DEPTH", "1")),
            max_pages=int(os.environ.get("CRAWL_MAX_PAGES", "5")),
            respect_robots=True,  # rispetta robots.txt + Crawl-delay: NON disattivare
        ),
        content=content,
        links="pure",  # selezione link euristica: niente modello semantico (più leggero)
        # enforce_ssrf_guard=True è il default: blocca gli host privati/interni.
    )
    print(f"[tg-deepseek] crawler attivo | content={content} (links=pure)", flush=True)
    return crawler.as_tools()


def _build_registry_tools() -> list:
    """Tool del registry DB + catalogo artifact condiviso (LazyTools).

    Nessuna dipendenza/credenziale esterna: se ``lazytools`` non è installato,
    si spegne da sola invece di far fallire l'avvio del bot.

    Read-only di default (``registry_status``/``artifact_search``/
    ``artifact_get``), come il server MCP: l'HITL classifica il *messaggio* in
    ingresso una volta sola, non le singole tool call dell'LLM durante il run —
    un task già autorizzato come READ_PUBLIC (es. "riassumi questa pagina") gira
    con accesso pieno ai tool, quindi un contenuto crawlato con istruzioni
    iniettate potrebbe invocare ``artifact_register`` senza revisione.
    ``REGISTRY_ALLOW_WRITE=1`` abilita esplicitamente la scrittura per chi
    accetta questo rischio.
    """
    if os.environ.get("ENABLE_REGISTRY", "1") != "1":
        return []
    try:
        from lazytools.registry import RegistryTools
    except ImportError:
        return []
    # Come STORE_DB/CRAWLER_DB: un default sotto /data così, appena
    # REGISTRY_ALLOW_WRITE=1, artifact_register ha subito un posto dove scrivere
    # che sopravvive ai redeploy.
    os.environ.setdefault("PULSE_ARTIFACTS_DB", "/data/pulse_artifacts.db")
    allow_write = os.environ.get("REGISTRY_ALLOW_WRITE", "0") == "1"
    return RegistryTools(allow_write=allow_write).as_tools()


def main() -> None:
    serve(tools=_build_crawler_tools() + _build_registry_tools())


if __name__ == "__main__":
    main()
