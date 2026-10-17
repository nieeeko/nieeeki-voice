"""nieeeki Voice-Core.

Pipeline:  Mikrofon (WebRTC, Browser-AEC) -> Silero-VAD -> Soniox STT (Streaming, DE)
           -> Turn-Erkennung -> Claude (Streaming) -> Soniox TTS (Streaming) -> Lautsprecher

Unterbrechungen:
  * Waehrend nieeeki spricht, laufen VAD + STT weiter.
  * MinWordsUserTurnStartStrategy: erst ab N Woertern wird nieeeki unterbrochen,
    "mhm"/"ja" (Rueckkanal) stoppen sie also nicht.
  * Pipecat schreibt nur die TATSAECHLICH gesprochenen Woerter in den Kontext
    (Soniox TTS liefert Wort-Zeitstempel). Nach einer Unterbrechung sieht Claude
    genau, wo sie aufgehoert hat.
  * on_assistant_turn_stopped(interrupted=True) haengt zusaetzlich eine kurze
    Systemnotiz an, damit Claude bewusst anknuepft statt neu anzufangen.

Start lokal:  uv run bot.py   (oder: python bot.py)  -> http://localhost:7860
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.services.soniox.stt import SonioxSTTService
from pipecat.turns.user_stop.deferred_user_turn_stop_strategy import deferred
from pipecat.turns.user_stop.llm_turn_completion_user_turn_stop_strategy import (
    LLMTurnCompletionUserTurnStopStrategy,
)
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_completion_mixin import UserTurnCompletionConfig
from pipecat.services.soniox.tts import SonioxTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
    MinWordsUserTurnStartStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from memory import MemoryStore
from persona import build_system_prompt
from soft_interrupt import PlaybackTracker, SoftInterruptGate

load_dotenv(override=True)

# EU-Endpunkte fuer Datenschutz (WAMOCON-Inhalte)
SONIOX_STT_URL = os.getenv("SONIOX_STT_URL", "wss://stt-rt.eu.soniox.com/transcribe-websocket")
SONIOX_TTS_URL = os.getenv("SONIOX_TTS_URL", "wss://tts-rt.soniox.com/tts-websocket")
SONIOX_VOICE = os.getenv("SONIOX_VOICE", "Nina")  # spaeter: UUID der geklonten Stimme
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL_FAST", "claude-haiku-5-5")
MIN_INTERRUPT_WORDS = int(os.getenv("MIN_INTERRUPT_WORDS", "3"))
NIEEEKI_GREETING = os.getenv("NIEEEKI_GREETING", "Hi Nikolaj, ich bin da.")
# Haelt Claude Nikolajs Satz fuer unvollstaendig, wartet sie so lange auf den Rest (Sekunden).
TURN_INCOMPLETE_SHORT_SECS = float(os.getenv("TURN_INCOMPLETE_SHORT_SECS") or 3.0)
TURN_INCOMPLETE_LONG_SECS = float(os.getenv("TURN_INCOMPLETE_LONG_SECS") or 8.0)
# Weiche Unterbrechung: so lange darf sie den Satz noch zu Ende sprechen, danach
# stoppt sie nach dem laufenden Wort plus N weiteren Woertern (nie mitten im Wort).
# Kurz halten: solange beide sprechen, versteht die Spracherkennung Nikolaj schlechter.
SOFT_INTERRUPT_MAX_SECS = float(os.getenv("SOFT_INTERRUPT_MAX_SECS") or 1.5)
SOFT_INTERRUPT_EXTRA_WORDS = int(os.getenv("SOFT_INTERRUPT_EXTRA_WORDS") or 1)
# So weit laufen die Wort-Zeitstempel dem hoerbaren Ton voraus (gemessen: ~0,5 s).
SOFT_INTERRUPT_LAG_SECS = float(os.getenv("SOFT_INTERRUPT_LAG_SECS") or 0.5)

INTERRUPTED_NOTE = (
    "[Hinweis fuer nieeeki: Du wurdest gerade unterbrochen. Deine letzte Antwort endet dort, "
    "wo du aufgehoert hast. Reagiere natuerlich auf den Einwurf und knuepfe dann, falls noch "
    "relevant, an deinem letzten Gedanken an - nicht von vorn beginnen.]"
)

transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ),
}


async def run_bot(transport, runner_args: RunnerArguments) -> None:
    user_id = (runner_args.body or {}).get("user_id") if runner_args.body else None
    memory = MemoryStore.from_env(user_id=user_id)

    stt = SonioxSTTService(
        api_key=os.environ["SONIOX_API_KEY"],
        url=SONIOX_STT_URL,
        settings=SonioxSTTService.Settings(
            # Nur Deutsch, strikt: mit DE+EN wurde "Wie geht's?" als "We get." erkannt.
            # Englische Fachwoerter (SQL, Statement) bleiben trotzdem erkennbar.
            language_hints=[Language.DE],
            language_hints_strict=True,
            enable_speaker_diarization=True,  # mehrere Personen im Raum -> Sprecher-Labels
            # Kein Soniox-Kontext: "domain=Sprachassistent" hat die Erkennung so stark
            # beeinflusst, dass aus "SQL-Statement" "Sprachassistentin" wurde.
        ),
    )

    tts = SonioxTTSService(
        api_key=os.environ["SONIOX_API_KEY"],
        url=SONIOX_TTS_URL,
        settings=SonioxTTSService.Settings(voice=SONIOX_VOICE, language=Language.DE),
    )

    system_prompt = build_system_prompt(await memory.recall_profile())

    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        settings=AnthropicLLMService.Settings(
            model=CLAUDE_MODEL,
            # Persona als system_instruction, nicht als System-Nachricht im Kontext:
            # die Satzende-Erkennung haengt ihre Regeln hier an. Eine System-Nachricht
            # im Kontext wuerde Pipecat dann komplett verwerfen.
            system_instruction=system_prompt,
            enable_prompt_caching=True,
            # Haiku 5.5 denkt sonst vor jeder Antwort nach (bis 1,6 s bis zum ersten Wort).
            thinking=AnthropicLLMService.ThinkingConfig(type="disabled"),
        ),
    )

    context = LLMContext()

    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(
                # Unterbrechen entscheidet das SoftInterruptGate, nicht die Strategie.
                start=[
                    MinWordsUserTurnStartStrategy(
                        min_words=MIN_INTERRUPT_WORDS, enable_interruptions=False
                    )
                ],
                # Satzende: Smart-Turn schlaegt vor, Claude entscheidet, ob Nikolaj fertig ist.
                # Bei Bruchstuecken ("SQL Statement.") wartet sie still, statt sofort zu antworten.
                stop=[
                    deferred(TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())),
                    LLMTurnCompletionUserTurnStopStrategy(
                        config=UserTurnCompletionConfig(
                            incomplete_short_timeout=TURN_INCOMPLETE_SHORT_SECS,
                            incomplete_long_timeout=TURN_INCOMPLETE_LONG_SECS,
                        )
                    ),
                ],
            ),
        ),
    )

    soft_gate = SoftInterruptGate(
        max_finish_secs=SOFT_INTERRUPT_MAX_SECS,
        extra_words=SOFT_INTERRUPT_EXTRA_WORDS,
        playback_lag_secs=SOFT_INTERRUPT_LAG_SECS,
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_agg,
            soft_gate,
            llm,
            tts,
            transport.output(),
            PlaybackTracker(soft_gate),
            assistant_agg,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    )

    @assistant_agg.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(_agg, message):
        await memory.log_turn(role="assistant", content=message.content, interrupted=message.interrupted)
        if message.interrupted:
            logger.info("nieeeki wurde unterbrochen nach: {!r}", message.content[-80:])
            # Nur wenn sie schon etwas gesagt hat; sonst verwirrt die Notiz Claude.
            if message.content.strip():
                context.add_message({"role": "user", "content": INTERRUPTED_NOTE})
        soft_gate.assistant_turn_recorded()

    @user_agg.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(_agg, _strategy, message):
        await memory.log_turn(role="user", content=message.content)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        await memory.start_session(channel=os.getenv("NIEEEKI_CHANNEL", "web"))
        # Kurze Begruessung waermt die Sprachausgabe auf: der erste Soniox-Satz einer
        # Verbindung brauchte einmal 10 s. Leer setzen, um sie abzuschalten.
        if NIEEEKI_GREETING:
            await task.queue_frames([TTSSpeakFrame(NIEEEKI_GREETING)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        await memory.end_session()
        await task.cancel()

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(task)


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
