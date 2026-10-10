# Global Context: Memory Across Channels (PS02)

IndiaMART Voice AI Hackathon 2.0. A GLID-keyed context layer that gives the voice and chat assistant ("Payal") a ready `buyer.md` / `seller.md` before it speaks, so it opens with something personal, never re-asks what it knows, and continues across voice and chat. Every conversation is written back into the file.

## Quick start

For hosting without MERP access, see [Render deployment and offline demo mode](RENDER_DEPLOY.md).
`GC_MODE=demo` uses curated JSON profiles; the default `GC_MODE=live` keeps internal API access.
On Render, set `ENABLE_VOICE=true` and `VOICE_TRANSPORT=websocket` for browser voice
through the hosted URL. This uses Sarvam STT/LLM/TTS one utterance at a time; speak
after the agent finishes. Local streaming voice keeps the default `VOICE_TRANSPORT=webrtc`.

```bash
./setup.sh      # once: Python env, dependencies, app/.env
./start.sh      # every time: starts the console
```

Open **http://localhost:8000**, pick a user (e.g. `7444262 · seller`), click **Prepare context**, then **Call** or **Start chat**.

## Requirements

- Linux or macOS with Python **3.11 or newer** (tested on 3.14)
- A **Sarvam API key** (speech-to-text, LLM, text-to-speech)
- Chrome or Firefox with a microphone. Use **headphones** for voice calls: speaker echo can interrupt the bot.
- Optional: IndiaMART network or VPN for the Global Context API. Without it, files are built from the local datasets.

## Setup in detail

`./setup.sh` does everything below and is safe to re-run:

1. Checks Python 3.11+ (choose another with `PYTHON=python3.12 ./setup.sh`).
2. Creates `app/.venv` and installs `app/requirements.txt` (pipecat-ai 1.12.0 and sarvamai 0.1.28 are pinned).
3. Creates `app/.env` from `app/.env.example` if it does not exist, and fills `SARVAM_API_KEY` from `sarvam ai details/sarvam api key` (or asks for it).
4. Checks the datasets and schema files, and creates `memory/` and `contexts/`.

Then edit `app/.env` if you use the Context API:

| Variable | Needed | Notes |
|---|---|---|
| `SARVAM_API_KEY` | Yes | Filled by setup |
| `GC_EMPID`, `GC_AK`, `GC_FORWARDED_FOR` | Optional | Global Context API. **`GC_AK` expires every 24 h**; paste a fresh one here. |
| `GC_BASE_URL`, `GC_SOURCE_ID` | Optional | Defaults are set |
| `DEMO_GLID`, `DEMO_ROLE` | No | Only when running `voice_agent.py` by hand |

Never commit `app/.env` or paste keys into chats; `.gitignore` already excludes them.

## Using the console

The selected Buyer/Seller role remains the session's origin. During chat or voice,
the user's buying needs switch to buyer context; their selling-business concerns
switch to seller context. A Sarvam intent check runs before each user reply, adding
one small model request per turn. The other role's file is loaded on demand. After
handling the side need, the assistant resumes an unfinished original-role question
without restarting the greeting. Stop requests end the interaction instead.

Conversation segments are saved when changing roles and at chat/call end. Each
segment updates only its matching Markdown file, creating it when needed. For
example, a seller's purchase requirement goes to `buyer.md`, while their lead
complaint goes to `seller.md`. The console's main role selector remains the origin.
Use **Buyer MD / Seller MD** inside **Context file** to inspect either file and its
version history without ending the conversation or changing its role. Changing the
selected user resets this file toggle to the main selected role.

| Area | What it does |
|---|---|
| **User** | GLID, buyer/seller, quick picks. `900000000001` is a new user with no history (cold start). |
| **Prepare context** | Finds or builds a concise file. **Rebuild from sources** refreshes API + dataset facts while preserving learned conversation facts and open commitments; old versions stay in history. |
| **Call** | Starts the voice agent for this user and connects your microphone. **Hang up** ends it. |
| **Start chat** | Text chat with the same context and memory. **End chat** saves it. |
| **Flow** | Live view of each step: lookup, Context API, datasets, LLM, guide, prompt, conversation, memory, file update, with timings. |
| **Context file** | The file the bot reads. Blue: Conversation Guide; purple: learned facts. **Changes since vN** shows at most three brief updates, with next steps first. **View details** expands the full comparison. Green highlights mark changed facts; timestamps, source tags and guide rewrites are ignored in the comparison. |
| **Memory** | Saved transcripts. **Reset** clears them (the context file is not changed). |

