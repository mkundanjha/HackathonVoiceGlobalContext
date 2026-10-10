"""Background pass after a template build: one-line LLM summaries of free-text records
(enquiry threads, WhatsApp sessions, call transcripts, long API text), swapped into the file.

Summaries are cached in contexts/<glid>/summaries.json by a hash of each record's text, so
a record is sent to the LLM once; later builds reuse it and only new or changed records cost
an LLM call. Conversation-learned bullets in the current file are carried over on re-render.
"""

import hashlib
import json
import os
import re
import threading
import time

from loguru import logger
from sarvamai import SarvamAI

from config import load_config
from events import emit
from guide import add_guide
from renderer import free_text_records, render_view
from compact import COMPACT_VERSION, compact_body, learned_state, merge_learned
from view_builder import CONTEXTS_DIR, ROOT, file_lock, read_view_file, save_history, write_view_file

MAX_RECORDS_PER_CALL = 20
MAX_CHARS_PER_RECORD = 1500
MAX_CHARS_PER_CALL = 14000


def _cache_path(glid: str):
    return CONTEXTS_DIR / glid / "summaries.json"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _load_cache(glid: str) -> dict:
    path = _cache_path(glid)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_summaries(glid: str, sources: dict, role: str) -> dict[str, str]:
    """{record key: summary} from the cache (used by the template renderer)."""
    records = free_text_records(sources, role)
    return {k: v["summary"] for k, v in _load_cache(glid).items()
            if v.get("summary") and k in records and v.get("hash") == _hash(records[k])}


def summarize_in_background(glid: str, role: str, sources: dict, revision: int) -> None:
    """Non-daemon thread, so a CLI or voice-agent prewarm finishes the pass before exiting."""
    threading.Thread(target=_run, args=(glid, role, sources, revision)).start()


def _run(glid: str, role: str, sources: dict, revision: int) -> None:
    try:
        _summarize(glid, role, sources, revision)
    except Exception as e:
        logger.exception("Summary pass failed")
        emit("summary", "error", glid, role, error=f"{type(e).__name__}: {e}"[:300])


def _summarize(glid: str, role: str, sources: dict, revision: int) -> None:
    records = free_text_records(sources, role)
    cache = _load_cache(glid)
    todo = {k: t for k, t in records.items() if cache.get(k, {}).get("hash") != _hash(t)}
    if not todo:
        emit("summary", "cached" if records else "skip", glid, role, records=len(records))
        return

    # One batched call, bounded in size; anything left over keeps its snippet until the next build.
    batch, size = {}, 0
    for key, text in todo.items():
        text = text[:MAX_CHARS_PER_RECORD]
        if len(batch) >= MAX_RECORDS_PER_CALL or size + len(text) > MAX_CHARS_PER_CALL:
            break
        batch[key] = text
        size += len(text)

    emit("summary", "start", glid, role, records=len(batch), pending=len(todo) - len(batch))
    t0 = time.perf_counter()
    summaries = _llm_summaries(role, batch)
    # Cache is shared across roles; serialize its read/merge/write independently of role locks.
    with file_lock(glid, "summaries"):
        cache = _load_cache(glid)
        for key in batch:
            if summaries.get(key):
                cache[key] = {"hash": _hash(todo[key]), "summary": summaries[key]}
        cache_path = _cache_path(glid)
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(cache_path)
    llm_ms = round((time.perf_counter() - t0) * 1000)

    cfg = load_config()
    path = CONTEXTS_DIR / glid / f"{role}.md"
    with file_lock(glid, role):
        if not path.exists():
            return
        meta, current = read_view_file(path)
        if meta.get("source_revision") != revision:
            emit("summary", "superseded", glid, role)
            return
        state = learned_state(current, meta)
        body = render_view(glid, role, sources, load_summaries(glid, sources, role), cfg["context_files"]["section_limits"])
        body = merge_learned(body, state, meta.get("suppressed_bullets", []))
        body = compact_body(body, role, cfg["context_files"].get("compact", {}))
        body = add_guide(glid, role, body)
        version = save_history(path, meta, current) + 1
        write_view_file(path, {**meta, "learned_state": state, "compact_version": COMPACT_VERSION,
                              "context_version": version, "summaries_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, body)

    emit("summary", "done", glid, role, records=len(summaries), ms=llm_ms, version=version,
         path=str(path.relative_to(ROOT)), words=len(body.split()))


def _llm_summaries(role: str, batch: dict[str, str]) -> dict[str, str]:
    cfg = load_config()
    llm = cfg["llm"]
    system = cfg["prompts"]["summarize"].replace("{role}", role)
    client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"], timeout=float(llm["summary_timeout_seconds"]))
    response = client.chat.completions(
        model=llm["summary_model"],
        messages=[{"role": "system", "content": system}, {"role": "user", "content": json.dumps(batch, ensure_ascii=False)}],
        max_tokens=60 * len(batch) + 200,
    )
    text = re.sub(r"<think>.*?</think>", "", response.choices[0].message.content or "", flags=re.DOTALL)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON in summary output: {text[:200]!r}")
    result = json.loads(match.group(0))
    # Only keys we asked for, as short single lines.
    return {k: re.sub(r"\s+", " ", str(v)).strip()[:200] for k, v in result.items() if k in batch and str(v).strip()}
