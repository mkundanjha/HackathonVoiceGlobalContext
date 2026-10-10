"""Flow events shared by every process (console, chat, voice agent).

Each step appends one JSON line to memory/events.jsonl; the console UI tails the
file to draw the flow and the live conversation.
"""

import json
import time
from pathlib import Path

EVENTS_FILE = Path(__file__).resolve().parent.parent / "memory" / "events.jsonl"


def emit(step: str, status: str, glid: str | None = None, role: str | None = None, **data) -> None:
    """step: flow node (lookup, sample, cache, api, datasets, llm, file, prompt, voice, chat, turn, memory).
    status: start | done | hit | miss | skip | error | ..."""
    event = {"ts": time.time(), "step": step, "status": status, "glid": glid, "role": role, **data}
    try:
        EVENTS_FILE.parent.mkdir(exist_ok=True)
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # events are for display only; never break a call because of them


def read_events(offset: int) -> tuple[list[dict], int]:
    """Events appended after byte offset; offset < 0 means 'start from the end now'."""
    if not EVENTS_FILE.exists():
        return [], 0
    size = EVENTS_FILE.stat().st_size
    if offset < 0 or offset > size:
        return [], size
    with open(EVENTS_FILE, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    # Only consume complete lines; a partial last line is read next time.
    end = chunk.rfind(b"\n") + 1
    events = [json.loads(line) for line in chunk[:end].splitlines() if line.strip()]
    return events, offset + end