### Demo script (about 3 minutes)

1. Pick `7444262 · seller` → **Prepare context**. The Flow shows API → datasets → LLM → file → guide; the file appears on the right.
2. **Start chat**: the opening uses real context. Say something new ("Friday tak 500 kg LD liner bags chahiye"), then **End chat**. Watch **Update file** add it (green box: what changed).
3. **Call**: the bot continues from the chat instead of starting over. Ask it to call back later, hang up, call again: it picks up the callback.
4. Pick **new user** to show the generic cold-start opening.
5. Show the same `.md` file as the non-bot use (executive call prep).

## How it works

### History API demo

Expand **History API demo** beneath Flow. Choose **New enquiry**, **New available lead**, **Open ticket**, or **Close ticket**, then click **Save demo activity**. Enquiries use product, city, quantity and unit; leads use product and city. Opening a ticket requires a numeric ticket ID and issue; closing requires only the ID. Ticket and lead options apply to sellers; buyers support enquiries only. Available leads represent opportunities, not purchases. Tickets affect the local demo context, not IndiaMART's actual ticket system.

The local flow shows **History saved → Event emitted → Context updating → File updated**. The actual context file receives explicitly labelled demo facts, a new version, and an updated opening. The comparison automatically selects the previous version. Start the next call/chat to use the new context; an already active session keeps its original prompt.

Results show save-to-file and event-receipt-to-file delay, plus p50/p95 for completed events for the selected user/role. Failed updates show **Retry update**; payloads and errors are under **Details**. New calls/chats wait until pending history activity finishes.

Activities and jobs persist in `memory/demo_history.sqlite3`; unfinished jobs recover after restarting the console. Refreshes and conversation updates preserve the latest three enquiry/lead activities from the last 30 days. The latest ticket state per ID is retained without expiry, so closed tickets do not reopen from an older API snapshot. Closing one ticket leaves other tickets unchanged; opening the same ID again restores priority. Durable conversation commitments remain separately preserved. This is a local simulation: it makes no production History API writes or source API calls during event processing.

Endpoints: `POST /api/demo/history`, `GET /api/demo/history/{event_id}`, `GET /api/demo/history/status?glid=...&role=...`, and `POST /api/demo/history/{event_id}/retry`. Every save includes `event_id`, `glid`, `role`, and `activity_type`. Types `enquiry.created` and `lead.available` require `product` and `city`; enquiries additionally require `quantity` and `unit`. Types `ticket.opened` and `ticket.closed` require `ticket_id`; opening additionally requires `issue`. Repeated IDs with identical inputs are deduplicated; conflicting reuse returns HTTP 409.

```
Call / chat for GLID + role
  → find context file: contexts/ (fresh sources) → samples/ (first lookup only) → refresh/build
       build = Context API (generateContextUID → getContext) + datasets → redact
               → template (renderer.py) → compact whole bullets → immediate template guide → role.md
               → background: LLM one-line summaries of free text (summarizer.py, cached per record) → role.md v+1
  → ensure "## Conversation Guide" (opening, continue-from, talking point; optional LLM writer)
  → system prompt = persona + rules + context file + last transcripts   (agent_config.yaml)
  → conversation (voice: Sarvam STT → LLM → TTS over WebRTC | chat: Sarvam LLM)
  → hang up / end chat → transcript to memory/ → LLM extracts new facts + follow-ups
  → corrections replace exact old bullets; completed/cancelled follow-ups removed; current facts merged
  → compact role.md, guide rewritten, version + 1, old version in history/
  → next conversation on either channel starts from the updated file
```

### Where the context file lives

| Path | What |
|---|---|
| `samples/<glid>/<role>.md` | Hand-written examples. Never edited; the first update copies them to `contexts/`. |
| `contexts/<glid>/<role>.md` | Generated or updated file the bot uses |
| `contexts/<glid>/history/<role>.vN.md` | Every earlier version |
| `contexts/<glid>/summaries.json` | Cached one-line summaries of free-text records (enquiry threads, WhatsApp chats, transcripts) |
| `memory/<glid>.jsonl` | Raw conversation transcripts |
| `memory/events.jsonl`, `memory/voice_agent.log` | Flow events and voice agent log |
| `buyer.schema.md`, `seller.schema.md` | Section rules the builder follows |

## Configuration: `app/agent_config.yaml`

Everything you might tune, re-read on every call or chat (no restart needed):

