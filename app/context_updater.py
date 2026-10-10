"""After a call or chat: extract what is new and write it into the GLID's buyer.md / seller.md.

The LLM only proposes short items (JSON); code inserts them as dated bullets under the
right section, keeps the previous version in contexts/<glid>/history/, and bumps
context_version. Runs under view_builder.file_lock, so it never races a summary pass. Hand-written samples/ files are never edited: the first update copies
the sample into contexts/, and that updated copy is used from then on.
"""

import json
import os
import re
import threading
import time
from pathlib import Path

from loguru import logger
from sarvamai import SarvamAI

from config import load_config
from events import emit
from guide import add_guide, strip_guide
from renderer import SECTIONS, SUMMARY_BULLET, insert_bullets, _sections_of
from compact import COMPACT_VERSION, compact_body, learned_state, merge_learned
from support_state import reconcile_support
from view_builder import CONTEXTS_DIR, ROOT, SAMPLES_DIR, file_lock, read_view_file, save_history, write_view_file

CONVERSATIONS = "Cross-Channel Conversations"
PENDING = "Pending Actions and Clarifications"
MAX_ITEMS = 6


def updating_marker(glid: str, role: str) -> Path:
    """Exists while a conversation is being written into the file; the next call waits for it."""
    return CONTEXTS_DIR / glid / f".{role}.updating"


def update_in_background(glid: str, role: str, channel: str, turns: list[dict]) -> None:
    """Non-daemon thread, so a voice hang-up handler returns immediately but the update still finishes."""
    if not any(t["speaker"] == "user" for t in turns):
        emit("update", "skip", glid, role, reason="user said nothing")
        return
    # Create the marker before returning, so a call started right after hang-up already sees it.
    marker = updating_marker(glid, role)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    threading.Thread(target=update_from_conversation, args=(glid, role, channel, turns)).start()


def update_from_conversation(glid: str, role: str, channel: str, turns: list[dict]) -> None:
    try:
        with file_lock(glid, role):
            _update(glid, role, channel, turns)
    except Exception as e:
        logger.exception("Context update failed")
        emit("update", "error", glid, role, error=f"{type(e).__name__}: {e}"[:300])
    finally:
        updating_marker(glid, role).unlink(missing_ok=True)


def _current_file(glid: str, role: str) -> tuple[dict, str, str | None]:
    """(front matter, body, based_on) of the file the bot is using now, or a blank skeleton."""
    generated = CONTEXTS_DIR / glid / f"{role}.md"
    if generated.exists():
        meta, body = read_view_file(generated)
        return meta, body, meta.get("based_on")
    sample = SAMPLES_DIR / glid / f"{role}.md"
    if sample.exists():
        meta, body = read_view_file(sample)
        return meta, body, str(sample.relative_to(ROOT))
    # Cold start: the conversation itself becomes the first context.
    title = "# Buyer Context" if role == "buyer" else "# Seller Context"
    body = title + "\n\n" + "\n\n".join(f"## {s}\n- Unknown." for s in SECTIONS[role])
    meta = {"schema_version": 2, "glid": glid, "role": role, "context_version": 0, "mode": "conversation_created"}
    return meta, body, None


def _extract(role: str, body: str, channel: str, turns: list[dict]) -> dict:
    cfg = load_config()
    transcript = "\n".join(f"{'User' if t['speaker'] == 'user' else 'Bot'}: {t['text']}" for t in turns)
    system = (
        cfg["prompts"]["update"]
        .replace("{role}", role)
        .replace("{sections}", "\n".join(f"- {s}" for s in SECTIONS[role]))
        .replace("{today}", time.strftime("%A, %d %b %Y"))
    )
    user = f"Current {role}.md:\n{body[:12000]}\n\n{channel} conversation that just ended:\n{transcript[-6000:]}"
    client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"],
                      timeout=float(cfg["llm"].get("updater_timeout_seconds", 20)))
    response = client.chat.completions(
        model=cfg["llm"]["updater_model"],
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=1500,
    )
    text = re.sub(r"<think>.*?</think>", "", response.choices[0].message.content or "", flags=re.DOTALL)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON in update output: {text[:200]!r}")
    return json.loads(match.group(0))


