"""Reconcile user-confirmed ticket outcomes with older API snapshots."""

import re

from renderer import _sections_of

SUPPORT = 'Support and Service State'
PENDING = 'Pending Actions and Clarifications'
TICKET_ID = re.compile(r'\bticket\s*[:#]?\s*(\d+)\b', re.I)
UNRESOLVED = re.compile(
    r'\bunresolved\b|no resolution|\b(?:not|never|isn\x27t|wasn\x27t)\b.*\b(?:resolved|solved|fixed|closed)\b'
    r'|not receiving any resolution', re.I)
RESOLVED = re.compile(r'\b(?:resolved|solved|fixed|closed)\b', re.I)


def open_ticket_evidence(body: str) -> list[str]:
    """Actual open ticket details, excluding guides, counts and conversation summaries."""
    support = _sections_of(reconcile_support(body)).get(SUPPORT, [])
    return [bullet for bullet in support
            if re.match(r'- Open support ticket:\s*\S', bullet, re.I)]


def reconcile_support(body: str) -> str:
    """Keep explicit learned outcomes authoritative without pretending to close backend tickets.

    Only ticket-specific conversation facts settle a ticket. A later user-reported
    reopening restores priority. Unknown IDs and unrelated tickets remain untouched.
    """
    sections = _sections_of(body)
    support = sections.get(SUPPORT, [])
    outcomes = {}
    for bullet in support:
        # API status and a request to close a ticket are not confirmation of resolution.
        if not re.search(r'\[(?:from )?(?:voice|chat) ', bullet):
            continue
        match = TICKET_ID.search(bullet)
        if not match or not re.search(r'user (?:confirmed|reported)|seller (?:confirmed|reported)', bullet, re.I):
            continue
        if UNRESOLVED.search(bullet) or re.search(r'\breopened\b|\bopen again\b', bullet, re.I):
            outcomes[match[1]] = False
        elif RESOLVED.search(bullet) and not re.search(r'\brequest|\bwants?\b|\basked\b|to be closed', bullet, re.I):
            outcomes[match[1]] = True
    resolved = {key for key, value in outcomes.items() if value}
    if not resolved:
        return body

    open_ids = {m[1] for b in support if 'Open support ticket:' in b
                for m in [TICKET_ID.search(b)] if m}
    # Legacy unresolved facts omitted the ID. Settle them only with a single known ticket.
    single_settled = len(resolved) == 1 and open_ids <= resolved
    removed = set()
    for section in (SUPPORT, PENDING):
        for bullet in sections.get(section, []):
            match = TICKET_ID.search(bullet)
            settled = bool(match and match[1] in resolved)
            if section == SUPPORT:
                if settled and ('Open support ticket:' in bullet or UNRESOLVED.search(bullet)):
                    removed.add(bullet)
                elif not match and single_settled and 'ticket' in bullet.lower() and UNRESOLVED.search(bullet):
                    removed.add(bullet)
            elif settled and re.search(r'follow.up|escalat|resolve|resolution|close', bullet, re.I):
                removed.add(bullet)
    return '\n'.join(line for line in body.splitlines() if line.strip() not in removed)
