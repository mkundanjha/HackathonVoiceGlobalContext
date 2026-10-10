"""The "## Conversation Guide" at the top of every buyer.md / seller.md: call purpose,
a ready opening line, ranked talking points, what not to re-ask and what to avoid.

Built immediately from current facts by default (context_files.guide_mode: template).
The optional LLM writer replaces it as a whole; failures use the template fallback.
"""

import os
import re
import time

from loguru import logger
from sarvamai import SarvamAI

from config import bot_profile, load_config
from events import emit
from renderer import FOLLOW_UP_BULLET, SUMMARY_BULLET
from support_state import open_ticket_evidence, reconcile_support

GUIDE_HEADING = "## Conversation Guide"
GUIDE_VERSION = 5  # Keep ticket numbers private unless the user asks for them.
FALLBACK_NOTE = "- Note: automatic template guide (guide writer unavailable); rewritten on next lookup."
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def has_guide(body: str) -> bool:
    """A saved template or LLM guide can be reused until context changes."""
    return any(line.strip().lower() == GUIDE_HEADING.lower() for line in body.splitlines())


def strip_guide(body: str) -> str:
    """Body without its guide section (the guide is never input to a new guide)."""
    lines = body.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip().lower() == GUIDE_HEADING.lower()), None)
    if start is None:
        return body
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[:start] + lines[end:]).strip()


def _place(body: str, guide: str) -> str:
    """Put the guide right after the '# Title' line, before the first section."""
    lines = strip_guide(body).splitlines()
    at = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
    return "\n".join(lines[:at] + [guide.strip(), ""] + lines[at:]).strip()


def _section_bullets(body: str, heading: str) -> list[str]:
    lines = body.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip().lower() == f"## {heading}".lower()), None)
    if start is None:
        return []
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return [l.strip() for l in lines[start + 1:end] if l.strip().startswith("- ")]


def continuity(body: str) -> tuple[str | None, list[str]]:
    """(summary of the most recent conversation with us, open follow-ups from conversations)."""
    summaries = [b for b in _section_bullets(body, "Cross-Channel Conversations") if SUMMARY_BULLET.match(b)]
    follow_ups = [b[2:] for b in _section_bullets(body, "Pending Actions and Clarifications") if FOLLOW_UP_BULLET.search(b)]
    return (summaries[-1][2:] if summaries else None), follow_ups[-4:]


def _continue_line(last: str | None, follow_ups: list[str]) -> str:
    if not last:
        return "- Continue from: nothing, no earlier conversation with us."
    open_items = "; ".join(follow_ups) if follow_ups else "none"
    return f"- Continue from: last conversation {last} Open follow-ups: {open_items}"


def _fallback_guide(cfg: dict, role: str, last: str | None, follow_ups: list[str]) -> str:
    """Template guide used when the LLM is unavailable or too slow: never leaves a call without an opening."""
    persona = {k: str(v) for k, v in cfg["persona"].items()}
    opening = bot_profile(cfg, role)['opening_continue' if last else 'opening_new'].strip()
    opening = PLACEHOLDER.sub(lambda m: persona.get(m.group(1), m.group(0)), opening)
    continue_from = f"- Continue from: {last}" if last else "- Continue from: no earlier conversation with us."
    lines = [GUIDE_HEADING, continue_from, f'- Opening: "{opening}"']
    if follow_ups:
        lines.append(f"- Talking point 1: {follow_ups[-1]}")
    lines.append(FALLBACK_NOTE)
    return "\n".join(lines)


def _buyer_guide(cfg: dict, body: str, last: str | None, follow_ups: list[str]) -> str:
    """Buyer call prep without assuming a purchase from an unrelated earlier conversation."""
    profile = _section_bullets(body, 'Profile and KYC')
    name = re.search(r'(?:Customer name:|\*\*Name:\*\*)\s*([^;\[\n]+)', '\n'.join(profile), re.I)
    greeting = f'Namaste {name.group(1).strip()} ji' if name else 'Namaste ji'
    opening = bot_profile(cfg, 'buyer')['opening_continue' if last else 'opening_new']
    opening = PLACEHOLDER.sub(lambda m: str(cfg['persona'].get(m.group(1), m.group(0))), opening)
    opening = opening.replace('Namaste ji', greeting)
    requirements = [b for b in _section_bullets(body, 'Requirements and Enquiries')
                    if b != '- Unknown.' and not re.search(
                        r'not confirm|no (?:active |current )?requirement|cancelled|canceled|fulfilled', b, re.I)]
    # Explicit pending concerns take precedence over unrelated historical products.
    point = follow_ups[0] if follow_ups else None
    if not point:
        concerns = [b for b in _section_bullets(body, 'Pending Actions and Clarifications')
                    if b != '- Unknown.' and 'Contact preference:' not in b]
        point = concerns[0][2:] if concerns else None
    if not point and requirements:
        point = requirements[0][2:]
        product = re.search(
            r'(?:Product|Product name|Requirement submitted|New requirement):\s*([^;\[\n.]+)'
            r'|enquired for (.+?) to ', requirements[0], re.I)
        if product:
            label = (product.group(1) or product.group(2)).strip()
            opening = (f'{greeting}, main {cfg["persona"]["name"]} {cfg["persona"]["company"]} se. '
                       f'Aapki {label} ki requirement ke silsile mein, kya abhi baat kar sakte hain?')
    lines = [GUIDE_HEADING, f'- Continue from: {last or "no earlier conversation with us."}',
             f'- Opening: "{opening}"']
    if point:
        lines.append('- Talking point 1: ' + point)
    lines.extend([
        '- Next action: follow the current buyer need; ask one missing essential at a time. Confirm complete details once.',
        '- Avoid: unrelated past products, repeating known details, treating old quantities or dates as current, and unsupported supplier or support promises.',
    ])
    return '\n'.join(lines)


