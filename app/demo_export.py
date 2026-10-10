"""Capture API responses and selected per-role history for an offline demo.

Run from the repository root: app/.venv/bin/python app/demo_export.py
Use --from-fixtures only when deliberately exporting existing local captures.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

from context_api import ALL_SEGMENTS, FIXTURES_DIR, ROOT, fetch_context, redact
from view_builder import collect_dataset_rows

DEFAULT_GLIDS = ["1834784", "624518", "7444262", "271504729", "35481469",
                 "95568576", "53203913", "3338522", "7195425", "2126164",
                 "2342705", "2416597", "2498888", "2614325", "2929150"]
PRIVATE = re.compile(r"password|secret|authorization|aadhaa?r|pan_?no|gst(?:in|_number|_no)$|"
                     r"recording|audio_?url|address|pincode|postal|mapping_id", re.I)
GST = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", re.I)
URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def demo_redact(value, key=""):
    """Keep business context, remove contact/tax credentials, addresses and URLs."""
    if PRIVATE.search(key) and not re.search(r"flag|verified|available|count|status", key, re.I):
        return "[REDACTED]" if value not in (None, "") else value
    if isinstance(value, dict):
        return {k: demo_redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [demo_redact(v, key) for v in value]
    value = redact(value, key)
    if isinstance(value, str):
        return GST.sub("[GST]", URL.sub("[URL]", value))
    return value


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--glids", nargs="+", default=DEFAULT_GLIDS)
    parser.add_argument("--output", type=Path, default=ROOT / "demo_data")
    parser.add_argument("--from-fixtures", action="store_true",
                        help="Use saved redacted captures instead of fetching live API data")
    parser.add_argument("--allow-dataset-only", action="store_true",
                        help="With --from-fixtures, export explicitly labelled dataset-only cases when a capture is missing")
    args = parser.parse_args()
    if args.allow_dataset_only and not args.from_fixtures:
        parser.error("--allow-dataset-only requires --from-fixtures")
    if any(not re.fullmatch(r"\d{1,15}", glid) for glid in args.glids):
        parser.error("GLIDs must be 1–15 digits")
    # Export always collects the original sources, even if the server runs in demo mode.
    os.environ["GC_MODE"] = "live"
    results = []
    for glid in dict.fromkeys(args.glids):
        try:
            origin = "api"
            datasets = {role: collect_dataset_rows(glid, role) for role in ("buyer", "seller")}
            if args.from_fixtures:
                path = FIXTURES_DIR / glid / "userctx.redacted.json"
                if path.exists():
                    fixture = json.loads(path.read_text())
                    data, fetched_at = fixture["data"], fixture["fetched_at"]
                elif args.allow_dataset_only and any(datasets.values()):
                    data, fetched_at, origin = {}, None, "datasets"
                else:
                    raise FileNotFoundError("No saved API response")
            else:
                data, _ = fetch_context(glid)
                fetched_at = datetime.now(timezone.utc).isoformat()
            if not isinstance(data, dict) or (not data and origin == "api"):
                raise ValueError("Empty API response")
            payload = {"schema_version": 1, "glid": glid, "origin": origin,
                       "fetched_at": fetched_at, "segments": ALL_SEGMENTS,
                       "data": data, "datasets": datasets}
            write_json(args.output / f"{glid}.json", demo_redact(payload))
            results.append({"glid": glid, "status": "ready", "origin": origin, "fetched_at": fetched_at})
            print(f"{glid}: saved", flush=True)
        except Exception as exc:
            # Exceptions can contain HTTP request bodies/credentials. Do not print them.
            results.append({"glid": glid, "status": "failed", "error_type": type(exc).__name__})
            print(f"{glid}: failed ({type(exc).__name__}); previous snapshot, if any, preserved", flush=True)
    write_json(args.output / "manifest.json", {
        "exported_at": datetime.now(timezone.utc).isoformat(), "records": results,
        "source": "existing fixtures" if args.from_fixtures else "live API",
        "privacy": "Redacted contacts, credentials, tax IDs, addresses and URLs; names and business context remain."})
    return 1 if any(r["status"] == "failed" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
