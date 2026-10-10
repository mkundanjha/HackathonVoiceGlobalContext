"""Shared by the voice bot and the chat app: load a GLID's context view,
recent cross-channel memory, and build the system prompt.

The view comes from view_builder.get_view(): samples/ first, then a cached
generated file in contexts/, otherwise it is generated (or None for cold start).
Prompt text, persona and memory limits come from agent_config.yaml.
"""

import json
import re
import time
from pathlib import Path

from config import bot_profile, load_config
from context_updater import update_in_background, updating_marker
from events import emit
from support_state import open_ticket_evidence
from view_builder import get_view

ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = ROOT / "memory"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def load_view(glid: str, role: str) -> str | None:
    """Context view without metadata front matter, or None for cold start."""
    return get_view(glid, role)


def load_memory(glid: str, role: str, lookback: int) -> list[dict]:
    path = MEMORY_DIR / f"{glid}.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in records if r["role"] == role][-lookback:]


def save_conversation(glid: str, role: str, channel: str, turns: list[dict]) -> None:
    """Append a finished conversation; turns are {"speaker": "user"|"bot", "text": ...}."""
    turns = [t for t in turns if t["text"].strip()]
    if not turns:
        return
    MEMORY_DIR.mkdir(exist_ok=True)
    record = {"glid": glid, "role": role, "channel": channel, "ended_at": int(time.time()), "turns": turns}
    with open(MEMORY_DIR / f"{glid}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    emit("memory", "saved", glid, role, channel=channel, turns=len(turns))
    # Then fold what was learned into buyer.md / seller.md (runs in the background).
    update_in_background(glid, role, channel, turns)


def _format_memory(records: list[dict], max_chars: int) -> str:
    blocks = []
    for r in records:
        when = time.strftime("%d %b %Y %H:%M", time.localtime(r["ended_at"]))
        lines = [f"{'User' if t['speaker'] == 'user' else 'Bot'}: {t['text']}" for t in r["turns"]]
        blocks.append(f"[{r['channel']} conversation, ended {when}]\n" + "\n".join(lines))
    text = "\n\n".join(blocks)
    # Keep the most recent part if over budget.
    return text[-max_chars:]


def build_system_prompt(glid: str, role: str, channel: str) -> str:
    from demo_history import user_status

    if user_status(glid, role)['pending']:
        raise RuntimeError('History activity is updating context; start the conversation after it finishes')
    started = time.perf_counter()
    cfg = load_config()
    profile = bot_profile(cfg, role)
    prompts = cfg["prompts"]
    wait_for_update(glid, role)
    view = load_view(glid, role)
    memory = load_memory(glid, role, cfg["memory"]["lookback_conversations"])
    tickets = open_ticket_evidence(view or '') if role == 'seller' else []
    ticket_policy = (
        'Actual unresolved ticket details are present in the current support section. '
        'You may open with that known ticket issue; do not invent additional tickets.'
        if tickets else
        'No actual unresolved ticket details are present in this role context. '
        'Do not open with tickets or say that tickets have arrived or are open. '
        'Zero ticket counts, old bot statements and conversation summaries are not ticket evidence. '
        'If the user raises a support concern, respond without assuming an existing ticket.'
    )
    emit(
        "prompt", "done", glid, role,
        channel=channel,
        view_words=len(view.split()) if view else 0,
        memory_conversations=len(memory),
        cold_start=view is None,
    )

    values = {
        **{k: str(v) for k, v in cfg["persona"].items()},
        "purpose": cfg["call_purpose"][role].strip(),
        "profile_name": profile['title'],
        "profile_instructions": profile['instructions'],
        "ticket_policy": ticket_policy,
        "today": time.strftime("%A, %d %b %Y"),
        "role": role,
        "channel": channel,
        "glid": glid,
        "channel_style": prompts[f"{channel}_style"].strip(),
        "context": view or prompts["cold_start"].strip(),
        "memory": _format_memory(memory, cfg["memory"]["max_chars"]) if memory else "None recorded.",
    }
    # One pass, so braces inside the context or memory text are never treated as placeholders.
    prompt = PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), prompts["system"]).strip()
    emit("prompt", "ready", glid, role, channel=channel, prompt_chars=len(prompt),
         prompt_words=len(prompt.split()), total_ms=round((time.perf_counter() - started) * 1000))
    return prompt


def wait_for_update(glid: str, role: str, max_wait_s: float = 45) -> None:
    """If the last call/chat is still being written into the file (possibly by another process),
    wait for it, so a quick follow-up call does not start from the old file."""
    marker = updating_marker(glid, role)
    if not marker.exists():
        return
    emit("prompt", "waiting", glid, role, reason="previous conversation still updating the file")
    deadline = time.time() + max_wait_s
    while marker.exists() and time.time() < deadline:
        time.sleep(0.3)


def load_prompt(name: str) -> str:
    """A prompt from agent_config.yaml (e.g. voice_opening), read fresh each time."""
    return load_config()["prompts"][name].strip()