def add_guide(glid: str, role: str, body: str, template_only: bool = False) -> str:
    """Body with a fresh guide; on LLM failure or timeout, with a template guide instead."""
    emit("guide", "start", glid, role)
    t0 = time.perf_counter()
    cfg = load_config()
    profile = bot_profile(cfg, role)
    body = reconcile_support(body)
    last, follow_ups = continuity(body)
    # Code-built guide is ready immediately; the LLM writer is an optional setting.
    if template_only or cfg["context_files"].get("guide_mode", "template") == "template":
        guide = _fallback_guide(cfg, role, last, follow_ups).replace("\n" + FALLBACK_NOTE, "")
        name = re.search(r"Customer name: ([^;\[\n]+)", body, re.I)
        if name:
            guide = guide.replace("Namaste ji", f"Namaste {name.group(1).strip()} ji")
        if "Contact preference: calls permitted" not in body and re.search(
                r"no further calls|requested no.*calls|asked not to be called|do not call", body, re.I):
            guide = (f"{GUIDE_HEADING}\n{_continue_line(last, [])}\n"
                     "- Next action: no outbound calls; respect the recorded no-call request.\n"
                     f'- Opening, inbound only: "Namaste ji, main {cfg["persona"]["name"]} '
                     f'{cfg["persona"]["company"]} se. Aaj main aapki kya madad kar sakti hoon?"\n'
                     "- Avoid: callback, leads or meeting proposals unless the user requests them.")
        else:
            demo = next((line for line in body.splitlines() if '[Demo history:' in line
                         and re.search(r'New enquiry:|New available lead:|Requirement submitted:', line)), None)
            support_section = 'Support and Service State' if role == 'seller' else 'Pending Actions and Clarifications'
            support = _section_bullets(body, support_section)
            ticket = next(iter(open_ticket_evidence(body)), None) if role == 'seller' else None
            unresolved = next((line for line in support if re.search(
                r'no resolution|not receiving any resolution|ticket.*unresolved', line, re.I)), None) if role == 'seller' else None
            if ticket or unresolved:
                greeting = f'Namaste {name.group(1).strip()} ji' if name else 'Namaste ji'
                reason = ('main dekh sakti hoon ki aapke kuch tickets aaye hue hain, usi ke silsile mein'
                          if ticket else 'aapki pending support concern ke silsile mein')
                opening = f'{greeting}, main {cfg["persona"]["name"]} {cfg["persona"]["company"]} se. {reason}, kya abhi baat kar sakte hain?'
                guide = '\n'.join([GUIDE_HEADING, f'- Continue from: {last or "pending service issue"}',
                                   f'- Opening: "{opening}"',
                                   '- Priority: address the unresolved support issue before leads, catalog or executive meetings.',
                                   '- Talking point 1: ' + (ticket or unresolved)[2:],
                                   '- Next action: acknowledge the issue; clarify help needed only if missing. Say the ticket number only when explicitly asked, using the known ID. Do not read other internal ticket metadata aloud.',
                                   '- Avoid: repeating known ticket questions, sales pitches, or promising resolution without an actual service action.'])
            elif demo:
                product = re.search(r'(?:New enquiry|New available lead|Requirement submitted): ([^;]+)', demo)
                if product:
                    greeting = f'Namaste {name.group(1).strip()} ji' if name else 'Namaste ji'
                    activity = 'nayi lead opportunity' if 'New available lead:' in demo else 'nayi enquiry'
                    reason = (f"aapki {product.group(1)} ki requirement ke silsile mein" if role == 'buyer'
                              else f"{product.group(1)} ke liye {activity} ke silsile mein")
                    opening = f'{greeting}, main {cfg["persona"]["name"]} {cfg["persona"]["company"]} se. {reason}, kya abhi baat kar sakte hain?'
                    guide = re.sub(r'^- Opening:.*$', lambda _: f'- Opening: "{opening}"', guide, flags=re.M)
                    guide += '\n- New activity: ' + demo[2:]
            if not follow_ups and not demo and not (ticket or unresolved):
                section = "Requirements and Enquiries" if role == "buyer" else "Leads and Enquiries"
                points = _section_bullets(body, section)
                if points and points[0] != "- Unknown.":
                    guide += "\n- Talking point: " + points[0][2:]
                    if role == 'buyer':
                        product = re.search(r'(?:Product|Product name|Requirement submitted):\s*([^;\[\n.]+)|enquired for (.+?) to ', points[0], re.I)
                        if product:
                            label = (product.group(1) or product.group(2)).strip()
                            greeting = f'Namaste {name.group(1).strip()} ji' if name else 'Namaste ji'
                            opening = f'{greeting}, main {cfg["persona"]["name"]} {cfg["persona"]["company"]} se. Aapki {label} ki requirement ke silsile mein, kya abhi baat kar sakte hain?'
                            guide = re.sub(r'^- Opening:.*$', lambda _: f'- Opening: "{opening}"', guide, flags=re.M)
        if name:
            guide = guide.replace("Namaste ji", f"Namaste {name.group(1).strip()} ji")
        if role == 'buyer' and '- Next action: no outbound calls;' not in guide:
            guide = _buyer_guide(cfg, body, last, follow_ups)
        emit("guide", "done", glid, role, ms=round((time.perf_counter() - t0) * 1000), mode="template")
        return _place(body, guide)
    try:
        values = {
            **{k: str(v) for k, v in cfg["persona"].items()},
            "role": role,
            "purpose": cfg["call_purpose"][role].strip(),
            "profile_name": profile['title'],
            "profile_instructions": profile['instructions'],
            "today": time.strftime("%A, %d %b %Y"),
        }
        system = PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), cfg["prompts"]["guide"])
        # Put the last conversation first, separately, so it cannot get lost in a long file.
        if last:
            header = (
                f"MOST RECENT CONVERSATION WITH US: {last}\n"
                f"OPEN FOLLOW-UPS FROM OUR CONVERSATIONS: {'; '.join(follow_ups) or 'none'}\n"
                + ("The Opening should continue the relevant buyer concern without assuming a purchase.\n\n"
                 if role == 'buyer' else "The Opening must continue from this.\n\n")
            )
        else:
            header = "MOST RECENT CONVERSATION WITH US: none (this is our first conversation).\n\n"
        client = SarvamAI(
            api_subscription_key=os.environ["SARVAM_API_KEY"],
            timeout=float(cfg["llm"].get("guide_timeout_seconds", 8)),
        )
        response = client.chat.completions(
            model=cfg["llm"]["guide_model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": header + "Context file:\n" + strip_guide(body)[:14000]},
            ],
            max_tokens=1200,
        )
        text = re.sub(r"<think>.*?</think>", "", response.choices[0].message.content or "", flags=re.DOTALL)
        text = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", text.strip())
        if GUIDE_HEADING.lower() not in text.lower():
            raise ValueError(f"no '{GUIDE_HEADING}' in output: {text[:150]!r}")
        lines = text[text.lower().index(GUIDE_HEADING.lower()):].splitlines()
        # Keep only the guide section even if the model wrote more.
        end = next((i for i in range(1, len(lines)) if lines[i].startswith("#")), len(lines))
        # "Continue from" is written by code from the file, not by the model, so it is always exact.
        body_lines = [l for l in lines[1:end] if not l.lower().lstrip("- ").startswith("continue from")]
        guide = "\n".join([lines[0], _continue_line(last, follow_ups)] + body_lines).strip()
        if role == 'seller' and not open_ticket_evidence(body) and re.search(r'ticket', guide, re.I):
            return add_guide(glid, role, body, template_only=True)
    except Exception as e:
        logger.warning(f"Conversation Guide LLM failed for {role} {glid}, using template guide: {e}")
        emit("guide", "fallback", glid, role, error=f"{type(e).__name__}: {e}"[:300],
             ms=round((time.perf_counter() - t0) * 1000))
        fallback = _fallback_guide(cfg, role, last, follow_ups)
        if role == 'buyer':
            # Same safeguards as the immediate template, including no-call requests.
            return add_guide(glid, role, body, template_only=True)
        return _place(body, fallback)
    opening = next((l for l in guide.splitlines() if l.lower().lstrip("- ").startswith("opening")), "")
    emit("guide", "done", glid, role, ms=round((time.perf_counter() - t0) * 1000), opening=opening[:200])
    return _place(body, guide)


def main():
    """Rewrite the guide of an existing contexts/<glid>/<role>.md (previous version kept in history/).

    python guide.py 35388172 seller
    """
    import argparse

    from dotenv import load_dotenv

    from view_builder import CONTEXTS_DIR, file_lock, read_view_file, save_history, write_view_file

    load_dotenv(override=True)
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("glid")
    parser.add_argument("role", choices=["buyer", "seller"])
    args = parser.parse_args()
    path = CONTEXTS_DIR / args.glid / f"{args.role}.md"
    if not path.exists():
        raise SystemExit(f"{path} not found. Prepare the context in the console first.")
    with file_lock(args.glid, args.role):
        meta, body = read_view_file(path)
        guided = add_guide(args.glid, args.role, body)
        version = save_history(path, meta, body) + 1
        write_view_file(path, {**meta, "context_version": version, "guide_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, guided)
    lines = guided.splitlines()
    print("\n".join(lines[lines.index(GUIDE_HEADING):][:4]))


if __name__ == "__main__":
    main()
