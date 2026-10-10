"""Template renderer: writes buyer.md / seller.md from API + dataset records with code, not an LLM.

Structured fields (profile, enquiries, calls, leads, counts) are formatted directly, in
milliseconds and exactly as in the source. Free-text records (enquiry threads, WhatsApp
sessions, call transcripts, long API text) appear as a short quoted snippet until
summarizer.py has written a one-line summary for them; then the summary is used.

Also home of the shared Markdown helpers: SECTIONS, insert_bullets, carry_over_learned.
"""

import re
from datetime import datetime

SECTIONS = {
    "buyer": [
        "Profile and KYC", "Requirements and Enquiries", "Searches and Interests", "Sellers Contacted",
        "Cross-Channel Conversations", "Buyer Engagement", "Pending Actions and Clarifications",
        "Coverage and Uncertainty",
    ],
    "seller": [
        "Business Profile and KYC", "Products and Categories", "Leads and Enquiries",
        "Responses and Call Outcomes", "Cross-Channel Conversations", "Seller Engagement",
        "Support and Service State", "Pending Actions and Clarifications", "Coverage and Uncertainty",
    ],
}

# Bullets written by context_updater after a call/chat (kept across re-renders).
SUMMARY_BULLET = re.compile(r"^- \d{1,2} \w{3} \d{4} \d{2}:\d{2}, (?:voice|chat): ")
FOLLOW_UP_BULLET = re.compile(r"\[from (?:voice|chat) [^\]]+\]\s*$")
LEARNED_BULLET = re.compile(r"\[(?:from )?(?:voice|chat) \d{1,2} \w{3} \d{2}:\d{2}\]\s*$")

# Context API section -> heading, per role.
API_SECTIONS = {
    "seller": {
        "kycdetails": "Business Profile and KYC", "ActualData": "Business Profile and KYC",
        "BLdetails": "Leads and Enquiries", "connectdetails": "Responses and Call Outcomes",
        "activitydetails": "Seller Engagement", "ticketsdetails": "Support and Service State",
        "duplicate_glids_details": "Coverage and Uncertainty",
    },
    "buyer": {
        "kycdetails": "Profile and KYC", "ActualData": "Profile and KYC",
        "BLdetails": "Requirements and Enquiries", "connectdetails": "Sellers Contacted",
        "activitydetails": "Searches and Interests", "ticketsdetails": "Pending Actions and Clarifications",
        "duplicate_glids_details": "Coverage and Uncertainty",
    },
}
SKIP_KEY = re.compile(r"(^|_)(id|ids|url|link|token|hash|upload|uploaded|md5|ak)($|_)|glid|password|otp", re.IGNORECASE)
GOOD_KEY = re.compile(r"name|title|product|status|subject|categor|keyword|mcat|city|desc|remark|type|date|time|qty|quantity|requirement|message|reason|resolution", re.IGNORECASE)
DATE_KEY = re.compile(r"date|time|_on$|_at$", re.IGNORECASE)
LONG_TEXT = 200
API_FIELD = re.compile(
    r"^(customer_name|first_name|last_name|company_name|company_type|customer_type|city|state|"
    r"gst_status|gst_registration_since|gst_turnover|gst_nature_of_business|approved_product_count|"
    r"product_name|category_name|title|description|status|subject|quantity|quantity_unit|"
    r"call_answered_date|call_sts_update|total_ticket_count|enq_last_30_days|enq_last_90_days|"
    r"pns_received_30_days|pns_received_90_days|hl_pns_success_30d)$", re.I)


# --- Small formatting helpers ---------------------------------------------------------

def fmt_date(value: str) -> str:
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except (ValueError, TypeError):
        return value or "date unknown"


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\\'", "'").replace('\\"', '"')).strip()


def _cap(text: str) -> str:
    """Capitalise the first letter only (str.capitalize would turn 'GST' into 'gst')."""
    return text[:1].upper() + text[1:]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _is_boilerplate(message: str, product: str) -> bool:
    """IndiaMART's default 'I am interested in <product>' enquiry text adds nothing."""
    m = _clean(message).lower().rstrip(".")
    return not m or m in (f"i am interested in {_clean(product).lower()}", "i am interested in your product")


