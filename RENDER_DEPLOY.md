# Publish the hackathon demo on Render

## What is included

`GC_MODE=live` is the default and retains MERP access plus local CSV datasets.
`GC_MODE=demo` reads only `demo_data/<glid>.json`, with no MERP requests or credentials.
Both modes use the existing context renderer, role switching, conversation updates,
version history and History API event simulation. Sarvam remains a live external
dependency for chat, summaries and extracting conversation outcomes.

The initial bundle has **15 profiles: eight saved API responses and seven
dataset-only cases**, explicitly labelled in `demo_data/manifest.json`. Source
capture timestamps are retained in context metadata; regenerating a file does not
make these old source snapshots current. For 15 actual API captures, run the export
on the IndiaMART network before publishing:

```bash
app/.venv/bin/python app/demo_export.py
```

Inspect the manifest for `failed` entries. A successful capture replaces that GLID's
snapshot and includes selected buyer/seller CSV history. Do not upload the full
`datasets/`, `fixtures/`, `contexts/`, `memory/`, or `app/.env` directories.
Curated JSON retains names and business context; use a private repository.

## Test demo mode locally

Set these values in your existing `app/.env`:

```dotenv
GC_MODE=demo
GC_DEMO_DIR=demo_data
ENABLE_VOICE=true
```

Run `./start.sh`, select a saved GLID, and click **Prepare context**. The header says
**Context: saved JSON demo** and Flow labels the JSON source. Try chat, end it,
then restart to show continuity; use History API demo to open and close a ticket.
Unknown GLIDs use cold-start behavior without contacting MERP. Restore
`GC_MODE=live` for local/internal-server deployment. The runtime mode is an
environment setting, not a UI switch. Existing learned context is preserved.

## Push the prepared code to GitHub

From a normal writable checkout, inspect the files you stage and push your branch:

If this folder has no working Git repository, initialise it first (or copy the code
into a clone of your existing repository). For a new, empty GitHub repository:

```bash
git init
git branch -M main
git remote add origin https://github.com/mkundanjha/HackathonVoiceGlobalContext.git
git add .gitignore app buyer.schema.md seller.schema.md start.sh setup.sh AGENTS.md tests README.md demo_data render.yaml RENDER_DEPLOY.md
```

If the remote already contains commits, clone it and copy the project files into
that checkout instead; preserve its history. For an existing checkout, stage the
changed files:

```bash
git add app/context_api.py app/view_builder.py app/renderer.py app/console.py app/static/console.html
git add app/hosted_voice.py app/static/hosted_voice.js app/requirements.txt app/pyproject.toml
git add app/demo_export.py app/.env.example app/requirements-render.txt
git add demo_data render.yaml RENDER_DEPLOY.md README.md tests/test_demo_mode.py tests/test_hosted_voice.py tests/test_hosted_voice.js
git diff --cached --stat
git commit -m "Add offline context demo mode and Render deployment"
git push origin HEAD
```

If GitHub requests a password, authenticate using GitHub CLI (`gh auth login`, then
`gh auth setup-git`) or your configured SSH key. Keep credentials out of repository
files and remote URLs. `demo_data/` is intentionally versioned; ordinary API fixtures
and generated files remain ignored.

## Deploy using the supplied Blueprint

1. Sign in to Render and connect your GitHub account.
2. Select **New → Blueprint**, then this repository and the branch containing these changes.
3. Render reads the root `render.yaml`. Enter your `SARVAM_API_KEY` when prompted.
4. Deploy the free web service and wait for its health check to pass.
5. Open its HTTPS `onrender.com` URL. Confirm `/healthz` returns `{"ok":true}` and
   `/api/runtime` reports `context_mode: demo` and 15 GLIDs.

Blueprint settings follow Render's [Blueprint reference](https://render.com/docs/blueprint-spec).
For a manual **New → Web Service** deployment, leave Root Directory blank and use:

| Setting | Value |
|---|---|
| Runtime | Python 3 |
| Plan | Free |
| Build command | `pip install -r app/requirements-render.txt` |
| Start command | `cd app && uvicorn console:app --host 0.0.0.0 --port $PORT --workers 1` |
| Health check | `/healthz` |
| Python version | `PYTHON_VERSION=3.12.11` |
| Environment | `GC_MODE=demo`, `GC_DEMO_DIR=demo_data`, `ENABLE_VOICE=true`, `VOICE_TRANSPORT=websocket`, `SARVAM_API_KEY=<your key>` |

Use one worker because chat sessions are in process memory. Binding to `0.0.0.0`
and `$PORT` follows Render's [FastAPI guide](https://render.com/docs/deploy-fastapi).
The smaller requirements file excludes the local Pipecat/audio stack. No MERP
credentials or `.env` file are needed on Render.

## Voice and persistence limits

This Blueprint hosts **chat, browser voice, context files and history-event demos**.
Hosted voice uses `VOICE_TRANSPORT=websocket`: microphone audio is split into short
utterances in the browser and sent to `/api/voice/ws` on the same HTTPS host. The
server calls Sarvam STT (`saaras:v4`, Roman/Hinglish transcription), the configured
LLM, and TTS (`bulbul:v3`) for each turn. Role switching, voice transcripts and
context updates use the same `RoleConversation` controller as local voice.

Click **Call**, allow microphone access, and wait for the personalised opening.
Speak after the agent finishes; the hosted demo waits for a short pause before
submitting your utterance. It supports one call at a time, without interrupting
the agent mid-sentence. Hang up saves the conversation; after the file update
finishes, resume in chat or another call. Live voice latency has not been measured.
Sarvam speech API access/quota is required in addition to working chat access.

Local streaming Pipecat/WebRTC remains available with `VOICE_TRANSPORT=webrtc`
(the default), the full dependencies and `ENABLE_VOICE=true`. Hosted voice uses
secure WebSockets because Render routes public traffic through
[one port](https://render.com/docs/websocket).

### Update an existing chat-only Render deployment

1. Upload/push the new `app/hosted_voice.py`, `app/static/hosted_voice.js`, updated
   `app/console.py`, `app/static/console.html`, and `app/requirements-render.txt`.
2. In your service's **Environment**, set `ENABLE_VOICE=true` and
   `VOICE_TRANSPORT=websocket`; leave `GC_MODE=demo` and the Sarvam key in place.
3. Choose **Save, rebuild, and deploy** (or manually deploy the latest commit).
   Updating an environment value without deploying the new code is insufficient.
4. Reload the browser. `/api/runtime` should now report `voice_enabled: true` and
   `voice_transport: websocket`. Test **Call**, microphone permission, a buyer/seller
   role switch, hangup, and context continuity after the updater finishes.
5. If a call fails, note the stage shown in the UI (speech recognition, agent reply,
   or speech synthesis) and check Render logs and your Sarvam speech quota.

Free services sleep after 15 minutes of inactivity and lose filesystem changes on
restart, redeploy or sleep. Generated Markdown, transcripts and SQLite demo events
therefore persist only while this instance stays running. Committed input snapshots
are restored on deployment. Open the demo shortly before presenting and keep its tab
open. For durable history, add storage separately; this application does not
integrate an external database. See [Render's free-tier limits](https://render.com/docs/free).

The console is a shared prototype with no login; use one presenter/session at a time.
Anyone with the deployed URL can access its context and transcript endpoints.
Hosting can be free; Sarvam usage follows your API account's quota/pricing.
