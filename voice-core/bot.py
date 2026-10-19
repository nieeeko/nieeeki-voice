"""nieeeki Voice-Core.

Pipeline:  Mikrofon (WebRTC, Browser-AEC) -> Silero-VAD -> Soniox STT (Streaming, DE)
           -> Turn-Erkennung -> Claude (Streaming) -> Soniox TTS (Streaming) -> Lautsprecher

Unterbrechungen:
  * Waehrend nieeeki spricht, laufen VAD + STT weiter.
  * MeaningfulWordsUserTurnStartStrategy: Rueckmeldelaute ("mhm", "ja genau") stoppen sie
    nicht, jedes andere Wort schon ("Doch", "Stopp").
  * Pipecat schreibt nur die TATSAECHLICH gesprochenen Woerter in den Kontext
    (Soniox TTS liefert Wort-Zeitstempel). Nach einer Unterbrechung sieht Claude
    genau, wo sie aufgehoert hat.
  * on_assistant_turn_stopped(interrupted=True) haengt zusaetzlich eine kurze
    Systemnotiz an, damit Claude bewusst anknuepft statt neu anzufangen.

Start lokal:  uv run bot.py   (oder: python bot.py)  -> http://localhost:7860
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMUpdateSettingsFrame, TTSSpeakFrame
from pipecat.services.settings import LLMSettings
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.utils.context.llm_context_summarization import (
    LLMAutoContextSummarizationConfig,
    LLMContextSummaryConfig,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.services.soniox.stt import SonioxContextObject, SonioxSTTService
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
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from memory import MemoryStore
from persona import build_system_prompt, greeting_for
from activity import ActivityReporter
from auth_gate import install_owner_gate
from audio_monitor import AudioInputMonitor, SpeakerTracker
from learning import learn_from_session
from dev_agent import DevAgent
from pc_host import decide_pc_approval, host_client, host_tools, report_pc_approval
from screen import register_screen_tools
from screen_stream import ScreenStreamer, register_stream_tool, video_params
from tools import register_tools
from soniox_tts import LazySonioxTTSService
from soft_interrupt import (
    MeaningfulWordsUserTurnStartStrategy,
    PlaybackTracker,
    SoftInterruptGate,
)

load_dotenv(override=True)

if sys.platform == "win32":
    # Hoehere Prioritaet: wenn nieeeki Programme oeffnet, belegen die kurz die CPU; das Audio soll
    # dann nicht stocken.
    import ctypes

    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x8000)

# Standard: EU-Endpunkte (Datenschutz); .env kann auf US umstellen
SONIOX_STT_URL = os.getenv("SONIOX_STT_URL", "wss://stt-rt.eu.soniox.com/transcribe-websocket")
SONIOX_TTS_URL = os.getenv("SONIOX_TTS_URL", "wss://tts-rt.soniox.com/tts-websocket")
SONIOX_VOICE = os.getenv("SONIOX_VOICE", "Nina")  # spaeter: UUID der geklonten Stimme
# Fachbegriffe fuer die Spracherkennung: Programme und Befehle, die Nikolaj oft sagt. Am 18.10. kam
# "öffne mal Opera" als "erste Mal Opera" an, und nieeeki hat Opera nicht geoeffnet. Bewusst nur
# "terms", kein "general": ein allgemeiner Kontext (domain=Sprachassistent) drueckte seine Woerter
# in fremde Saetze. .env: SONIOX_TERMS (kommagetrennt), leer = ohne Fachbegriffe.
DEFAULT_STT_TERMS = (
    "nieeeki, öffne mal, Opera, Opera GX, WhatsApp, VS Code, Visual Studio Code, Claude Code, "
    "Chrome, Spotify, Explorer, VPN, Canvas, Dark Mode, SQL-Statement, Supabase, Vercel, GitHub"
)
STT_TERMS = [t.strip() for t in os.getenv("SONIOX_TERMS", DEFAULT_STT_TERMS).split(",") if t.strip()]
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL_FAST", "claude-haiku-5-5")
# Woerter ohne Rueckmeldelaute, ab denen sie unterbrochen wird ("Doch" reicht, "mhm" nicht)
MIN_INTERRUPT_WORDS = int(os.getenv("MIN_INTERRUPT_WORDS", "1"))
# Lernen im laufenden Gespraech: nach so vielen neuen Saetzen von Nikolaj wertet Haiku den
# Abschnitt aus; Neues landet sofort im System-Prompt.
LEARN_EVERY_USER_TURNS = int(os.getenv("LEARN_EVERY_USER_TURNS") or 4)
LEARN_OVERLAP = 4
SUMMARY_TRIGGER_TOKENS = int(os.getenv("SUMMARY_TRIGGER_TOKENS") or 60000)
SUMMARY_TARGET_TOKENS = int(os.getenv("SUMMARY_TARGET_TOKENS") or 25000)
SUMMARY_PROMPT = (
    "Fasse das bisherige Gespraech zwischen Nikolaj und nieeeki auf Deutsch knapp zusammen: Themen, "
    "Entscheidungen, offene Auftraege, Namen, Zahlen, Links und was nieeeki am PC getan hat. "
    "Nichts erfinden, keine Wertungen."
)  # so viele fruehere Zeilen als Kontext mitgeben
NIEEEKI_GREETING = os.getenv("NIEEEKI_GREETING", "Hi Nikolaj, ich bin da.")
NIEEEKI_WAKE_GREETING = os.getenv("NIEEEKI_WAKE_GREETING", "Ja?")  # per Weckwort gestartet
# Haelt Claude Nikolajs Satz fuer unvollstaendig, wartet sie so lange auf den Rest (Sekunden).
SMART_TURN_STOP_SECS = float(os.getenv("SMART_TURN_STOP_SECS") or 1.0)
TURN_INCOMPLETE_SHORT_SECS = float(os.getenv("TURN_INCOMPLETE_SHORT_SECS") or 1.5)
# Nach einer Pause fragt Pipecat Claude erneut. Die englischen Standardtexte fuehrten dazu, dass
# Claude nur den Marker "●" ohne Text schickte: nieeeki blieb stumm.
PAUSE_SHORT_PROMPT = (
    "Nikolaj hat mitten im Satz kurz pausiert. Lies seine letzten Bruchstuecke zusammen. Ist klar, "
    "was er will, erledige es jetzt (auch mit Werkzeug). Sonst sag einen ganz kurzen deutschen Satz, "
    "der ihn weiterreden laesst. WICHTIG: Beginne mit ● und einem Leerzeichen, dann dein Text oder "
    "Werkzeugaufruf. Nie ◐ oder ○, nie ● allein. Beispiel: ● Ja, das Pop-up, und dann?"
)
PAUSE_LONG_PROMPT = (
    "Nikolaj ist seit ein paar Sekunden still. Ist aus dem Zusammenhang klar, was er will, erledige "
    "es jetzt. Sonst frag in einem kurzen deutschen Satz freundlich nach. WICHTIG: Beginne mit ● "
    "und einem Leerzeichen, dann dein Text. Nie ◐ oder ○, nie ● allein. Beispiel: ● Bin noch da, "
    "sag einfach Bescheid."
)
TURN_INCOMPLETE_LONG_SECS = float(os.getenv("TURN_INCOMPLETE_LONG_SECS") or 3.0)  # 8 s fuehlte sich wie ein Haenger an
# Weiche Unterbrechung: nach einem Einwurf spricht sie den Satz zu Ende, hoechstens aber
# das laufende Wort plus so viele weitere Woerter (nie mitten im Wort).
SOFT_INTERRUPT_MAX_WORDS = int(os.getenv("SOFT_INTERRUPT_MAX_WORDS") or 3)
# So weit laufen die Wort-Zeitstempel dem hoerbaren Ton voraus (gemessen: ~0,5 s).
SOFT_INTERRUPT_LAG_SECS = float(os.getenv("SOFT_INTERRUPT_LAG_SECS") or 0.5)

INTERRUPTED_NOTE = (
    "[Hinweis fuer nieeeki: Du wurdest gerade unterbrochen. Deine letzte Antwort endet dort, "
    "wo du aufgehoert hast. Reagiere natuerlich auf den Einwurf und knuepfe dann, falls noch "
    "relevant, an deinem letzten Gedanken an - nicht von vorn beginnen.]"
)

# Profil (Fakten, Vorlieben, Sprechweise) zwischenspeichern: die Supabase-Abfrage dauerte vor jedem
# Gespraechsstart ~1,7 s. Ab dem zweiten Gespraech sofort aus dem Speicher, frisch im Hintergrund.
_profile_cache: list[str] = []
_profile_loaded = False


async def cached_profile(memory) -> list[str]:
    global _profile_loaded

    async def refresh():
        facts = await memory.recall_profile()
        if facts:
            _profile_cache[:] = facts

    if not _profile_loaded:
        await refresh()
        _profile_loaded = True
    else:
        task = asyncio.create_task(refresh())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    return list(_profile_cache)


# Referenzen halten, sonst raeumt der Garbage Collector laufende Lern-Tasks weg.
_background_tasks: set[asyncio.Task] = set()

transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        **video_params(),  # Bildschirm-Stream (screen_stream.py), nur auf Wunsch
    ),
}


async def run_bot(transport, runner_args: RunnerArguments) -> None:
    # Immer Nikolajs Gedaechtnis: nur er kommt durch die Zugangskontrolle (auth_gate.py). Eine
    # user_id aus dem Anfrage-Body wird bewusst ignoriert, die koennte jeder Client setzen.
    memory = MemoryStore.from_env()

    stt = SonioxSTTService(
        api_key=os.environ["SONIOX_API_KEY"],
        url=SONIOX_STT_URL,
        settings=SonioxSTTService.Settings(
            # Nur Deutsch, strikt: mit DE+EN wurde "Wie geht's?" als "We get." erkannt.
            # Englische Fachwoerter (SQL, Statement) bleiben trotzdem erkennbar.
            language_hints=[Language.DE],
            language_hints_strict=True,
            enable_speaker_diarization=True,  # mehrere Personen im Raum -> Sprecher-Labels
            context=SonioxContextObject(terms=STT_TERMS) if STT_TERMS else None,
        ),
    )

    tts = LazySonioxTTSService(
        api_key=os.environ["SONIOX_API_KEY"],
        url=SONIOX_TTS_URL,
        settings=SonioxTTSService.Settings(voice=SONIOX_VOICE, language=Language.DE),
    )

    system_prompt = build_system_prompt(await cached_profile(memory))

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

    tools = register_tools(llm, memory)
    # Entwicklungsauftraege an Claude Code; Freigaben per Klick in der UI (siehe dev_agent.py)
    dev = DevAgent(memory, user_turns=lambda: sum(1 for role, _ in transcript if role == "user"))
    dev.register(llm)
    # Live-Bildschirm in der Oberflaeche (Fernzugriff vom Handy), per Knopf oder Werkzeug
    streamer = ScreenStreamer()
    stream_schema = register_stream_tool(llm, streamer)
    tools = ToolsSchema(standard_tools=[*tools.standard_tools, *dev.schemas(), stream_schema])
    context = LLMContext(tools=tools)

    # PC-Werkzeuge (Host per MCP-stdio) im Hintergrund dazuholen: das Gespraech wartet nicht darauf.
    pc_client = host_client()
    # Bildschirm sehen/klicken: Foto vom Host, Bildmodell entscheidet (siehe screen.py)
    screen_schemas = register_screen_tools(llm, lambda: pc_client)

    async def attach_pc_tools():
        pc = await host_tools(pc_client)
        if pc:
            context.set_tools(
                ToolsSchema(standard_tools=[*tools.standard_tools, *pc.standard_tools, *screen_schemas])
            )

    pc_task = asyncio.create_task(attach_pc_tools())
    _background_tasks.add(pc_task)
    pc_task.add_done_callback(_background_tasks.discard)
    transcript: list[tuple[str, str]] = []  # fuers Lernen
    learned_upto = 0  # bis zu diesem Index in transcript ist ausgewertet
    learn_task: asyncio.Task | None = None

    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        # Lange Gespraeche zusammenfassen, bevor der Prompt teuer wird (Haiku: ab 100k Tokens 5x Preis)
        assistant_params=LLMAssistantAggregatorParams(
            enable_auto_context_summarization=True,
            auto_context_summarization_config=LLMAutoContextSummarizationConfig(
                max_context_tokens=SUMMARY_TRIGGER_TOKENS,
                max_unsummarized_messages=None,
                summary_config=LLMContextSummaryConfig(
                    target_context_tokens=SUMMARY_TARGET_TOKENS,
                    min_messages_after_summary=8,
                    summarization_prompt=SUMMARY_PROMPT,
                    summary_message_template="Zusammenfassung des bisherigen Gespraechs: {summary}",
                ),
            ),
        ),
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(
                # Unterbrechen entscheidet das SoftInterruptGate, nicht die Strategie.
                start=[
                    MeaningfulWordsUserTurnStartStrategy(
                        min_words=MIN_INTERRUPT_WORDS, enable_interruptions=False
                    )
                ],
                # Satzende: Smart-Turn schlaegt vor, Claude entscheidet, ob Nikolaj fertig ist.
                # Bei Bruchstuecken ("SQL Statement.") wartet sie still, statt sofort zu antworten.
                stop=[
                    # Smart-Turn haelt kurze Antworten ("Ja") oft fuer unfertig und wartet dann
                    # stop_secs Stille ab (Standard 3 s). Claude entscheidet danach ohnehin mit.
                    deferred(
                        TurnAnalyzerUserTurnStopStrategy(
                            turn_analyzer=LocalSmartTurnAnalyzerV3(
                                params=SmartTurnParams(stop_secs=SMART_TURN_STOP_SECS)
                            )
                        )
                    ),
                    LLMTurnCompletionUserTurnStopStrategy(
                        config=UserTurnCompletionConfig(
                            incomplete_short_timeout=TURN_INCOMPLETE_SHORT_SECS,
                            incomplete_long_timeout=TURN_INCOMPLETE_LONG_SECS,
                            incomplete_short_prompt=PAUSE_SHORT_PROMPT,
                            incomplete_long_prompt=PAUSE_LONG_PROMPT,
                        )
                    ),
                ],
            ),
        ),
    )

    soft_gate = SoftInterruptGate(
        max_words=SOFT_INTERRUPT_MAX_WORDS,
        playback_lag_secs=SOFT_INTERRUPT_LAG_SECS,
    )

    pipeline = Pipeline(
        [
            transport.input(),
            AudioInputMonitor(),  # Diagnose: Luecken und Pegel des Browser-Mikrofons
            stt,
            SpeakerTracker(),  # wer spricht -> Farbe im Stimmen-Rand
            user_agg,
            soft_gate,
            llm,
            ActivityReporter(),  # Werkzeugaufrufe live an die Oberflaeche (Zeitleiste)
            tts,
            streamer,  # schiebt bei Bedarf Bildschirmbilder in die Videospur
            transport.output(),
            PlaybackTracker(soft_gate),
            assistant_agg,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    )
    dev.attach(task)

    @task.rtvi.event_handler("on_client_message")
    async def on_client_message(_rtvi, msg):
        # Klicks aus der UI (z. B. Freigaben). Kommt nur ueber die eigene WebRTC-Verbindung.
        if msg.type == "bildschirm":
            await streamer.set_active(bool((msg.data or {}).get("an")), "Knopf")
            return
        # Fernsteuerung aus dem Live-Bild am Handy (screen_stream.py, ausgefuehrt vom Host)
        data = msg.data or {}
        if msg.type == "bildschirm_klick":
            await streamer.click(data.get("x"), data.get("y"), pc_client, data.get("taste"))
            return
        if msg.type == "bildschirm_scrollen":
            await streamer.scroll(data.get("x"), data.get("y"), data.get("richtung"), data.get("schritte"), pc_client)
            return
        if msg.type == "bildschirm_tippen":
            await streamer.type_text(data.get("text"), bool(data.get("enter")), pc_client)
            return
        if msg.type == "bildschirm_taste":
            await streamer.press_key(data.get("taste"), pc_client)
            return
        # Freigabe-Karte fuer eine PC-Aktion (z. B. PowerShell); Entwicklungsauftraege gehen an dev
        if msg.type == "freigabe" and data.get("job") == "pc":
            ja = data.get("entscheidung") == "ja"
            result = await decide_pc_approval(pc_client, data.get("id"), ja)
            await report_pc_approval(task, ja, result)
            return
        await dev.on_client_message(msg.type, msg.data or {})

    @assistant_agg.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(_agg, message):
        # Erst alles, was das Gespraech braucht; die Datenbank zuletzt. Ist Supabase langsam,
        # darf das weder den Verlauf noch die weiche Unterbrechung aufhalten.
        transcript.append(("assistant", message.content))
        if message.interrupted:
            logger.info("nieeeki wurde unterbrochen nach: {!r}", message.content[-80:])
            # Nur wenn sie schon etwas gesagt hat; sonst verwirrt die Notiz Claude.
            if message.content.strip():
                context.add_message({"role": "user", "content": INTERRUPTED_NOTE})
        soft_gate.assistant_turn_recorded()
        await memory.log_turn(role="assistant", content=message.content, interrupted=message.interrupted)

    @user_agg.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(_agg, _strategy, message):
        transcript.append(("user", message.content))
        new_user_turns = sum(1 for role, _ in transcript[learned_upto:] if role == "user")
        if new_user_turns >= LEARN_EVERY_USER_TURNS and not (learn_task and not learn_task.done()):
            start_learning(live=True)
        await memory.log_turn(role="user", content=message.content)

    async def learn(live: bool, previous: asyncio.Task | None):
        nonlocal learned_upto
        if previous and not previous.done():
            await previous  # nie zwei Auswertungen gleichzeitig
        end = len(transcript)
        chunk = transcript[max(0, learned_upto - LEARN_OVERLAP):end]
        saved = await learn_from_session(memory, chunk, CLAUDE_MODEL)
        learned_upto = end
        if saved and live:
            # Sofort anwenden: neuer System-Prompt mit allem, was sie jetzt ueber Nikolaj weiss.
            facts = await memory.recall_profile()
            _profile_cache[:] = facts
            new_prompt = build_system_prompt(facts)
            await task.queue_frame(
                LLMUpdateSettingsFrame(delta=LLMSettings(system_instruction=new_prompt))
            )
            logger.info("Im Gespraech gelernt und angewendet: {}", saved)

    def start_learning(live: bool):
        nonlocal learn_task
        learn_task = asyncio.create_task(learn(live, learn_task))
        _background_tasks.add(learn_task)  # Referenz halten, laeuft auch nach dem Auflegen weiter
        learn_task.add_done_callback(_background_tasks.discard)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        # Kurze Begruessung waermt die Sprachausgabe auf: der erste Soniox-Satz einer
        # Verbindung brauchte einmal 10 s. Leer setzen, um sie abzuschalten.
        greeting = greeting_for(runner_args.body, NIEEEKI_GREETING, NIEEEKI_WAKE_GREETING)
        if greeting:
            await task.queue_frames([TTSSpeakFrame(greeting)])
        # Sitzung erst nach der Begruessung anlegen (Supabase-Insert kostete ~0,6 s vor dem ersten Ton)
        await memory.start_session(channel=os.getenv("NIEEEKI_CHANNEL", "web"))

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        await memory.end_session()
        await task.cancel()
        if pc_client:
            await pc_client.close()
        await dev.close()  # laufenden Entwicklungsauftrag beim Auflegen beenden
        # Rest seit der letzten Auswertung im Hintergrund lernen; blockiert nichts.
        start_learning(live=False)

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    await runner.run(task)


async def bot(runner_args: RunnerArguments):
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import app, main

    # Nur die eigene Oberflaeche darf Sitzungen starten. Ohne Liste erlaubt der Runner jede
    # Herkunft (CORS *): eine fremde Webseite im Browser koennte ueber localhost:7860 eine
    # Sitzung aufbauen und so die PC-Werkzeuge ausloesen. Ein leerer Wert oeffnet das nicht.
    if not os.getenv("PIPECAT_ALLOWED_ORIGINS", "").strip():
        os.environ["PIPECAT_ALLOWED_ORIGINS"] = (
            "http://localhost:3000,http://127.0.0.1:3000,https://nieeeki.nieeeko.com"
        )
    # Und auch von dort nur Nikolaj: Supabase-Login mit zweitem Faktor (auth_gate.py).
    install_owner_gate(app)
    main()
