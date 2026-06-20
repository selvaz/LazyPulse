# Deploy su VPS Hetzner + Coolify — guida mobile, passo-passo

Obiettivo: bot Telegram **always-on**, **prezzo fisso ~€4–5/mese**, **4 GB di RAM**,
gestione **da telefono** (dashboard web, come Render) — senza mai usare SSH.

Stack: **Hetzner CX22** (VPS) + **Coolify** (pannello PaaS self-hosted che installiamo
in automatico) che deploya questa cartella `deploy/tg-bot/` dal repo GitHub.

> Ti serve a portata di mano: `BOT_TOKEN` (@BotFather), `OWNER_ID` (il tuo
> user_id Telegram), `DEEPSEEK_API_KEY` (platform.deepseek.com).

---

## Passo 1 — Crea il server Hetzner (dall'app o dal browser del telefono)

1. Registrati su **https://console.hetzner.cloud** (o scarica l'app *Hetzner Cloud*).
2. **New Project** → dagli un nome (es. `bot`).
3. **Add Server** e imposta:
   - **Location**: una EU (es. Falkenstein/Norimberga).
   - **Image**: **Ubuntu 24.04**.
   - **Type**: **CX22** (2 vCPU / 4 GB / 40 GB) — *shared vCPU, x86*.
   - **SSH key**: puoi **saltarla** (non ci serve: useremo solo la dashboard web).
4. Apri la sezione **Cloud config / User data** e **incolla esattamente** questo:

   ```yaml
   #cloud-config
   package_update: true
   package_upgrade: true
   runcmd:
     - curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash
   ```

   (Installa Docker + Coolify da solo al primo avvio — **niente SSH**.)
5. **Create & Buy now**. Annota l'**IP pubblico** che ti mostra.

> Attendi **~5–8 minuti**: il server si accende e installa Coolify in background.

### (Consigliato) Firewall Hetzner
Nel progetto → **Firewalls** → crea una regola **Inbound** che consente solo:
`22` (SSH, opz.), `80`, `443`, `8000` (UI Coolify). Applicala al server. Chiude tutto il resto.

---

## Passo 2 — Primo accesso a Coolify (dal browser del telefono)

1. Dopo ~5–8 min apri: **`http://IL_TUO_IP:8000`**
   (se dà errore, aspetta ancora un paio di minuti: sta finendo l'install).
2. Crea **subito** l'account admin (email + **password forte**): è il tuo pannello, esposto in rete.
3. Salta/segui l'onboarding finché arrivi alla dashboard. Il server locale ("localhost")
   è già pronto come destinazione di deploy.

---

## Passo 3 — Deploya il bot da GitHub

1. **+ New** → **Project** → dagli un nome → entra → **+ New Resource**.
2. Scegli **Public Repository** (il repo `LazyPulse` è pubblico).
   - **Repository URL**: `https://github.com/selvaz/LazyPulse`
   - **Branch**: `claude/telegram-api-deepseek-test-t78da9`
3. Nelle impostazioni del servizio:
   - **Build Pack**: **Dockerfile**
   - **Base Directory**: **`/deploy/tg-bot`**  (qui stanno Dockerfile e bot.py)
   - **Ports / Domain**: lascialo **vuoto** e **disattiva l'health check** — è un *worker*,
     non un sito web (non espone porte HTTP).
4. **Environment Variables** → aggiungi:
   | Key | Value |
   |---|---|
   | `BOT_TOKEN` | il token di @BotFather |
   | `OWNER_ID` | il tuo user_id Telegram |
   | `DEEPSEEK_API_KEY` | la chiave DeepSeek |
   | *(opz.)* `MODEL` | `deepseek-v4-flash` |
   | *(opz.)* `CRAWL_CONTENT` | `pure` (o `ml`, qui hai 4 GB) |
5. **Persistent Storage** → **+ Add** un volume:
   - **Mount Path**: **`/data`**  (è dove vivono `pulse.db` e `crawler.db`)
6. Premi **Deploy**.

---

## Passo 4 — Verifica

1. Apri la tab **Logs** del servizio in Coolify: deve comparire
   `[tg-deepseek] avvio | model=... tools=...`.
2. Scrivi al bot su Telegram → ti risponde l'agente DeepSeek. 🎉

---

## Uso quotidiano (tutto da mobile)

- **Aggiornare il bot**: modifica un file in `deploy/tg-bot/` dall'**app GitHub** →
  commit → in Coolify premi **Redeploy** (o attiva l'auto-deploy via webhook).
- **Cambiare un parametro** (es. `CRAWL_CONTENT=ml`): cambia la env var in Coolify → **Redeploy**.
- **Log / restart / risorse**: tutto dalla dashboard Coolify nel browser del telefono.

---

## Troubleshooting

| Sintomo | Causa | Fix |
|---|---|---|
| `http://IP:8000` non apre | install non finito / firewall | aspetta 2–3 min; apri la porta 8000 nel firewall Hetzner |
| Deploy fallito sul build | `git`/rete | i repo sono pubblici; rilancia il deploy e leggi i Build Logs |
| Bot non risponde | mittente ≠ `OWNER_ID` | la policy accetta solo l'owner verificato; controlla `OWNER_ID` |
| `409 Conflict` nei log | due istanze sullo stesso bot | **una sola** istanza; non far girare il bot anche altrove |
| Stato perso dopo redeploy | volume non montato | aggiungi Persistent Storage su `/data` (Passo 3.5) |
| RAM al limite | `CRAWL_CONTENT=ml` pesante | con 4 GB ci sta; se serve, torna a `pure` |

> Sicurezza: token e chiavi vivono **solo nelle Environment Variables di Coolify**,
> mai nel codice. Usa una **password admin forte** per Coolify e tieni il **firewall** attivo.