def _snippet(text: str, limit: int) -> str:
    text = _clean(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _human(key: str) -> str:
    return _cap(re.sub(r"[_\.]+", " ", key).strip().lower())


def _keyword(value: str) -> str:
    """'Plastic Carry Bag_p1484_mn1_...' -> 'Plastic Carry Bag'."""
    return re.sub(r"_p\d+_.*$", "", value or "").strip()


# --- Free-text records (shared with summarizer.py) --------------------------------------

def free_text_records(sources: dict, role: str) -> dict[str, str]:
    """{record key: normalised text} for everything worth an LLM summary."""
    ds = sources.get("dataset", {})
    records: dict[str, str] = {}

    # Enquiry threads: the enquiry message (if more than boilerplate) plus its replies, in order.
    threads: dict[str, list[str]] = {}
    for e in ds.get("gc_enquiries_received.csv", []):
        if not _is_boilerplate(e.get("message", ""), e.get("product_name", "")):
            threads.setdefault(e["query_id"], []).append(f"Buyer: {e.get('message', '')}")
    for m in sorted(ds.get("gc_enquiry_messages.csv", []), key=lambda m: (m.get("query_id", ""), _num(m.get("sequence"), 0))):
        threads.setdefault(m["query_id"], []).append(f"{m.get('message_from') or 'party'}: {m.get('reply_text', '')}")
    for qid, lines in threads.items():
        text = _clean(" | ".join(lines))
        if len(text) > 25:
            records[f"enquiry:{qid}"] = text

    # WhatsApp chatbot sessions where the user actually wrote something.
    sessions: dict[str, list[dict]] = {}
    for w in ds.get("gc_whatsapp_chatbot_conversations.csv", []):
        sessions.setdefault(w.get("session_id") or w.get("chat_id", ""), []).append(w)
    for sid, msgs in sessions.items():
        if not any(m.get("user_message") for m in msgs):
            continue
        lines = []
        for m in sorted(msgs, key=lambda m: m.get("entry_date", "")):
            if m.get("user_message"):
                lines.append(f"User: {m['user_message']}")
            if m.get("bot_reply"):
                lines.append(f"Bot: {m['bot_reply']}")
        records[f"whatsapp:{sid}"] = _clean(" | ".join(lines))

    # Bot call transcripts without a call summary.
    summarised = {c["attempt_id"] for c in ds.get("gc_bot_calls.csv", []) if c.get("lead_call_summary")}
    turns: dict[str, list[str]] = {}
    for t in ds.get("gc_bot_call_turns.csv", []):
        if t["attempt_id"] not in summarised:
            turns.setdefault(t["attempt_id"], []).append(f"{t.get('speaker', '')}: {t.get('text', '')}")
    for aid, lines in turns.items():
        records[f"transcript:{aid}"] = _clean(" | ".join(lines))

    # Long free text inside API sections.
    for section, data in (sources.get("api") or {}).items():
        for path, value in _walk(data, section):
            if isinstance(value, str) and len(value) >= LONG_TEXT:
                records[f"api:{path}"] = _clean(value)
    return records


def _walk(value, path):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, value


# --- Rendering --------------------------------------------------------------------------

class _Doc:
    def __init__(self, role: str):
        self.sections = {s: [] for s in SECTIONS[role]}

    def add(self, section: str, text: str) -> None:
        self.sections[section].append(f"- {text}")

    def markdown(self, title: str) -> str:
        parts = [title]
        for name, bullets in self.sections.items():
            parts.append(f"## {name}\n" + ("\n".join(bullets) if bullets else "- Unknown."))
        return "\n\n".join(parts)


def render_view(glid: str, role: str, sources: dict, summaries: dict[str, str], limits: dict) -> str:
    """Markdown body (no front matter, no guide) built from the sources."""
    doc = _Doc(role)
    items = int(limits.get("items", 5))
    snip = int(limits.get("snippet_chars", 120))
    texts = free_text_records(sources, role)

    def say(key: str, fallback: str = "") -> str:
        """Summary if we have one, else a short quote of the record."""
        if key in summaries:
            return summaries[key]
        if key in texts:
            return f'"{_snippet(texts[key], snip)}"'
        return _snippet(fallback, snip) if fallback else ""

    ds = sources.get("dataset", {})
    if role == "seller":
        _seller(doc, ds, say, items)
    else:
        _buyer(doc, ds, say, items)
    _api(doc, role, sources.get("api") or {}, say, items)
    _coverage(doc, role, sources)
    return doc.markdown("# Seller Context" if role == "seller" else "# Buyer Context").strip()


def _seller(doc: _Doc, ds: dict, say, items: int) -> None:
    profile = (ds.get("gc_seller_profile.csv") or [{}])[0]
    src = "[Dataset: seller profile]"
    if profile:
        place = ", ".join(x for x in (profile.get("seller_city"), profile.get("seller_state")) if x)
        who = " · ".join(x for x in (
            profile.get("company_name"), place, profile.get("business_type"), profile.get("nature_of_business"),
        ) if x)
        doc.add("Business Profile and KYC", f"{who} {src}")
        facts = []
        if profile.get("annual_turnover") and profile["annual_turnover"] != "NA":
            facts.append(f"turnover {profile['annual_turnover']}")
        if profile.get("gst_registration_year"):
            facts.append(f"GST registered {profile['gst_registration_year']}")
        verified = [n for n, k in (("mobile", "mobile_verified_flag"), ("email", "email_verified_flag"), ("GST", "gst_verified_flag")) if profile.get(k) == "1"]
        if verified:
            facts.append("verified: " + ", ".join(verified))
        if facts:
            doc.add("Business Profile and KYC", _cap("; ".join(facts)) + f" {src}")
        if _num(profile.get("do_not_call_requested"), 0) > 0:
            doc.add("Business Profile and KYC", f"Asked not to be called in the past: check consent before calling {src}")
        if _num(profile.get("already_in_touch_with_executive"), 0) > 0:
            doc.add("Business Profile and KYC", f"Already in touch with an IndiaMART executive {src}")

        cats = [profile.get(f"top_category_{i}") for i in (1, 2, 3)]
        cats = [c for c in cats if c]
        if cats:
            group = f" ({profile['top_category_group']})" if profile.get("top_category_group") else ""
            doc.add("Products and Categories", f"Top categories: {', '.join(cats)}{group} {src}")
        catalog = []
        if profile.get("eng_product_count"):
            catalog.append(f"{int(_num(profile['eng_product_count'], 0))} products listed")
        if profile.get("eng_products_modified_90d"):
            catalog.append(f"{int(_num(profile['eng_products_modified_90d'], 0))} updated in last 90 days")
        if profile.get("eng_cqs"):
            catalog.append(f"catalog quality score {int(_num(profile['eng_cqs'], 0))}")
        if catalog:
            doc.add("Products and Categories", _cap(", ".join(catalog)) + f" {src}")

        eng = []
        for label, k90, k1y in (("enquiries received", "eng_enq_received_90d", "eng_enq_received_1yr"),
                                ("enquiry replies", "eng_enq_replies_90d", "eng_enq_replies_1yr"),
                                ("buyer calls (PNS) received", "eng_pns_received_90d", "eng_pns_received_1yr"),
                                ("buyer calls answered", "eng_pns_answered_90d", "eng_pns_answered_1yr")):
            if profile.get(k90) or profile.get(k1y):
                eng.append(f"{label}: {profile.get(k90, '?')} in 90 days / {profile.get(k1y, '?')} in 1 year")
        for line in eng:
            doc.add("Seller Engagement", f"{_cap(line)} {src}")
        if profile.get("bot_attempts"):
            outcomes = ", ".join(f"{n} {label}" for label, n in (
                ("meeting fixed", profile.get("calls_meeting_fixed")), ("not interested", profile.get("calls_not_interested")),
                ("call later", profile.get("calls_call_later")), ("general", profile.get("calls_general")),
            ) if n and n != "0")
            doc.add("Seller Engagement", f"Bot calls: {profile['bot_attempts']} attempted, {profile.get('bot_answered', '?')} answered"
                    + (f" ({outcomes})" if outcomes else "") + f" {src}")
        if _num(profile.get("showed_frustration"), 0) > 0 or _num(profile.get("asked_if_talking_to_bot"), 0) > 0:
            doc.add("Seller Engagement", f"Earlier bot calls: showed frustration or asked if it was a bot; keep it short and human {src}")

    enquiries = ds.get("gc_enquiries_received.csv", [])
    replied = {m["query_id"] for m in ds.get("gc_enquiry_messages.csv", []) if (m.get("message_from") or "").lower() == "seller"}
    seen = set()
    for e in enquiries:
        if e["query_id"] in seen or len(seen) >= items:
            continue
        seen.add(e["query_id"])
        frm = ", ".join(x for x in (e.get("buyer_company"), e.get("buyer_city")) if x) or "a buyer"
        read = "replied" if e["query_id"] in replied else "read, no reply seen" if e.get("read_status") == "1" else "not read"
        detail = say(f"enquiry:{e['query_id']}")
        doc.add("Leads and Enquiries", f"{fmt_date(e.get('enquiry_date'))}: enquiry for {e.get('product_name') or 'a product'} from {frm}, {read}"
                + (f": {detail}" if detail else "") + f" [Dataset: enquiry {e['query_id']}]")
    if len(enquiries) > len(seen):
        doc.add("Leads and Enquiries", f"{len(enquiries) - len(seen)} more recent enquiries in the data [Dataset: enquiries]")
    unread = [e for e in enquiries if e.get("read_status") != "1" and e["query_id"] not in replied]
    if unread:
        doc.add("Pending Actions and Clarifications", f"{(f'{len(unread)} recent enquiry' if len(unread) == 1 else f'{len(unread)} recent enquiries')} not read or replied to, latest {fmt_date(unread[0].get('enquiry_date'))} for {unread[0].get('product_name') or 'a product'} [Dataset: enquiries]")

    leads = ds.get("gc_buyleads_bought.csv", [])
    if leads:
        credits = sum(int(_num(l.get("credits_used"), 0)) for l in leads)
        products = list(dict.fromkeys(k for k in (_keyword(l.get("keyword", "")) for l in leads) if k))
        doc.add("Leads and Enquiries", f"Bought {_plural(len(leads), 'buy-lead')} recently ({credits} credits), latest {fmt_date(leads[0].get('purchase_date'))}"
                + (f"; products: {', '.join(products[:items])}" if products else "") + " [Dataset: buy-leads]")

    replies = ds.get("gc_enquiry_messages.csv", [])
    if replies:
        threads = {}
        for m in replies:
            threads.setdefault(m["query_id"], m)
        for qid, m in list(threads.items())[:items]:
            if qid in seen:
                continue  # already summarised under Leads and Enquiries
            doc.add("Responses and Call Outcomes", f"{fmt_date(m.get('reply_date'))}: enquiry thread {say(f'enquiry:{qid}', m.get('reply_text', ''))} [Dataset: enquiry {qid}]")

    calls = ds.get("gc_buyer_calls_received.csv", [])
    if calls:
        connected = [c for c in calls if _num(c.get("talk_sec"), 0) > 0]
        last = calls[0]
        doc.add("Responses and Call Outcomes", f"Buyer calls: {len(connected)} of {len(calls)} recent calls connected; latest {fmt_date(last.get('call_datetime'))} {(last.get('call_status') or '').lower()}"
                + (f", talked {int(_num(last.get('talk_sec'), 0))} s" if _num(last.get("talk_sec"), 0) else "") + " [Dataset: buyer calls]")

    for c in ds.get("gc_bot_calls.csv", [])[:3]:
        summary = _snippet(c.get("lead_call_summary", ""), 160) or say(f"transcript:{c['attempt_id']}")
        doc.add("Responses and Call Outcomes", f"{fmt_date(c.get('call_start_time'))}: bot call, {c.get('disposition_label') or 'no outcome'}"
                + (f": {summary}" if summary else "") + f" [Dataset: bot call {c['attempt_id']}]")

    _whatsapp(doc, ds, say, items)
    execs = ds.get("gc_executive_calls.csv", [])
    if execs:
        answered = sum(1 for x in execs if (x.get("status") or "").lower() == "answered")
        doc.add("Cross-Channel Conversations", f"Executive calls: {answered} of {len(execs)} recent calls answered; latest {fmt_date(execs[0].get('call_start_time'))} {execs[0].get('status', '')} [Dataset: executive calls]")
    for x in ds.get("gc_buyer_call_extractions.csv", [])[:2]:
        doc.add("Cross-Channel Conversations", f"{fmt_date(x.get('call_date'))}: call about {x.get('products_discussed') or x.get('categories') or 'products'}"
                + (f", prices {x['prices_quoted']}" if x.get("prices_quoted") else "") + f" [Dataset: call extraction {x['file_id']}]")


def _buyer(doc: _Doc, ds: dict, say, items: int) -> None:
    enquiries = ds.get("gc_enquiries_received.csv", [])
    if enquiries:
        e = enquiries[0]
        who = ", ".join(x for x in (e.get("buyer_company"), e.get("buyer_designation"), e.get("buyer_city"), e.get("buyer_state")) if x)
        if who:
            doc.add("Profile and KYC", f"{who} (as given on enquiries) [Dataset: enquiries]")
    seen = set()
    for e in enquiries:
        if e["query_id"] in seen or len(seen) >= items:
            continue
        seen.add(e["query_id"])
        to = ", ".join(x for x in (e.get("seller_company"), e.get("seller_city")) if x) or "a seller"
        detail = say(f"enquiry:{e['query_id']}")
        doc.add("Requirements and Enquiries", f"{fmt_date(e.get('enquiry_date'))}: enquired for {e.get('product_name') or 'a product'} to {to}"
                + (f": {detail}" if detail else "") + f" [Dataset: enquiry {e['query_id']}]")
    for x in ds.get("gc_buyer_call_extractions.csv", [])[:3]:
        bits = [x.get("products_discussed") or x.get("categories") or "products"]
        if x.get("specs_discussed"):
            bits.append(f"specs {x['specs_discussed']}")
        if x.get("prices_quoted"):
            bits.append(f"prices heard {x['prices_quoted']}")
        doc.add("Requirements and Enquiries", f"{fmt_date(x.get('call_date'))}: discussed on a call: {', '.join(bits)} [Dataset: call extraction {x['file_id']}]")

    keywords = list(dict.fromkeys(e["search_keyword"] for e in enquiries if e.get("search_keyword")))
    products = list(dict.fromkeys(e["product_name"] for e in enquiries if e.get("product_name")))
    if keywords:
        doc.add("Searches and Interests", f"Searched: {', '.join(keywords[:items])} [Dataset: enquiries]")
    if products:
        doc.add("Searches and Interests", f"Interested in: {', '.join(products[:items])} [Dataset: enquiries]")

    sellers = list(dict.fromkeys(e["seller_company"] for e in enquiries if e.get("seller_company")))
    if sellers:
        doc.add("Sellers Contacted", f"By enquiry: {', '.join(sellers[:items])}" + (f" and {len(sellers) - items} more" if len(sellers) > items else "") + " [Dataset: enquiries]")
    calls = ds.get("gc_buyer_calls_received.csv", [])
    if calls:
        connected = [c for c in calls if _num(c.get("talk_sec"), 0) > 0]
        doc.add("Sellers Contacted", f"By phone: {_plural(len(calls), 'recent call')} to {_plural(len({c['seller_glid'] for c in calls}), 'seller')}, {len(connected)} connected; latest {fmt_date(calls[0].get('call_datetime'))} [Dataset: buyer calls]")

    for m in ds.get("gc_enquiry_messages.csv", [])[:items]:
        if m["query_id"] in seen:
            continue
        doc.add("Cross-Channel Conversations", f"{fmt_date(m.get('reply_date'))}: enquiry message {say(f'enquiry:' + m['query_id'], m.get('reply_text', ''))} [Dataset: enquiry {m['query_id']}]")
    _whatsapp(doc, ds, say, items)

    if enquiries or calls:
        dates = sorted(x for x in [e.get("enquiry_date", "") for e in enquiries] + [c.get("call_datetime", "") for c in calls] if x)
        doc.add("Buyer Engagement", f"{len(enquiries)} enquiries and {len(calls)} calls in the recent sample, {fmt_date(dates[0])} to {fmt_date(dates[-1])} [Dataset]")


def _whatsapp(doc: _Doc, ds: dict, say, items: int) -> None:
    rows = ds.get("gc_whatsapp_chatbot_conversations.csv", [])
    if not rows:
        return
    sessions: dict[str, list[dict]] = {}
    for w in rows:
        sessions.setdefault(w.get("session_id") or w.get("chat_id", ""), []).append(w)
    chats = {sid: msgs for sid, msgs in sessions.items() if any(m.get("user_message") for m in msgs)}
    for sid, msgs in list(chats.items())[:3]:
        date = fmt_date(max(m.get("entry_date", "") for m in msgs))
        doc.add("Cross-Channel Conversations", f"{date}: WhatsApp chat {say(f'whatsapp:{sid}')} [Dataset: WhatsApp session]")
    # Sessions where only the bot wrote (templates, nudges): one summary line.
    bot_only = [msgs for sid, msgs in sessions.items() if sid not in chats]
    if bot_only:
        latest = max(m.get("entry_date", "") for msgs in bot_only for m in msgs)
        intents = list(dict.fromkeys(m["intent"] for msgs in bot_only for m in msgs if m.get("intent")))
        doc.add("Cross-Channel Conversations", f"{_plural(len(bot_only), 'WhatsApp session')} with bot messages only, latest {fmt_date(latest)}"
                + (f" ({', '.join(intents[:3])})" if intents else "") + " [Dataset: WhatsApp sessions]")


def _api(doc: _Doc, role: str, api: dict, say, items: int) -> None:
    """Generic rendering of Context API sections (exact field names are not documented)."""
    mapping = API_SECTIONS[role]
    fallback = "Coverage and Uncertainty"
    for section, data in api.items():
        if section not in mapping:
            continue
        if role == "seller" and section == "activitydetails" and isinstance(data, dict):
            # Personal buying/browsing belongs in the buyer view, not a sales call brief.
            data = {k: v for k, v in data.items() if "buyer" not in k.lower() and "last_enquiry" not in k.lower()}
        heading = mapping.get(section, fallback)
        if section == 'ticketsdetails':
            _tickets(doc, heading, data)
            continue
        for line in _api_lines(section, data, say, items):
            doc.add(heading, f"{line} [API: {section}]")


def _tickets(doc: _Doc, heading: str, data) -> None:
    """Normalize nested ticket records; current records take precedence over historical copies."""
    found = {}
    def visit(value, path):
        if isinstance(value, dict):
            if value.get('ticket_id'):
                key = str(value['ticket_id'])
                rank = 3 if 'current' in path else 2 if 'wip' in path.lower() else 1
                if key not in found or rank >= found[key][0]:
                    found[key] = (rank, value, path)
            for key, value in value.items():
                if isinstance(value, (dict, list)):
                    visit(value, path + '.' + key)
        elif isinstance(value, list):
            for item in value:
                visit(item, path)
    visit(data, 'ticketsdetails')
    for _, ticket, path in found.values():
        raw_status = str(ticket.get('ticket_status') or '').strip()
        status = raw_status.lower()
        history = str(ticket.get('ticket_history') or '')
        closed = status in ('closed', 'resolved', 'completed')
        is_open = not closed and (status in ('open', 'wip', 'in progress', 'work in progress')
                                  or (raw_status == 'W' and ('wip' in path.lower()
                                      or re.search(r'Status\s*:\s*WIP', history, re.I))))
        state = 'WIP' if is_open and (raw_status == 'W' or 'wip' in path.lower()) else raw_status or 'unknown'
        prefix = 'Open support ticket' if is_open else 'Support ticket'
        issued = str(ticket.get('ticket_issue_date') or '')
        for fmt in ('%d-%b-%y', '%d-%b-%Y', '%Y-%m-%d'):
            try:
                issued = datetime.strptime(issued, fmt).strftime('%d %b %Y')
                break
            except ValueError:
                pass
        comment = str(ticket.get('ticket_opening_comments') or ticket.get('subject') or '')
        comment = re.sub(r'\s+to employee\s*\([^)]*\)', '', comment, flags=re.I)
        comment = re.sub(r'\s+from Client Mob Number\s*=\s*[^,.]+[,\.]?', '', comment, flags=re.I)
        comment = re.sub(r'\bclick_to_call_id\s*=\s*\d+[,\.]?', '', comment, flags=re.I)
        comment = _clean(comment)
        detail = f"{prefix}: {ticket['ticket_id']}; status {state}; {ticket.get('ticket_type') or 'type unknown'}"
        if issued:
            detail += f'; issued {issued}'
        if comment:
            detail += f'. Opening note: {_snippet(comment, 240)}'
        elif is_open:
            detail += '. Issue details not provided; ask what help is needed.'
        doc.add(heading, detail + f" [API: ticketsdetails; ticket {ticket['ticket_id']}]")
    if not found:
        for line in _api_lines('ticketsdetails', data, lambda key, fallback='': fallback, 2):
            doc.add(heading, line + ' [API: ticketsdetails]')


def _api_lines(section: str, data, say, items: int) -> list[str]:
    lines: list[str] = []
    if isinstance(data, dict):
        scalars = [(k, v) for k, v in data.items() if not isinstance(v, (dict, list))
                   and API_FIELD.match(k) and v != "[REDACTED]"]
        scalars.sort(key=lambda kv: 0 if kv[0].lower() in ("customer_name", "company_name", "city") else 1)
        texts = [f"{_human(k)}: {say(f'api:{section}.{k}', str(v)) if isinstance(v, str) and len(v) >= LONG_TEXT else v}" for k, v in scalars]
        for i in range(0, len(texts), 6):
            lines.append("; ".join(texts[i:i + 6]))
        for k, v in data.items():
            if isinstance(v, (dict, list)) and not SKIP_KEY.search(k):
                lines += [f"{_human(k)}: {line}" for line in _api_lines(f"{section}.{k}", v, say, items)]
    elif isinstance(data, list):
        dicts = [d for d in data if isinstance(d, dict)]
        if dicts:
            date_key = next((k for k in dicts[0] if DATE_KEY.search(k)), None)
            ordered = sorted(range(len(dicts)), key=lambda i: str(dicts[i].get(date_key, "")), reverse=True) if date_key else range(len(dicts))
            lines.append(f"{len(dicts)} records")
            for i in list(ordered)[:3]:
                d = dicts[i]
                fields = [(k, v) for k, v in d.items() if not isinstance(v, (dict, list))
                          and API_FIELD.match(k) and v != "[REDACTED]"]
                fields.sort(key=lambda kv: 0 if GOOD_KEY.search(kv[0]) else 1)
                parts = []
                for k, v in fields[:5]:
                    value = say(f"api:{section}[{i}].{k}", str(v)) if isinstance(v, str) and len(v) >= LONG_TEXT else v
                    parts.append(f"{_human(k)}: {fmt_date(value) if DATE_KEY.search(k) and isinstance(value, str) else value}")
                if parts:
                    lines.append("; ".join(parts))
        elif data:
            lines.append(", ".join(str(x) for x in data[:items]))
    return lines[: items + 1]


def _coverage(doc: _Doc, role: str, sources: dict) -> None:
    snapshot = sources.get("demo_snapshot")
    if snapshot:
        detail = (f"saved API capture from {snapshot.get('fetched_at') or 'unknown date'}"
                  if snapshot.get("origin") == "api" else "dataset-only profile; no API capture")
        doc.add("Coverage and Uncertainty", f"Offline demo: {detail}; source activity is frozen, demo events and conversations can update this file")
    tables = sorted(t.replace("gc_", "").replace(".csv", "").replace("_", " ") for t in sources.get("dataset", {}))
    doc.add("Coverage and Uncertainty", "Datasets used: " + (", ".join(tables) if tables else "none (GLID not in the local 5,000-seller sample)")
            + "; dataset snapshot ends 02 Oct 2026")
    api = sources.get("api")
    doc.add("Coverage and Uncertainty", "Context API sections: " + (", ".join(api) if api else "not available for this build"))
    if role == "buyer":
        doc.add("Coverage and Uncertainty", "Dataset buyer history only covers dealings with the 5,000 sample sellers")


# --- Shared Markdown helpers (used by context_updater and summarizer) --------------------

def insert_bullets(body: str, section: str, bullets: list[str]) -> str:
    """Add bullets at the end of a '## section' block, replacing a lone 'Unknown.' placeholder."""
    lines = body.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip().lower() == f"## {section}".lower()), None)
    if start is None:
        return body.rstrip() + f"\n\n## {section}\n" + "\n".join(bullets) + "\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    block = lines[start + 1:end]
    content = [l for l in block if l.strip()]
    if len(content) == 1 and re.fullmatch(r"-\s*(Unknown|No data available)\.?", content[0].strip(), re.IGNORECASE):
        block = []
    while block and not block[-1].strip():
        block.pop()
    return "\n".join(lines[:start + 1] + block + bullets + [""] + lines[end:])


def _sections_of(body: str) -> dict[str, list[str]]:
    out, current = {}, None
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            out.setdefault(current, [])
        elif current and line.strip().startswith("- "):
            out[current].append(line.strip())
    return out


def carry_over_learned(old_body: str, new_body: str) -> str:
    """Re-insert bullets learned from conversations (old file) that a re-render dropped."""
    new_lines = {l.strip() for l in new_body.splitlines()}
    for section, bullets in _sections_of(old_body).items():
        if section == "Conversation Guide":
            continue
        keep = [b for b in bullets if (SUMMARY_BULLET.match(b) or LEARNED_BULLET.search(b)) and b not in new_lines]
        if keep:
            new_body = insert_bullets(new_body, section, keep)
    return new_body
