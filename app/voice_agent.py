"""Voice channel: browser call (WebRTC) -> Sarvam STT -> Sarvam LLM -> Sarvam TTS.

Run: python voice_agent.py   then open http://localhost:7860/client and click Connect.
The user is chosen by DEMO_GLID / DEMO_ROLE in .env. Before the server starts, the
context file is found or generated (view_builder). On hang-up the transcript is
saved to memory/, so the chat app picks the conversation up.
"""

import asyncio
import os

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMRunFrame,
    LLMTextFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.sarvam.llm import SarvamLLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import TTS_MODEL_CONFIGS, SarvamTTSService, get_speakers_for_model
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_start import MinWordsUserTurnStartStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from config import load_config
from context import load_prompt
from events import emit
from role_conversation import RoleConversation

load_dotenv(override=True)
API_KEY = os.environ["SARVAM_API_KEY"]
# The console passes VOICE_GLID/VOICE_ROLE. They are not in .env, so load_dotenv(override=True)
# above cannot replace them; DEMO_GLID/DEMO_ROLE from .env apply only when run by hand.
GLID = os.getenv("VOICE_GLID") or os.getenv("DEMO_GLID", "271504729")
ROLE = os.getenv("VOICE_ROLE") or os.getenv("DEMO_ROLE", "seller")


class TurnTap(FrameProcessor):
    """Passes every frame through; emits user transcripts and complete bot replies for the console."""

    def __init__(self, conversation, llm_context, user_input=False):
        super().__init__()
        self._bot_text = []
        self.conversation = conversation
        self.llm_context = llm_context
        self.user_input = user_input

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if self.user_input and isinstance(frame, TranscriptionFrame) and frame.text.strip():
            prompt = await asyncio.to_thread(self.conversation.before_user, frame.text)
            self.llm_context.get_messages()[0]['content'] = prompt
            emit("turn", "user", GLID, ROLE, channel="voice", text=frame.text)
        elif isinstance(frame, LLMTextFrame):
            self._bot_text.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame) and self._bot_text:
            text = ''.join(self._bot_text).strip()
            self.conversation.on_bot(text)
            emit("turn", "bot", GLID, ROLE, channel="voice", text=text)
            self._bot_text = []
        await self.push_frame(frame, direction)


async def bot(runner_args: RunnerArguments):
    try:
        await run_call(runner_args)
    except Exception as e:
        # Without this the error only reaches the log; the console would still say "ready".
        emit("voice", "error", GLID, ROLE, error=f"{type(e).__name__}: {e}"[:300])
        raise


async def run_call(runner_args: RunnerArguments):
    transport = await create_transport(
        runner_args,
        {"webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True)},
    )

    cfg = load_config()
    voice, turns_cfg = cfg["voice"], cfg["turn_taking"]

    stt = SarvamSTTService(
        api_key=API_KEY,
        model=voice["stt_model"],
        language=voice["stt_language"],
        mode=voice.get('stt_mode', 'translit'),
    )
    tts_model, tts_voice = voice["tts_model"], voice["tts_voice"]
    if tts_model not in TTS_MODEL_CONFIGS:
        logger.warning(f"tts_model {tts_model} not supported by this pipecat version, using bulbul:v3")
        tts_model = "bulbul:v3"
    if tts_voice not in get_speakers_for_model(tts_model):
        logger.warning(f"tts_voice {tts_voice} not available for {tts_model}, using simran")
        tts_voice = "simran"
    tts = SarvamTTSService(
        api_key=API_KEY,
        settings=SarvamTTSService.Settings(
            model=tts_model,
            voice=tts_voice,
            language=Language(voice["tts_language"]),
            pace=float(voice["tts_pace"]),
        ),
    )
    llm = SarvamLLMService(
        api_key=API_KEY,
        settings=SarvamLLMService.Settings(model=cfg["llm"]["model"]),
    )

    conversation = RoleConversation(GLID, ROLE, 'voice')
    system_prompt = conversation.system_prompt()
    logger.info(f"Loaded context for {ROLE} {GLID}: {len(system_prompt)} chars")

    context = LLMContext([{"role": "system", "content": system_prompt}])
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # Stricter than pipecat defaults (0.7 / 0.2 s / 0.6) so echo and room noise count less as speech.
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    confidence=turns_cfg["vad_confidence"],
                    start_secs=turns_cfg["vad_start_secs"],
                    min_volume=turns_cfg["vad_min_volume"],
                )
            ),
            # Turn starts on transcribed words, not raw sound: 1 word when the bot is quiet,
            # interrupt_min_words while it is speaking (stops echo and "hmm" cutting it off).
            user_turn_strategies=UserTurnStrategies(
                start=[MinWordsUserTurnStartStrategy(min_words=turns_cfg["interrupt_min_words"])]
            ),
        ),
    )

    pipeline = Pipeline([
        transport.input(),
        stt,
        TurnTap(conversation, context, user_input=True),
        context_aggregator.user(),
        llm,
        TurnTap(conversation, context),
        tts,
        transport.output(),
        context_aggregator.assistant(),
    ])
    task = PipelineTask(pipeline)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        emit("voice", "connected", GLID, ROLE)
        context.add_message({"role": "system", "content": load_prompt("voice_opening")})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        turns = [
            {"speaker": "user" if m["role"] == "user" else "bot", "text": m["content"]}
            for m in context.get_messages()
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
        ]
        conversation.flush()
        logger.info(f"Saved {len(turns)} voice turns to memory")
        emit("voice", "ended", GLID, ROLE)
        await task.cancel()

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(task)


if __name__ == "__main__":
    from pipecat.runner.run import main

    from view_builder import get_view

    # Prewarm: find or generate the context file now, so the call opens without delay.
    logger.info(f"Preparing {ROLE}.md for GLID {GLID}...")
    emit("voice", "start", GLID, ROLE)
    view = get_view(GLID, ROLE)
    logger.info("Context ready" if view else "No data: cold start, generic opening")
    emit("voice", "ready", GLID, ROLE)

    main()
