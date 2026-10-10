"""Demo console: one page to pick a user, build their context file, call or chat with
them, and watch the flow, the conversation and the context file update live.

The page talks to the voice agent directly over WebRTC (POST <agent>/api/offer), so
one Call button starts the agent and connects the microphone.

Run: uvicorn console:app --port 8000   then open http://localhost:8000
(Do not run voice_agent.py separately; the console starts and stops it.)
"""

import atexit
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Literal
from contextlib import asynccontextmanager
import demo_history

import chat_app
import hosted_voice
from context_api import context_mode, available_demo_glids
from events import emit, read_events
from view_builder import CONTEXTS_DIR, SAMPLES_DIR, get_view, read_view_file

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
MEMORY_DIR = ROOT / "memory"
VOICE_LOG = MEMORY_DIR / "voice_agent.log"
VOICE_PORT = 7860
GLID_PATTERN = re.compile(r"^\d{1,15}$")

@asynccontextmanager
async def lifespan(app):
    demo_history.start_worker()
    yield
    demo_history.stop_worker()


app = FastAPI(title="Global Context Console", lifespan=lifespan)


def voice_enabled() -> bool:
    return os.getenv("ENABLE_VOICE", "true").lower() not in ("false", "0", "no")


def voice_transport() -> str:
    transport = os.getenv("VOICE_TRANSPORT", "webrtc").lower()
    if transport not in ("webrtc", "websocket"):
        raise ValueError("VOICE_TRANSPORT must be webrtc or websocket")
    return transport


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/api/runtime")
def runtime_info():
    mode = context_mode()
    return {"context_mode": mode, "demo_glids": available_demo_glids() if mode == "demo" else [],
            "voice_enabled": voice_enabled(), "voice_transport": voice_transport()}


@app.websocket("/api/voice/ws")
async def hosted_voice_call(websocket: WebSocket):
    if not voice_enabled() or voice_transport() != "websocket":
        await websocket.close(code=1008)
        return
    await hosted_voice.serve(websocket)


@app.get("/static/hosted_voice.js")
def hosted_voice_script():
    return FileResponse(APP_DIR / "static" / "hosted_voice.js", media_type="application/javascript")

# Chat endpoints are the same functions the plain chat app uses.
app.post("/api/chat/start")(chat_app.start)
app.post("/api/chat/message")(chat_app.message)
app.post("/api/chat/end")(chat_app.end)


class UserRequest(BaseModel):
    glid: str
    role: str
    force: bool = False


class DemoHistoryRequest(BaseModel):
    event_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    glid: str = Field(pattern=r'^\d{1,15}$')
    role: Literal['buyer', 'seller']
    activity_type: Literal['enquiry.created', 'lead.available', 'ticket.opened', 'ticket.closed']
    product: str | None = Field(default=None, min_length=1, max_length=100)
    quantity: float | None = Field(default=None, gt=0, le=1_000_000_000, allow_inf_nan=False)
    unit: str | None = Field(default=None, min_length=1, max_length=20)
    city: str | None = Field(default=None, min_length=1, max_length=60)
    ticket_id: str | None = Field(default=None, pattern=r'^\d{1,20}$')
    issue: str | None = Field(default=None, min_length=1, max_length=200)


@app.post('/api/demo/history', status_code=202)
def demo_history_save(req: DemoHistoryRequest):
    if req.role == 'buyer' and req.activity_type != 'enquiry.created':
        raise HTTPException(400, 'Lead and ticket events apply to sellers only')
    payload = req.model_dump()
    if req.activity_type.startswith('ticket.'):
        if not req.ticket_id:
            raise HTTPException(400, 'Ticket events require a ticket ID')
        if req.activity_type == 'ticket.opened':
            issue = ' '.join((req.issue or '').split())
            if not issue or any(char in issue for char in '[];<>'):
                raise HTTPException(400, 'Opening a ticket requires a plain-text issue without brackets or markup')
        else:
            issue = None
        payload = {k: payload[k] for k in ('event_id', 'glid', 'role', 'activity_type', 'ticket_id')}
        if issue:
            payload['issue'] = issue
        try:
            return demo_history.submit(payload)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
    payload.pop('ticket_id')
    payload.pop('issue')
    if req.activity_type == 'enquiry.created' and (req.quantity is None or req.unit is None):
        raise HTTPException(400, 'Enquiries require quantity and unit')
    for field in ('product', 'city') + (('unit',) if req.activity_type == 'enquiry.created' else ()):
        payload[field] = ' '.join((payload[field] or '').split())
        if not payload[field] or any(char in payload[field] for char in '[];<>'):
            raise HTTPException(400, f'{field} must be plain text without brackets or markup')
    if req.activity_type == 'enquiry.created':
        payload['quantity'] = format(req.quantity, '.15g')
    else:
        payload.pop('quantity')
        payload.pop('unit')
    try:
        return demo_history.submit(payload)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.get('/api/demo/history/status')
def demo_history_status(glid: str, role: str):
    _check(glid, role)
    return demo_history.user_status(glid, role)


@app.get('/api/demo/history/{event_id}')
def demo_history_event(event_id: str):
    record = demo_history.get(event_id)
    if record is None:
        raise HTTPException(404, 'Unknown demo event')
    return record


@app.post('/api/demo/history/{event_id}/retry')
def demo_history_retry(event_id: str):
    try:
        return demo_history.retry(event_id)
    except KeyError:
        raise HTTPException(404, 'Unknown demo event')


