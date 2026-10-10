"""Chat channel: minimal web chat over the same context and memory as the voice bot.

Run: uvicorn chat_app:app --port 8000   then open http://localhost:8000
Starting a chat for a GLID with no file generates one first (can take ~30-60 s).
"""

import os
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sarvamai import SarvamAI

from config import load_config
from context import load_prompt
from events import emit
from demo_history import user_status
from role_conversation import RoleConversation

load_dotenv(override=True)
client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])

app = FastAPI()
sessions: dict[str, dict] = {}


def _complete(messages: list[dict]) -> str:
    response = client.chat.completions(model=load_config()["llm"]["model"], messages=messages)
    return response.choices[0].message.content.strip()


class StartRequest(BaseModel):
    glid: str
    role: str


class MessageRequest(BaseModel):
    session_id: str
    text: str


class EndRequest(BaseModel):
    session_id: str


@app.post("/start")
def start(req: StartRequest):
    if req.role not in ("buyer", "seller"):
        raise HTTPException(400, "role must be buyer or seller")
    if user_status(req.glid, req.role)['pending']:
        raise HTTPException(409, 'History activity is updating context; try again when the update finishes')
    emit("chat", "start", req.glid, req.role)
    conversation = RoleConversation(req.glid, req.role, 'chat')
    messages = [
        {"role": "system", "content": conversation.system_prompt()},
        {"role": "user", "content": load_prompt("chat_opening")},
    ]
    opening = _complete(messages)
    messages.append({"role": "assistant", "content": opening})
    conversation.on_bot(opening)
    session_id = uuid.uuid4().hex
    sessions[session_id] = {"glid": req.glid, "role": req.role, "messages": messages,
                            "conversation": conversation}
    emit("chat", "connected", req.glid, req.role)
    emit("turn", "bot", req.glid, req.role, channel="chat", text=opening)
    return {"session_id": session_id, "reply": opening}


@app.post("/message")
def message(req: MessageRequest):
    session = sessions.get(req.session_id)
    if not session:
        raise HTTPException(404, "unknown session")
    glid, role = session["glid"], session["role"]
    conversation = session['conversation']
    try:
        session['messages'][0]['content'] = conversation.before_user(req.text)
    except Exception as exc:
        raise HTTPException(503, 'Unable to prepare the conversation role; please retry') from exc
    session["messages"].append({"role": "user", "content": req.text})
    emit("turn", "user", glid, role, channel="chat", text=req.text)
    reply = _complete(session["messages"])
    session["messages"].append({"role": "assistant", "content": reply})
    conversation.on_bot(reply)
    emit("turn", "bot", glid, role, channel="chat", text=reply)
    return {"reply": reply, "active_role": conversation.active, "origin_role": conversation.origin}


@app.post("/end")
def end(req: EndRequest):
    session = sessions.pop(req.session_id, None)
    if not session:
        raise HTTPException(404, "unknown session")
    # Skip the system prompt and the synthetic "chat opened" trigger.
    turns = [
        {"speaker": "user" if m["role"] == "user" else "bot", "text": m["content"]}
        for m in session["messages"][2:]
    ]
    session['conversation'].flush()
    emit("chat", "ended", session["glid"], session["role"])
    return {"saved_turns": len(turns)}


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>IndiaMART chat</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: system-ui, sans-serif; max-width: 640px; margin: 24px auto; padding: 0 16px; }
  #log { border: 1px solid #ccc; border-radius: 8px; height: 60vh; overflow-y: auto; padding: 12px; }
  .bot, .user { margin: 8px 0; padding: 8px 12px; border-radius: 12px; max-width: 80%; }
  .bot { background: #eef; } .user { background: #dfd; margin-left: auto; }
  form { display: flex; gap: 8px; margin-top: 8px; } input[name=text] { flex: 1; }
</style></head>
<body>
<div>
  GLID <input id="glid" value="271504729" size="12">
  <select id="role"><option>seller</option><option>buyer</option></select>
  <button id="startBtn">Start chat</button> <button id="endBtn" disabled>End chat</button>
</div>
<div id="log"></div>
<form id="f"><input name="text" autocomplete="off" disabled><button disabled>Send</button></form>
<script>
let sid = null;
const log = document.getElementById('log'), form = document.getElementById('f');
const add = (who, text) => { const d = document.createElement('div'); d.className = who; d.textContent = text; log.appendChild(d); log.scrollTop = log.scrollHeight; };
const post = (path, body) => fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(r => r.json());
const enable = on => { form.text.disabled = form.querySelector('button').disabled = !on; document.getElementById('endBtn').disabled = !on; };
document.getElementById('startBtn').onclick = async () => {
  log.innerHTML = ''; add('bot', '…');
  const r = await post('/start', {glid: document.getElementById('glid').value, role: document.getElementById('role').value});
  log.innerHTML = ''; sid = r.session_id; add('bot', r.reply); enable(true); form.text.focus();
};
document.getElementById('endBtn').onclick = async () => {
  const r = await post('/end', {session_id: sid}); add('bot', `(chat saved to memory: ${r.saved_turns} turns)`); sid = null; enable(false);
};
form.onsubmit = async e => {
  e.preventDefault(); const text = form.text.value.trim(); if (!text) return;
  form.text.value = ''; add('user', text);
  const r = await post('/message', {session_id: sid, text}); add('bot', r.reply);
};
</script>
</body></html>
"""
