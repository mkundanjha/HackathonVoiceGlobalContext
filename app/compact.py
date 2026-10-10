"""Bound source detail while keeping durable conversation facts and open commitments."""

import re

from renderer import SECTIONS, SUMMARY_BULLET, LEARNED_BULLET, insert_bullets, _sections_of
from support_state import reconcile_support

COMPACT_VERSION = 3


def is_learned(bullet: str) -> bool:
    return bool(SUMMARY_BULLET.match(bullet) or LEARNED_BULLET.search(bullet)
                or re.search(r"\[(?:from )?(?:voice|chat) \d{1,2} \w{3} [^\]]+\]", bullet))


def learned_state(body: str, meta: dict) -> dict[str, list[str]]:
    """Migrate old dated bullets once; newer files store the complete state in metadata."""
    if "learned_state" in meta:
        return {s: list(bs) for s, bs in meta["learned_state"].items()}
    return {s: [b for b in bs if is_learned(b)] for s, bs in _sections_of(body).items()
            if s != "Conversation Guide" and any(is_learned(b) for b in bs)}


def merge_learned(body: str, state: dict[str, list[str]], suppressed: list[str]) -> str:
    lines = body.splitlines()
    # Exact source statements superseded by conversations must not reappear on refresh.
    body = "\n".join(l for l in lines if l.strip() not in suppressed)
    existing = set(body.splitlines())
    for section, bullets in state.items():
        additions = [b for b in bullets if b not in existing]
        if additions:
            body = insert_bullets(body, section, additions)
    return reconcile_support(body)


def compact_body(body: str, role: str, limits: dict) -> str:
    """Select complete bullets, never cut quantities or dates mid-sentence.

    The word budget is soft: current learned facts and unresolved actions take priority.
    The full learned state and previous Markdown remain outside the prompt in metadata/history.
    """
    sections = _sections_of(reconcile_support(body))
    maximum = int(limits.get("max_words", 450))
    per_section = int(limits.get("source_bullets", 2))
    summaries = int(limits.get("conversation_summaries", 2))
    selected, candidates = {}, []
    for section in SECTIONS[role]:
        bullets = list(dict.fromkeys(sections.get(section, [])))
        learned = [b for b in bullets if is_learned(b) and not SUMMARY_BULLET.match(b)]
        recent = [b for b in bullets if SUMMARY_BULLET.match(b)][-summaries:] if summaries else []
        selected[section] = learned + recent
        sources = [b for b in bullets if not is_learned(b) and b != "- Unknown."]
        # Contact restrictions and uncertainty should survive source-detail pruning.
        important = [b for b in sources if '[Demo history:' in b or re.search(
            r"open support ticket|no further calls|not to be called|do not call|conflict|unresolved|unknown|snapshot|not available",
            b, re.I)]
        selected[section] += important
        candidates.extend((section, b) for b in sources if b not in important)
    candidates.sort(key=lambda item: 0 if "Customer name:" in item[1] else 1)
    used = sum(len(b.split()) for bs in selected.values() for b in bs)
    used += sum(len(s.split()) + 2 for s in SECTIONS[role]) + 3
    # Round-robin sections so large profiles do not consume the entire budget.
    for rank in range(per_section):
        for section in SECTIONS[role]:
            options = [b for s, b in candidates if s == section]
            if rank < len(options):
                bullet = options[rank]
                if used + len(bullet.split()) <= maximum:
                    selected[section].append(bullet)
                    used += len(bullet.split())
    title = "# Buyer Context" if role == "buyer" else "# Seller Context"
    return title + "\n\n" + "\n\n".join(
        f"## {s}\n" + ("\n".join(selected[s]) or "- Unknown.") for s in SECTIONS[role])