def _check(glid: str, role: str) -> None:
    if not GLID_PATTERN.match(glid):
        raise HTTPException(400, "GLID must be digits only")
    if role not in ("buyer", "seller"):
        raise HTTPException(400, "role must be buyer or seller")


# --- Context file ---------------------------------------------------------------

def _view_info(glid: str, role: str) -> dict:
    """The file the bot would read now, in the same order as view_builder.get_view()."""
    generated, sample = CONTEXTS_DIR / glid / f"{role}.md", SAMPLES_DIR / glid / f"{role}.md"
    candidates = []
    if generated.exists():
        source = "updated" if read_view_file(generated)[0].get("conversation_updates") else "generated"
        candidates.append((source, generated))
    candidates += [("sample", sample), ("generated", generated)]
    for source, path in candidates:
        if path.exists():
            meta, _ = read_view_file(path)
            if meta.get('mode') == 'demo_history_updated':
                source = 'history'
            return {
                "source": source,
                "path": str(path.relative_to(ROOT)),
                "content": path.read_text(encoding="utf-8"),
                "version": meta.get("context_version"),
                "last_update": meta.get("last_update"),
                "age_hours": round((time.time() - path.stat().st_mtime) / 3600, 2),
            }
    return {"source": None, "path": None, "content": None, "version": None, "last_update": None, "age_hours": None}


@app.get("/api/context/history")
def context_history(glid: str, role: str):
    """Earlier versions saved before each conversation update, newest first."""
    _check(glid, role)
    history = CONTEXTS_DIR / glid / "history"
    files = sorted(history.glob(f"{role}.v*.md"), key=lambda p: int(p.stem.rsplit(".v", 1)[1]), reverse=True)
    return [{"version": int(p.stem.rsplit(".v", 1)[1]), "content": p.read_text(encoding="utf-8")} for p in files]


@app.get("/api/context")
def context_info(glid: str, role: str):
    _check(glid, role)
    return _view_info(glid, role)


@app.post("/api/context/prepare")
def context_prepare(req: UserRequest):
    _check(req.glid, req.role)
    try:
        get_view(req.glid, req.role, force=req.force)
    except Exception as e:
        raise HTTPException(500, f"Context build failed: {e}")
    return _view_info(req.glid, req.role)


# --- Memory -----------------------------------------------------------------------

@app.get("/api/memory")
def memory(glid: str, role: str):
    _check(glid, role)
    path = MEMORY_DIR / f"{glid}.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in records if r["role"] == role][-10:]


@app.post("/api/memory/reset")
def memory_reset(req: UserRequest):
    _check(req.glid, req.role)
    path = MEMORY_DIR / f"{req.glid}.jsonl"
    if path.exists():
        kept = [line for line in path.read_text(encoding="utf-8").splitlines() if line and json.loads(line)["role"] != req.role]
        path.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    emit("memory", "reset", req.glid, req.role)
    return {"ok": True}


# --- Events -------------------------------------------------------------------------

@app.get("/api/events")
def events(offset: int = -1):
    items, new_offset = read_events(offset)
    return {"events": items, "offset": new_offset}


# --- Voice agent process ----------------------------------------------------------

voice = {"proc": None, "glid": None, "role": None, "started_at": None}


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _stop_voice() -> None:
    proc = voice["proc"]
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    voice.update(proc=None)


atexit.register(_stop_voice)


@app.post("/api/voice/start")
def voice_start(req: UserRequest):
    if not voice_enabled():
        raise HTTPException(503, "Voice is disabled on this deployment; use Start chat for the hosted demo")
    _check(req.glid, req.role)
    if voice_transport() == "websocket":
        return {"ok": True, "transport": "websocket", "websocket_path": "/api/voice/ws"}
    if demo_history.user_status(req.glid, req.role)['pending']:
        raise HTTPException(409, 'History activity is updating context; try again when the update finishes')
    _stop_voice()
    if _port_open(VOICE_PORT):
        raise HTTPException(409, f"Port {VOICE_PORT} is busy. Stop any voice_agent.py you started by hand.")
    MEMORY_DIR.mkdir(exist_ok=True)
    env = {**os.environ, "VOICE_GLID": req.glid, "VOICE_ROLE": req.role}
    with open(VOICE_LOG, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "voice_agent.py"],
            cwd=APP_DIR, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    voice.update(proc=proc, glid=req.glid, role=req.role, started_at=time.time())
    return {"ok": True}


@app.post("/api/voice/stop")
def voice_stop():
    glid, role = voice["glid"], voice["role"]
    _stop_voice()
    emit("voice", "stopped", glid, role)
    return {"ok": True}


@app.get("/api/voice/status")
def voice_status():
    if voice_transport() == "websocket":
        return hosted_voice.status()
    proc = voice["proc"]
    running = bool(proc and proc.poll() is None)
    status = {
        "running": running,
        "ready": running and _port_open(VOICE_PORT),
        "glid": voice["glid"],
        "role": voice["role"],
        "offer_url": f"http://localhost:{VOICE_PORT}/api/offer",
        "exit_code": proc.poll() if proc else None,
    }
    if proc and not running and VOICE_LOG.exists():
        # Crashed or exited: show the end of its log so errors are visible in the UI.
        status["log_tail"] = VOICE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
    return status


@app.get("/")
def page():
    return FileResponse(APP_DIR / "static" / "console.html", headers={"Cache-Control": "no-store"})
