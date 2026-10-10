"""Hosted voice validation, role continuity and WebSocket lifetime without API calls."""
import base64
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import hosted_voice as voice


def recording(rate=16000, channels=1, seconds=0.25):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(bytes(int(rate * seconds) * channels * 2))
    return output.getvalue()


class AudioTests(unittest.TestCase):
    def test_valid_audio_round_trips(self):
        data = recording()
        self.assertEqual(voice.decode_audio(base64.b64encode(data).decode()), data)

    def test_invalid_audio_never_reaches_sarvam(self):
        cases = [None, "bad!", base64.b64encode(b"not audio").decode(),
                 base64.b64encode(recording(8000)).decode(),
                 base64.b64encode(recording(channels=2)).decode(),
                 base64.b64encode(recording(seconds=17)).decode(),
                 base64.b64encode(recording()[:-10]).decode()]
        for data in cases:
            with self.subTest(data=str(data)[:20]), self.assertRaises(ValueError):
                voice.decode_audio(data)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.sdk = Mock()
        self.sdk.chat.completions.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Ji, kitni quantity chahiye?"))])
        self.sdk.text_to_speech.convert.return_value = SimpleNamespace(audios=["wav-audio"])
        self.sdk.speech_to_text.transcribe.return_value = SimpleNamespace(transcript="Mujhe bags khareedne hain")
        self.conversation = Mock(active="buyer")
        self.conversation.system_prompt.return_value = "seller context"
        self.conversation.before_user.return_value = "buyer context"
        p = patch.object(voice, "RoleConversation", return_value=self.conversation)
        self.routing = p.start()
        self.addCleanup(p.stop)
        p = patch.object(voice, "emit")
        self.events = p.start()
        self.addCleanup(p.stop)
        self.session = voice.HostedVoiceSession("123", "seller", self.sdk)

    def test_voice_origin_and_switched_context_reach_llm(self):
        self.routing.assert_called_once_with("123", "seller", "voice")
        response = self.session.respond(recording())
        self.assertEqual(self.session.messages[0]["content"], "buyer context")
        self.conversation.before_user.assert_called_once_with("Mujhe bags khareedne hain")
        self.assertEqual(response["active_role"], "buyer")
        self.assertEqual(response["origin_role"], "seller")
        self.assertEqual(response["audios"], ["wav-audio"])
        self.assertEqual(self.sdk.speech_to_text.transcribe.call_args.kwargs["mode"], "translit")
        self.assertEqual(self.sdk.text_to_speech.convert.call_args.kwargs["output_audio_codec"], "wav")

    def test_empty_transcription_is_not_a_conversation_turn(self):
        self.sdk.speech_to_text.transcribe.return_value.transcript = " "
        self.assertEqual(self.session.respond(recording())["type"], "ready")
        self.sdk.chat.completions.assert_not_called()
        self.conversation.before_user.assert_not_called()

    def test_opening_and_hangup_use_existing_memory_controller(self):
        self.assertEqual(self.session.opening()["type"], "reply")
        self.conversation.on_bot.assert_called_once()
        self.session.finish()
        self.conversation.flush.assert_called_once()


class Socket:
    def __init__(self, requests):
        self.requests = list(requests)
        self.sent = []
        self.closed = False

    async def accept(self):
        pass

    async def receive_json(self):
        if not self.requests:
            raise voice.WebSocketDisconnect()
        return self.requests.pop(0)

    async def send_json(self, value):
        self.sent.append(value)

    async def close(self):
        self.closed = True


class SocketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Run mocked SDK work inline; unit tests do not need an executor or real I/O.
        async def offload(function, *args, **kwargs):
            return function(*args, **kwargs)
        p = patch.object(voice.asyncio, "to_thread", offload)
        p.start(); self.addCleanup(p.stop)
        self.active = voice._active
        voice._active = None
        self.addCleanup(setattr, voice, "_active", self.active)
        self.session = Mock(stage="Speech synthesis")
        self.session.opening.return_value = {"type": "reply", "audios": ["opening"]}
        self.session.respond.return_value = {"type": "reply", "audios": ["response"]}
        for target, value in (("HostedVoiceSession", Mock(return_value=self.session)),
                              ("emit", Mock()), ("user_status", Mock(return_value={"pending": False}))):
            p = patch.object(voice, target, value)
            p.start(); self.addCleanup(p.stop)

    async def test_open_audio_stop_flushes_and_releases_call(self):
        audio = base64.b64encode(recording()).decode()
        socket = Socket([{"type": "start", "glid": "123", "role": "seller"},
                         {"type": "audio", "audio": audio}, {"type": "stop"}])
        await voice.serve(socket)
        self.assertEqual(len(socket.sent), 2)
        self.session.respond.assert_called_once_with(recording())
        self.session.finish.assert_called_once()
        self.assertIsNone(voice._active)
        self.assertTrue(socket.closed)

    async def test_disconnect_also_saves_memory(self):
        await voice.serve(Socket([{"type": "start", "glid": "123", "role": "buyer"}]))
        self.session.finish.assert_called_once()
        self.assertIsNone(voice._active)

    async def test_second_call_does_not_release_first_calls_reservation(self):
        voice._active = {"glid": "456", "role": "buyer"}
        socket = Socket([{"type": "start", "glid": "123", "role": "seller"}])
        await voice.serve(socket)
        self.assertEqual(socket.sent[0]["type"], "error")
        self.assertEqual(voice.status()["glid"], "456")
        self.session.finish.assert_not_called()

    async def test_pending_history_prevents_opening(self):
        socket = Socket([{"type": "start", "glid": "123", "role": "buyer"}])
        with patch.object(voice, "user_status", return_value={"pending": True}):
            await voice.serve(socket)
        self.assertIn("updating context", socket.sent[0]["message"])
        self.session.opening.assert_not_called()
        self.assertIsNone(voice._active)

    async def test_upstream_error_body_is_not_sent_to_browser(self):
        self.session.opening.side_effect = RuntimeError("secret API key and private upstream body")
        socket = Socket([{"type": "start", "glid": "123", "role": "buyer"}])
        await voice.serve(socket)
        self.assertNotIn("secret", str(socket.sent))
        self.assertIn("Speech synthesis failed", socket.sent[0]["message"])
        self.session.finish.assert_called_once()
        self.assertIsNone(voice._active)


if __name__ == "__main__":
    unittest.main()