| Section | Settings |
|---|---|
| `persona` | name, company, language, tone |
| `call_purpose` | why the assistant contacts a seller / buyer (drives openings and talking points) |
| `llm` | conversation model; summary, updater and guide models and timeouts; LLM builder (only for `builder: llm`) |
| `voice` | TTS voice, language, pace; STT model and language |
| `turn_taking` | words needed to interrupt the bot; voice-detection sensitivity |
| `memory` | how many past transcripts go into the prompt |
| `context_files` | `builder: template` (default) or `llm`; max file age; items per section; rows read per table |
| `prompts` | system prompt, openings, cold start, guide writer, template fallback openings, summaries, post-conversation updater |

`context_files.compact` sets a 450-word body target, two source bullets per section, and two recent conversation summaries. The guide is additional. Current learned facts and unresolved commitments can exceed the target; complete bullets preserve dates, quantities, and source tags. Durable learned state is stored in front matter (excluded from the agent prompt), and previous versions retain omitted detail. Existing generated files are compacted on their next lookup; samples remain unchanged.

`context_files.guide_mode: template` creates guides without an LLM request; set it to `llm` for the optional writer. Source freshness uses `source_refreshed_at`, independent of conversation/guide writes. Cached summaries are accepted only when their source-text hash matches. CSVs are indexed per process and reloaded when files change. Flow prompt events report complete prompt characters, words, and preparation time; these are not model token counts.

Run offline regression checks from the repository root:

```bash
app/.venv/bin/python -m unittest discover -s tests -v
python3 -m compileall -q app tests
```

## Command-line tools

Every successful Context API fetch saves its complete response data, before Markdown pruning, to `fixtures/<glid>/userctx.redacted.json`. The file contains the fetch time, requested segments, timings, and readable JSON; contact details and credentials are redacted. Each fetch replaces the previous copy. Cached context lookups do not fetch or rewrite this file; choose **Rebuild from sources** to request a new copy. Captured responses are gitignored.

Run from `app/` after `source .venv/bin/activate`:

```bash
python view_builder.py 7444262 seller          # find or build a context file
python view_builder.py 7444262 seller --force  # rebuild from sources
python guide.py 35388172 seller                # rewrite a file's Conversation Guide
python context_api.py 271504729                # fetch the API and save a redacted copy to fixtures/
python voice_agent.py                          # voice agent alone (DEMO_GLID in .env), client at :7860/client
```

## Troubleshooting

| Problem | Fix |
|---|---|
| Call fails with `Unsupported model 'bulbul:v4-flash'` | pipecat 1.12 supports `bulbul:v3`. Set `voice.tts_model: bulbul:v3` in `agent_config.yaml`. |
| Bot stops mid-sentence | Echo or noise interrupting it. Use headphones, or raise `turn_taking.interrupt_min_words`. |
| Microphone permission denied | Allow the microphone for `localhost:8000` in the browser and click **Call** again. |
| "Port 7860 is busy" | A `voice_agent.py` started by hand is still running. Stop it. |
| Context API "unavailable" in the Flow | VPN off or `GC_AK` expired. Refresh `GC_AK` in `app/.env`; files still build from datasets. |
| Building a file is slow | Check `context_files.builder: template` (file in ~1 s; summaries follow in the background). `llm` writes the whole file with an LLM (~6–10 s). |
| Messages show as "quoted…" text | Their summaries are not ready yet (Flow: Summaries) or the LLM skipped them; they are retried on the next build. |
| Bot uses the wrong user | Restart the console after changing code; the console passes the GLID to the agent as `VOICE_GLID`. |
| Anything else | See the red box in the console, `memory/voice_agent.log`, or the Event log at the bottom of the page. |

## Project layout

```
setup.sh, start.sh          install and run
README.md                   this file
final-plan.md               design notes
buyer.schema.md, seller.schema.md
app/
  console.py                console server (UI, voice agent control, APIs)    static/console.html
  voice_agent.py            Pipecat voice pipeline (Sarvam STT, LLM, TTS)
  chat_app.py               chat channel
  context.py                system prompt + memory
  view_builder.py           find / build context files, file lock, history
  renderer.py               template writer for the 9 sections (no LLM)
  summarizer.py             background LLM summaries of free text, cached
  context_api.py            Global Context API client (+ redaction)
  guide.py                  Conversation Guide
  context_updater.py        learn from each conversation
  events.py, config.py      flow events, config loader
  agent_config.yaml         all tunable settings
  requirements.txt, .env.example
samples/  contexts/  memory/  datasets/
```