def _update(glid: str, role: str, channel: str, turns: list[dict]) -> None:
    emit("update", "start", glid, role, channel=channel)
    t0 = time.perf_counter()
    meta, body, based_on = _current_file(glid, role)
    old_body = body
    state = learned_state(body, meta)
    # Include every open commitment, even when the display budget limits older summaries.
    body = merge_learned(strip_guide(body), state, meta.get("suppressed_bullets", []))
    result = _extract(role, strip_guide(body), channel, turns)
    if not isinstance(result, dict):
        raise ValueError("Updater output must be a JSON object")
    for key in ("updates", "pending", "resolved"):
        if result.get(key) is not None and not isinstance(result[key], list):
            raise ValueError(f"Updater {key} must be a list")

    stamp = f"{channel} {time.strftime('%d %b %H:%M')}"
    added: list[tuple[str, str]] = []
    if isinstance(result.get("summary"), str) and result["summary"].strip():
        added.append((CONVERSATIONS, f"- {time.strftime('%d %b %Y %H:%M')}, {channel}: {result['summary'].strip()}"))
    suppressed = list(meta.get("suppressed_bullets", []))
    removed = set()
    for item in (result.get("updates") or [])[:MAX_ITEMS]:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            continue
        section = item.get("section", "")
        if not isinstance(section, str) or section not in SECTIONS[role]:
            section = CONVERSATIONS
        # Accept only exact statements in the named section, never arbitrary deletion commands.
        current = _sections_of(body).get(section, [])
        for previous in item.get("replaces", []) if isinstance(item.get("replaces", []), list) else []:
            if isinstance(previous, str) and previous in current:
                removed.add(previous)
        added.append((section, f"- {item['text'].strip()} [{stamp}]"))
    pending = _sections_of(body).get(PENDING, [])
    for previous in result.get("resolved", []) if isinstance(result.get("resolved", []), list) else []:
        if isinstance(previous, str) and previous in pending:
            removed.add(previous)
    preference = result.get("contact_preference")
    if preference in ("no_calls", "calls_permitted"):
        for bullets in _sections_of(body).values():
            removed.update(b for b in bullets if "Contact preference:" in b)
        text = "no further calls" if preference == "no_calls" else "calls permitted"
        added.append((PENDING, f"- Contact preference: {text}. [{stamp}]"))
    if preference == "no_calls":
        removed.update(b for b in pending if re.search(r"\bcall(?:back)?\b|phone|meeting", b, re.I))
    for text in (result.get("pending") or [])[:MAX_ITEMS]:
        if isinstance(text, str) and text.strip():
            if preference == "no_calls" and re.search(r"\bcall(?:back)?\b|phone|meeting", text, re.I):
                continue
            added.append((PENDING, f"- {text.strip()} [from {stamp}]"))

    if not added and not removed:
        emit("update", "skip", glid, role, reason="nothing new")
        return

    body = "\n".join(l for l in body.splitlines() if l.strip() not in removed)
    for section, bullets in state.items():
        state[section] = [b for b in bullets if b not in removed]
    suppressed = list(dict.fromkeys(suppressed + list(removed)))
    for section in dict.fromkeys(s for s, _ in added):
        body = insert_bullets(body, section, [b for s, b in added if s == section])
        state[section] = list(dict.fromkeys(state.get(section, []) + [b for s, b in added if s == section]))
    reconciled = reconcile_support(body)
    settled = set(body.splitlines()) - set(reconciled.splitlines())
    for section, bullets in state.items():
        state[section] = [b for b in bullets if b not in settled]
    # Keep ticket outcomes as durable facts: refreshed API wording is reconciled by ID.
    body = reconciled
    # Only recent conversation summaries are needed for current state; history retains all versions.
    summaries = [b for b in state.get(CONVERSATIONS, []) if SUMMARY_BULLET.match(b)]
    keep = int(load_config()["context_files"].get("compact", {}).get("conversation_summaries", 2))
    recent = summaries[-keep:] if keep else []
    state[CONVERSATIONS] = [b for b in state.get(CONVERSATIONS, []) if not SUMMARY_BULLET.match(b) or b in recent]
    body = compact_body(body, role, load_config()["context_files"].get("compact", {}))
    # New facts change what the next call should open with: rewrite the guide too.
    body = add_guide(glid, role, body)

    path = CONTEXTS_DIR / glid / f"{role}.md"
    # Keep the pre-update file (even a sample or blank skeleton) so the console can show the diff.
    version = save_history(path, dict(meta), old_body) + 1
    meta.update(
        context_version=version,
        mode="conversation_updated",
        conversation_updates=int(meta.get("conversation_updates") or 0) + 1,
        last_update={"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "channel": channel, "added": len(added)},
        learned_state=state, suppressed_bullets=suppressed, compact_version=COMPACT_VERSION,
    )
    if based_on:
        meta["based_on"] = based_on
    write_view_file(path, meta, body)

    ms = round((time.perf_counter() - t0) * 1000)
    emit(
        "update", "done", glid, role,
        channel=channel, version=version, path=str(path.relative_to(ROOT)), ms=ms,
        added=[{"section": s, "text": b[2:]} for s, b in added],
    )
    logger.info(f"Updated {path} to v{version} with {len(added)} items in {ms} ms")
