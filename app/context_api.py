"""Client for the IndiaMART Global Context API.

Two calls: generateContextUID (GLID -> mapping_id for the requested segments),
then getContext (mapping_id -> data). Credentials come from .env, never code.

CLI, to save a redacted fixture and see the response shape:
    uv run context_api.py 271504729
    uv run context_api.py 271504729 --segments kycdetails,activitydetails --raw
"""

import argparse
import json
import os
import re
import sys
import time
import tempfile
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "fixtures"


def context_mode() -> str:
    mode = os.getenv("GC_MODE", "live").strip().lower()
    if mode not in ("live", "demo"):
        raise ContextAPIError("GC_MODE must be live or demo")
    return mode


def demo_directory() -> Path:
    path = Path(os.getenv("GC_DEMO_DIR", "demo_data"))
    return path if path.is_absolute() else ROOT / path


def load_demo_snapshot(glid: str) -> dict:
    """Read a captured response; never fall back to the internal API."""
    if not re.fullmatch(r"\d{1,15}", str(glid)):
        raise ContextAPIError("GLID must be digits only")
    path = demo_directory() / f"{glid}.json"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if (str(snapshot.get("glid")) != str(glid)
                or not isinstance(snapshot.get("data"), dict)
                or not isinstance(snapshot.get("datasets", {}), dict)):
            raise ValueError("invalid snapshot envelope")
        return snapshot
    except FileNotFoundError:
        raise ContextAPIError(f"No demo snapshot for GLID {glid}; use an available demo GLID or cold start") from None
    except (ValueError, TypeError, AttributeError) as exc:
        raise ContextAPIError(f"Invalid demo snapshot for GLID {glid}") from exc


def available_demo_glids() -> list[str]:
    return sorted(p.stem for p in demo_directory().glob("*.json")
                  if re.fullmatch(r"\d{1,15}", p.stem))

ALL_SEGMENTS = [
    "kycdetails",
    "activitydetails",
    "connectdetails",
    "ticketsdetails",
    "BLdetails",
    "ActualData",
    "duplicate_glids_details",
]
MAPPING_TYPE_GLID = 1


class ContextAPIError(RuntimeError):
    pass


def _config() -> dict:
    missing = [k for k in ("GC_EMPID", "GC_AK") if not os.getenv(k)]
    if missing:
        raise ContextAPIError(f"Set {', '.join(missing)} in app/.env")
    return {
        "base_url": os.getenv("GC_BASE_URL", "http://merp.intermesh.net/go/api/globalcontext/v1"),
        "empid": int(os.environ["GC_EMPID"]),
        "ak": os.environ["GC_AK"],
        "source_id": int(os.getenv("GC_SOURCE_ID", "3")),
        "forwarded_for": os.getenv("GC_FORWARDED_FOR", ""),
        "timeout": float(os.getenv("GC_TIMEOUT_SECONDS", "15")),
    }


def _headers(cfg: dict) -> dict:
    headers = {"content-type": "application/json"}
    if cfg["forwarded_for"]:
        headers["X-Forwarded-For"] = cfg["forwarded_for"]
    return headers


def _unwrap(response: httpx.Response, step: str) -> dict:
    if response.status_code != 200:
        # Body may echo request details; show only the status and a short prefix.
        raise ContextAPIError(f"{step}: HTTP {response.status_code}: {response.text[:200]}")
    body = response.json()
    if body.get("code") != 200:
        raise ContextAPIError(f"{step}: code {body.get('code')}: {body.get('message')}")
    return body.get("data") or {}


def generate_mapping_id(glid: str, segments: list[str], client: httpx.Client, cfg: dict) -> str:
    payload = {
        "mapping_type": MAPPING_TYPE_GLID,
        "mapping_value": str(glid),
        "source_id": cfg["source_id"],
        "empid": cfg["empid"],
        "ak": cfg["ak"],
        "data_segments": ",".join(segments),
    }
    response = client.post(f"{cfg['base_url']}/generateContextUID", json=payload, headers=_headers(cfg))
    mapping_id = _unwrap(response, "generateContextUID").get("mapping_id")
    if not mapping_id:
        raise ContextAPIError("generateContextUID: no mapping_id in response")
    return mapping_id


