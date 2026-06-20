# tg-deepseek-bot — bot Telegram always-on (LazyPulse + DeepSeek)

Bot Telegram conversazionale che gira **24/7** su [Railway](https://railway.app)
(o Render), gestibile **interamente da mobile**: codice via app GitHub, tutto il
resto dalla dashboard web.

- `bot.py` — il bot (`PulseAgent.serve()`, loop infinito).
- `Dockerfile` — immagine pronta (installa le 3 librerie dai repo pubblici).
- `requirements.txt` — dipendenze (API moderna da GitHub).

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
   → scegli **`selvaz/LazyPulse`**, branch **`claude/telegram-api-deepseek-test-t78da9`**.
2. Apri il servizio → **Settings → Root Directory** = **`deploy/tg-bot`**.
   Railway rileva il `Dockerfile` da solo.
3. **Variables** → aggiungi:
   - `BOT_TOKEN` = il token
   - `OWNER_ID` = il tuo user_id
   - `DEEPSEEK_API_KEY` = la chiave DeepSeek
   - *(opzionali)* `MODEL=deepseek-v4-flash`, `SYSTEM_PROMPT=...`, `TICK_SECONDS=3`
   - *(crawler)* `ENABLE_CRAWLER=1` (default), `CRAWL_CONTENT=pure` (o `ml`), `CRAWL_MAX_PAGES=5`, `CRAWL_MAX_DEPTH=1`
4. **Volume persistente** (fondamentale): aggiungi un Volume con **Mount path = `/data`**.
   È dove vive `pulse.db` (offset Telegram + dedupe + task). Senza, a ogni redeploy
   il bot "dimentica" lo stato.
5. **Una sola istanza**: in Settings tieni **Replicas = 1** e niente autoscaling.
   Due processi che pollano lo stesso bot causano `409 Conflict` su Telegram.
6. **Deploy**. Apri i **Logs**: deve comparire `[tg-deepseek] avvio | model=...`.
   Scrivi al bot su Telegram → ti risponde l'agente DeepSeek. ✅

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

## Note & troubleshooting

| Sintomo | Causa | Fix |
|---|---|---|
| Build fallisce sul `pip install` | `git` assente / rete | il `Dockerfile` installa `git`; ricontrolla i log di build |
| Il bot non risponde | mittente ≠ `OWNER_ID` | `TelegramPolicy` accetta solo l'owner verificato; controlla `OWNER_ID` |
| `409 Conflict` nei log | due poller sullo stesso bot | tieni **1 replica** e non far girare il bot anche altrove (Colab/locale) |
| Stato perso dopo un redeploy | Volume non montato | monta un Volume su `/data` (vedi passo 4) |
| Nessun `reasoning_content`/tool | thinking mode | il bot non usa thinking (corretto): DeepSeek in thinking non supporta i tool |

Il token Telegram e la chiave DeepSeek **vivono solo nelle Variables della
piattaforma**, mai nel codice committato.
