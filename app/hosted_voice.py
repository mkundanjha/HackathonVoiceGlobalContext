"""Lightweight browser voice over the console's public WebSocket.

The browser sends complete WAV utterances; Sarvam REST STT/LLM/TTS handles each
turn. Local streaming Pipecat/WebRTC remains in voice_agent.py.
"""

import asyncio
import base64
import binascii
import io
import os
import re
import wave

from fastapi import WebSocket, WebSocketDisconnect
from sarvamai import SarvamAI

from config import load_config
from context import load_prompt
from demo_history import user_status
from events import emit
from role_conversation import RoleConversation

SAMPLE_RATE = 16000
MAX_SECONDS = 16
MAX_AUDIO_BYTES = SAMPLE_RATE * 2 * MAX_SECONDS + 4096
_active = None


def decode_audio(encoded: str) -> bytes:
    if not isinstance(encoded, str) or len(encoded) > (MAX_AUDIO_BYTES + 2) // 3 * 4:
        raise ValueError("Audio must be a short WAV utterance")
    try:
        audio = base64.b64decode(encoded, validate=True)
        if len(audio) > MAX_AUDIO_BYTES:
            raise ValueError("Audio too large")
        with wave.open(io.BytesIO(audio), "rb") as recording:
            frames = recording.getnframes()
            if (recording.getnchannels() != 1 or recording.getsampwidth() != 2
                    or recording.getframerate() != SAMPLE_RATE
                    or recording.getcomptype() != "NONE"
                    or not SAMPLE_RATE // 10 <= frames <= SAMPLE_RATE * MAX_SECONDS
                    or len(recording.readframes(frames)) != frames * 2):
                raise ValueError("Expected 16 kHz mono PCM WAV, at most 16 seconds")
        return audio
    except (binascii.Error, wave.Error, EOFError) as exc:
        raise ValueError("Invalid WAV audio") from exc


class HostedVoiceSession:
    def __init__(self, glid: str, role: str, client=None):
        self.glid, self.role = glid, role
        self.cfg = load_config()
        self.client = client or SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"],
                                         timeout=30)
        self.stage = "Context preparation"
        self.conversation = RoleConversation(glid, role, "voice")
        self.messages = [{"role": "system", "content": self.conversation.system_prompt()}]

    def _reply(self) -> dict:
        self.stage = "Agent reply"
        response = self.client.chat.completions(model=self.cfg["llm"]["model"], messages=self.messages)
        reply = (response.choices[0].message.content or "").strip()
        if not reply:
            raise ValueError("Empty agent reply")
        self.messages.append({"role": "assistant", "content": reply})
        self.conversation.on_bot(reply)
        emit("turn", "bot", self.glid, self.role, channel="voice", text=reply)
        self.stage = "Speech synthesis"
        voice = self.cfg["voice"]
        speech = self.client.text_to_speech.convert(
            text=reply, target_language_code=voice["tts_language"],
            speaker=voice["tts_voice"], model=voice["tts_model"],
            pace=float(voice["tts_pace"]), speech_sample_rate=24000,
            output_audio_codec="wav",
        )
        if not speech.audios:
            raise ValueError("Empty speech output")
        return {"type": "reply", "text": reply, "audios": speech.audios,
                "active_role": self.conversation.active, "origin_role": self.role}

    def opening(self) -> dict:
        self.messages.append({"role": "user", "content": load_prompt("voice_opening")})
        return self._reply()

    def respond(self, audio: bytes) -> dict:
        self.stage = "Speech recognition"
        voice = self.cfg["voice"]
        response = self.client.speech_to_text.transcribe(
            file=("utterance.wav", audio, "audio/wav"), model=voice["stt_model"],
            language_code=voice["stt_language"], mode=voice.get("stt_mode", "translit"),
        )
        text = response.transcript.strip()
        if not text:
            return {"type": "ready", "message": "No speech detected; please speak again"}
        self.stage = "Role/context preparation"
        self.messages[0]["content"] = self.conversation.before_user(text)
        self.messages.append({"role": "user", "content": text})
        emit("turn", "user", self.glid, self.role, channel="voice", text=text)
        return self._reply()

    def finish(self):
        self.conversation.flush()


def status() -> dict:
    return {"running": _active is not None, "ready": _active is not None,
            "glid": _active["glid"] if _active else None,
            "role": _active["role"] if _active else None,
            "transport": "websocket", "websocket_path": "/api/voice/ws"}


async def serve(websocket: WebSocket):
    """One hosted call at a time, matching the console's local demo behavior."""
    global _active
    await websocket.accept()
    session = None
    owns_call = False
    glid = role = None
    try:
        request = await asyncio.wait_for(websocket.receive_json(), timeout=15)
        glid, role = request.get("glid"), request.get("role")
        if (request.get("type") != "start" or not isinstance(glid, str)
                or not re.fullmatch(r"\d{1,15}", glid) or role not in ("buyer", "seller")):
            raise ValueError("Start requires a numeric GLID and buyer/seller role")
        if _active is not None:
            await websocket.send_json({"type": "error", "message": "Another voice call is active; hang up first"})
            return
        # Reserve before awaiting preparation so simultaneous connections cannot race.
        _active = {"glid": glid, "role": role}
        owns_call = True
        if (await asyncio.to_thread(user_status, glid, role))["pending"]:
            raise ValueError("History activity is updating context; wait for it to finish")
        emit("voice", "start", glid, role, transport="websocket")
        session = await asyncio.to_thread(HostedVoiceSession, glid, role)
        emit("voice", "connected", glid, role, transport="websocket")
        await websocket.send_json(await asyncio.to_thread(session.opening))
        while True:
            request = await websocket.receive_json()
            if request.get("type") == "stop":
                break
            if request.get("type") != "audio":
                raise ValueError("Expected audio or stop message")
            session.stage = "Audio validation"
            audio = decode_audio(request.get("audio"))
            await websocket.send_json(await asyncio.to_thread(session.respond, audio))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        stage = session.stage if session else "Voice setup"
        # Do not send exception bodies: upstream responses may contain credentials.
        code = getattr(exc, "status_code", None)
        reason = f"{stage} failed ({'HTTP ' + str(code) if isinstance(code, int) else type(exc).__name__})"
        if isinstance(exc, ValueError) and session is None:
            reason = str(exc)
        if owns_call:
            emit("voice", "error", glid, role, error=reason)
        try:
            await websocket.send_json({"type": "error", "message": reason})
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        try:
            if session:
                await asyncio.to_thread(session.finish)
        finally:
            if owns_call:
                _active = None
                emit("voice", "ended", glid, role)
            try:
                await websocket.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