def get_context(mapping_id: str, client: httpx.Client, cfg: dict) -> dict:
    response = client.get(
        f"{cfg['base_url']}/x/getContext", params={"mapping_id": mapping_id}, headers=_headers(cfg)
    )
    return _unwrap(response, "getContext")


def fetch_context(glid: str, segments: list[str] | None = None) -> tuple[dict, dict]:
    """Returns (data, timings_ms); demo reads JSON without credentials or HTTP."""
    if context_mode() == "demo":
        t0 = time.perf_counter()
        snapshot = load_demo_snapshot(glid)
        data = snapshot["data"]
        if segments:
            data = {k: v for k, v in data.items() if k in segments or k == "glid"}
        return data, {"mapping_ms": 0, "context_ms": round((time.perf_counter() - t0) * 1000),
                      "source_mode": "demo", "snapshot_fetched_at": snapshot.get("fetched_at"),
                      "snapshot_origin": snapshot.get("origin", "api")}
    cfg = _config()
    segments = segments or ALL_SEGMENTS
    with httpx.Client(timeout=cfg["timeout"]) as client:
        t0 = time.perf_counter()
        mapping_id = generate_mapping_id(glid, segments, client, cfg)
        t1 = time.perf_counter()
        data = get_context(mapping_id, client, cfg)
        t2 = time.perf_counter()
    timings = {"mapping_ms": round((t1 - t0) * 1000), "context_ms": round((t2 - t1) * 1000)}
    save_response(glid, segments, data, timings)
    return data, timings


def save_response(glid: str, segments: list[str], data: dict, timings: dict) -> Path:
    """Save complete response data before rendering/pruning; hide contact details and credentials."""
    if not re.fullmatch(r'\d{1,15}', str(glid)):
        raise ValueError('GLID must be digits only')
    out_dir = FIXTURES_DIR / str(glid)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / 'userctx.redacted.json'
    fixture = {'glid': str(glid), 'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
               'segments': ','.join(segments), 'timings_ms': timings, 'data': redact(data)}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=out_dir,
                                         prefix='userctx.', suffix='.json.tmp', delete=False) as f:
            temporary = Path(f.name)
            json.dump(fixture, f, ensure_ascii=False, indent=2)
            f.write('\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


# --- Redaction for fixtures we keep on disk -----------------------------------

SENSITIVE_KEY = re.compile(r"mobile|phone|email|pan_?no|aadhaa?r|password|token|\bak\b|otp", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
URL_TOKEN = re.compile(r"([?&](?:token|key|sig|signature|ak|auth)[^=]*=)[^&\s\"]+", re.IGNORECASE)


def redact(value, key: str = ""):
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, str):
        if SENSITIVE_KEY.search(key) and value.strip():
            return "[REDACTED]"
        value = URL_TOKEN.sub(r"\1[REDACTED]", value)
        value = EMAIL.sub("[EMAIL]", value)
        return PHONE.sub("[PHONE]", value)
    if isinstance(value, int) and SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    return value


def _shape(data, depth: int = 0, max_depth: int = 2) -> list[str]:
    lines = []
    if isinstance(data, dict):
        for k, v in data.items():
            kind = f"list[{len(v)}]" if isinstance(v, list) else type(v).__name__
            lines.append(f"{'  ' * depth}{k}: {kind}")
            if depth < max_depth and isinstance(v, dict):
                lines += _shape(v, depth + 1, max_depth)
    return lines


def main():
    parser = argparse.ArgumentParser(description="Fetch a GLID's context and save a redacted fixture")
    parser.add_argument("glid")
    parser.add_argument("--segments", default=",".join(ALL_SEGMENTS))
    parser.add_argument("--raw", action="store_true", help="also save the unredacted response (gitignored)")
    args = parser.parse_args()

    try:
        data, timings = fetch_context(args.glid, args.segments.split(","))
    except (ContextAPIError, httpx.HTTPError) as e:
        sys.exit(f"Failed: {e}")

    out_dir = FIXTURES_DIR / args.glid
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.raw:
        raw_dir = FIXTURES_DIR / "raw"
        raw_dir.mkdir(exist_ok=True)
        (raw_dir / f"{args.glid}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"mapping {timings['mapping_ms']} ms, getContext {timings['context_ms']} ms")
    print(f"Saved {out_dir / 'userctx.redacted.json'}")
    print("Response shape:")
    print("\n".join(_shape(data)))


if __name__ == "__main__":
    main()
