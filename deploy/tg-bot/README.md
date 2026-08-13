# tg-deepseek-bot — bot Telegram always-on (LazyPulse + DeepSeek)

Bot Telegram conversazionale che gira **24/7** su [Railway](https://railway.app)
(o Render), gestibile **interamente da mobile**: codice via app GitHub, tutto il
resto dalla dashboard web.

- `bot.py` — involucro sottile: costruisce i tool di LazyCrawler e del registry
  e li passa a `lazypulse.launcher.serve`.
- `loadtest.py` — harness di test/stress (offline, senza token: throughput,
  dedup, flusso HITL, recovery, retention). Vedi in fondo.
- `Dockerfile` — immagine pronta (installa le 3 librerie dai repo pubblici).
- `requirements.txt` — dipendenze (API moderna da GitHub).

> **Il motore del bot vive nel pacchetto.** Inbox Telegram, policy owner-only,
> human-in-the-loop, recovery, retention e calendario ricorrente stanno in
> `lazypulse.launcher`, configurati da variabili d'ambiente e documentati in
> [Launcher](https://pulse.lazybridge.com/launcher/). Qui resta solo ciò che il
> pacchetto non deve conoscere: i tool dell'ecosistema.
>
> Se non ti servono crawler e registry, questo file non serve affatto —
> `lazypulse serve` fa tutto il resto.

**Lavoro ricorrente:** imposta `CALENDAR_FILE` a un TOML di schedule (orari,
giorni di borsa, dipendenze fra job) e il bot lo esegue. Validalo prima con
`lazypulse check-calendar <file>`. Perché un task schedulato ti raggiunga deve
usare il tool `notify_owner`: non ha una conversazione in cui rispondere.

**Human-in-the-loop:** quando chiedi al bot un'azione rischiosa (parole come
*invia, manda, cancella, paga* — configurabili), il task viene parcheggiato e il
bot ti manda un messaggio: rispondi **`/approve <id>`** o **`/reject <id>`**.
Solo l'owner può approvare.

---

## Cosa ti serve prima (una tantum)

| Valore | Dove |
|---|---|
| `BOT_TOKEN` | @BotFather → `/newbot` (es. `123456789:AAE...`) |
| `OWNER_ID` | scrivi al bot, apri `https://api.telegram.org/bot<TOKEN>/getUpdates`, leggi `message.from.id` |
| `DEEPSEEK_API_KEY` | platform.deepseek.com (`sk-...`) |

---

## Deploy su Railway — passi da mobile

1. **railway.app** → accedi con GitHub → **New Project** → **Deploy from GitHub repo**
   → scegli **`selvaz/LazyPulse`**, branch **`main`**.
2. Apri il servizio → **Settings → Root Directory** = **`deploy/tg-bot`**.
   Railway rileva il `Dockerfile` da solo.
3. **Variables** → aggiungi:
   - `BOT_TOKEN` = il token
   - `OWNER_ID` = il tuo user_id
   - `DEEPSEEK_API_KEY` = la chiave DeepSeek
   - *(opzionali)* `MODEL=deepseek-v4-flash`, `SYSTEM_PROMPT=...`, `TICK_SECONDS=3`
   - *(store always-on)* `RETENTION_SECONDS=604800` (7g, pota record + rate-bucket), `STALE_AFTER=600`
   - *(HITL)* `REVIEW_KEYWORDS=invia,manda,cancella,paga,...` (default attivo; `REVIEW_KEYWORDS=` per spegnerlo)
   - *(crawler)* `ENABLE_CRAWLER=1` (default), `CRAWL_CONTENT=pure` (o `ml`), `CRAWL_MAX_PAGES=5`, `CRAWL_MAX_DEPTH=1`
4. **Volume persistente** (fondamentale): aggiungi un Volume con **Mount path = `/data`**.
   È dove vive `pulse.db` (offset Telegram + dedupe + task). Senza, a ogni redeploy
   il bot "dimentica" lo stato.
5. **Una sola istanza**: in Settings tieni **Replicas = 1** e niente autoscaling.
   Due processi che pollano lo stesso bot causano `409 Conflict` su Telegram.
6. **Deploy**. Apri i **Logs**: deve comparire `[tg-deepseek] avvio | model=... hitl=on`.
   Scrivi al bot su Telegram → ti risponde l'agente DeepSeek. ✅ Chiedi qualcosa
   di "rischioso" (es. *"invia il report a Bob"*) → il bot ti chiede
   `/approve <id>` prima di procedere.

> **Render**, in alternativa: crea un **Background Worker** (non un Web Service,
> così non "dorme"), *Root Directory* `deploy/tg-bot`, runtime Docker, aggiungi un
> **Disk** montato su `/data`, stesse env var, 1 istanza.

---

## Aggiornare il bot (da mobile)

Modifica un file in `deploy/tg-bot/` dall'**app GitHub** (o `github.dev`) →
**commit** → Railway/Render fanno **redeploy automatico**. Niente SSH.

---

## Crawler web (LazyCrawler) — attivo, ma "educato"

Il bot dà all'agente i tool `web_search` / `web_crawl`. È configurato per stare
nei limiti di un uso lecito (e quindi accettato da Railway/Render):

- **`respect_robots=True`** — rispetta `robots.txt` e il `Crawl-delay`. **Non disattivarlo.**
- **rate-limit per host** sempre attivo (anti-martellamento).
- **SSRF guard** attivo: non può colpire host privati/interni.
- gira **solo quando lo chiedi tu** (solo l'owner attiva il worker), con `max_pages`/`max_depth` piccoli.
- **cache-first**: i risultati restano in `/data/crawler.db` (sul Volume), meno richieste ripetute.

Manopole utili (env var): `CRAWL_CONTENT=pure` (default, **zero token LLM**, leggero) o
`ml` (riassunti/entità con **modelli locali**, più RAM); `ENABLE_CRAWLER=0` per spegnerlo.

> Sul trial **512 MB** parti con `pure`. Evita: disattivare `respect_robots`,
> alzare `max_pages` a decine/centinaia, o crawl massivi in loop → è quello che
> Railway considera "scraping abusivo".

## Test & stress test (offline, senza token)

Prima di deployare — o dopo una modifica — verifica tutto in locale con
`loadtest.py`: guida i componenti **reali** (inbox + agente + reviewer) con un
client Telegram finto, senza rete.

```bash
python deploy/tg-bot/loadtest.py                 # stress offline (default: 2000 msg)
python deploy/tg-bot/loadtest.py --messages 5000 # più carico
python deploy/tg-bot/loadtest.py --live          # smoke test live (serve BOT_TOKEN, OWNER_ID)
```

Controlla: **throughput + dedup**, **HITL** (approve/reject via /comando),
**recovery senza doppia esecuzione**, **retention** che limita lo Store. La
modalità `--live` è un semplice round-trip gentile: lo stress pesante va fatto
offline (Telegram rate-limita/sospende sotto carico).

## Note & troubleshooting

| Sintomo | Causa | Fix |
|---|---|---|
| Build fallisce sul `pip install` | `git` assente / rete | il `Dockerfile` installa `git`; ricontrolla i log di build |
| Il bot non risponde | mittente ≠ `OWNER_ID` | `TelegramPolicy` accetta solo l'owner verificato; controlla `OWNER_ID` |
| Chiedi un'azione ma "non fa niente" | è in attesa di approvazione HITL | controlla i messaggi del bot e rispondi `/approve <id>`; o allenta `REVIEW_KEYWORDS` |
| Lo Store cresce troppo | retention non impostata | imposta `RETENTION_SECONDS` (default 7g già attivo nel bot) |
| `409 Conflict` nei log | due poller sullo stesso bot | tieni **1 replica** e non far girare il bot anche altrove (Colab/locale) |
| Stato perso dopo un redeploy | Volume non montato | monta un Volume su `/data` (vedi passo 4) |
| Nessun `reasoning_content`/tool | thinking mode | il bot non usa thinking (corretto): DeepSeek in thinking non supporta i tool |

Il token Telegram e la chiave DeepSeek **vivono solo nelle Variables della
piattaforma**, mai nel codice committato.
