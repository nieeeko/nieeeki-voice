"""Weiche Unterbrechung: nieeeki haelt an der naechsten Wortgrenze inne, nie mitten im Wort.

Ablauf mit AudioHold (playback.py, Standard)
  * Ein einzelnes echtes Wort von Nikolaj, waehrend sie spricht ("Danke", "Du"): sie haelt an der
    naechsten Wortgrenze inne, wie ein Mensch, der merkt, dass der andere ansetzt. Kommt nichts nach
    (resume_after_secs Stille, hoechstens max_pause_secs), spricht sie genau dort weiter. Vorher
    brach so ein Bruchstueck die Antwort ab oder wurde ganz ueberhoert (21.10.: " Al", " Du").
    Rueckmeldelaute ("mhm", "ja genau") halten sie gar nicht an.
  * Zwei echte Woerter oder ein Sofort-Wort ("Stopp", "warte"): Unterbrechung. Haelt sie schon inne,
    sofort; sonst an der naechsten Wortgrenze (spaetestens nach boundary_wait_secs). Der Ton steht
    dann schon, darum braucht es weder das Ausblenden in der Oberflaeche noch eine feste Wartezeit.
  * PlaybackTracker (nach transport.output) meldet jedes Wort erst, wenn sein Ton wirklich hinausgeht
    (AudioHold misst das). Woerter, die nicht mehr gespielt wurden, landen nicht im Verlauf.
  * Endet Nikolajs Turn vorher, haelt das Gate die neue Anfrage an Claude zurueck, bis nieekis
    gesprochener Text im Verlauf steht, und sortiert Nikolajs Einwurf dahinter ein. Sonst sieht
    Claude die eigene Antwort nicht und faengt von vorn an.
  * Spricht nieeeki noch nicht (Claude denkt noch), wird wie bisher sofort unterbrochen.

Ohne AudioHold (NIEEEKI_INNEHALTEN=0) gilt das Verfahren vom 21.10.: Unterbrechung nach dem laufenden
Wort plus max_words weiteren oder an einer Satzgrenze, die Oberflaeche blendet aus ("leiser_stopp"),
und der Server bricht playback_lag_secs spaeter ab, weil die Wort-Zeitstempel dem Ton vorauslaufen.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Callable

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallCancelFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterruptionFrame,
    LLMContextFrame,
    SystemFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
    MinWordsUserTurnStartStrategy,
)

from playback import AudioHold

SENTENCE_END = (".", "!", "?", "…", ":")

# Was mit einem Wort geschieht, wenn sein Ton hinausgehen soll (SoftInterruptGate.on_word)
PLAY, HOLD, CUT = "spielen", "halten", "schneiden"

# Rueckmeldelaute: zeigen Zuhoeren, sind kein Einwurf. Alles andere zaehlt ("Doch", "Nein", "Stopp").
BACKCHANNEL_WORDS = frozenset(
    {
        "mhm", "mhmm", "mm", "hm", "hmm", "ähm", "äh", "aha", "ah", "oh", "ok", "okay",
        "ja", "jaja", "jo", "genau", "stimmt", "richtig", "klar", "super", "gut", "cool",
        "achso", "ach", "so", "verstehe", "uh", "huh",
    }
)


# Sofort-Woerter: Nikolaj will, dass sie aufhoert. Unterbrechen schon allein, ohne Mindestzahl.
STOP_WORDS = frozenset(
    {
        "stopp", "stop", "halt", "warte", "wart", "moment", "nein", "nee", "ruhe", "pause", "leise",
        "abbrechen", "abbruch", "aufhören", "aufhoeren",
    }
)

ANSWER_GRACE_SECS = 4.0  # nach dem letzten Werkzeug: so lange gilt sie noch als beschaeftigt
WATCH_SECS = 0.05  # so oft prueft das Gate Innehalten und Weitersprechen
POLL_SECS = 0.02  # so oft schaut der Tracker hoechstens nach, ob ein Wort dran ist
TOLERANCE_NS = 30_000_000  # Wort gilt als erreicht, wenn der Ton bis auf 30 ms dort ist


def _words(text: str) -> list[str]:
    return [w.strip(".,!?…:;-\"'").lower() for w in text.split()]


def meaningful_word_count(text: str) -> int:
    """Zaehlt echte Woerter. Einzelne Buchstaben sind Bruchstuecke der Live-Erkennung
    ("J" vor "Ja", "H" vor "Hi") und zaehlen nicht."""
    return sum(1 for w in _words(text) if len(w) >= 2 and w not in BACKCHANNEL_WORDS)


def is_stop_request(text: str) -> bool:
    """'Stopp', 'warte', 'nein', 'Moment': sofort aufhoeren, auch wenn es nur ein Wort ist."""
    return any(w in STOP_WORDS for w in _words(text))


class MeaningfulWordsUserTurnStartStrategy(MinWordsUserTurnStartStrategy):
    """Wie MinWords, aber waehrend nieeeki spricht oder arbeitet zaehlen Rueckmeldelaute nicht mit.

    "mhm" oder "ja genau" lassen sie weiterreden. Unterbrochen wird ab min_words echten Woertern
    (Soniox liefert den bisherigen Satz in jeder Teil-Erkennung, gezaehlt wird also der ganze Einwurf)
    oder sofort bei einem Sofort-Wort wie "Stopp". Weniger echte Woerter meldet on_partial: dann haelt
    sie inne und wartet ab, ob mehr kommt (SoftInterruptGate.hint).
    Dieselben Regeln gelten, solange ein Werkzeug laeuft und kurz danach, bis ihre Antwort beginnt:
    ein "okay" auf ihr "Moment." (fillers.py) wuerde sonst als neuer Satz das Werkzeug abbrechen.
    Sonst startet wie bisher jedes Wort den Turn.
    """

    def __init__(
        self,
        *,
        on_partial: Callable[[str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._on_partial = on_partial
        self._now = monotonic
        self._tools: set[str] = set()
        self._answer_due_until = 0.0  # Werkzeuge fertig, ihre Antwort kommt gleich

    async def handle_user_turn_started(self):
        await super().handle_user_turn_started()
        self._answer_due_until = 0.0

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, FunctionCallInProgressFrame):
            self._tools.add(frame.tool_call_id)
        elif isinstance(frame, (FunctionCallResultFrame, FunctionCallCancelFrame)):
            self._tools.discard(frame.tool_call_id)
            if not self._tools:
                self._answer_due_until = self._now() + ANSWER_GRACE_SECS
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._answer_due_until = 0.0
        return await super().process_frame(frame)

    def busy(self) -> bool:
        return self._bot_speaking or bool(self._tools) or self._now() < self._answer_due_until

    async def _handle_transcription(self, frame) -> ProcessFrameResult:
        if not self.busy():
            return await super()._handle_transcription(frame)
        count = meaningful_word_count(frame.text)
        if count >= self._min_words or is_stop_request(frame.text):
            logger.debug(f"{self}: Einwurf erkannt ({frame.text!r})")
            await self.trigger_user_turn_started()
            return ProcessFrameResult.STOP
        if count and self._bot_speaking and self._on_partial:
            self._on_partial(frame.text)
        await self.trigger_reset_aggregation()
        return ProcessFrameResult.CONTINUE


ASSISTANT_RECORD_TIMEOUT_SECS = 1.0


class SoftInterruptGate(FrameProcessor):
    """Entscheidet an jeder hoerbaren Wortgrenze: weiterspielen, innehalten oder abbrechen."""

    def __init__(
        self,
        *,
        max_words: int = 0,
        playback_lag_secs: float = 0.5,
        hold: AudioHold | None = None,
        resume_after_secs: float = 0.7,
        max_pause_secs: float = 2.5,
        boundary_wait_secs: float = 0.4,
        monotonic: Callable[[], float] = time.monotonic,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._max_words = max_words
        self._playback_lag_secs = playback_lag_secs
        self._hold = hold
        self._resume_after = resume_after_secs
        self._max_pause = max_pause_secs
        self._boundary_wait = boundary_wait_secs
        self._now = monotonic
        self._bot_speaking = False
        self._pending = False  # Nikolaj spricht, Unterbrechung steht aus
        self._firing = False  # Grenze gefunden, Unterbrechung laeuft
        self._armed_at = 0.0
        self._last_word_ended_sentence = False
        self._words_left = 0  # so viele Woerter darf sie nach dem Einwurf noch sprechen (ohne Hold)
        # Innehalten: Nikolaj setzt an (ein Wort), sie haelt an der naechsten Wortgrenze inne
        self._hint_since: float | None = None
        self._paused_since: float | None = None
        self._hint_text = ""
        self._last_hint = 0.0
        self._resumed_text = ""  # dieser Ansatz hat schon einmal angehalten, nicht noch einmal
        self._user_speaking = False
        self._user_quiet_since = 0.0
        self._held: list[Frame] = []
        self._held_user_msgs: list[dict] = []
        self._fire_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._turns_recorded = 0
        self._turns_at_arm = 0
        self._turn_recorded = asyncio.Event()

    def assistant_turn_recorded(self):
        """Aus on_assistant_turn_stopped aufrufen: nieekis Text steht jetzt im Verlauf."""
        self._turns_recorded += 1
        self._turn_recorded.set()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._last_word_ended_sentence = False
            self._resumed_text = ""
            self._clear_hint(resume=True)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._clear_hint(resume=True)
            if self._pending:
                await self.push_frame(frame, direction)
                self._start_fire("Antwort zu Ende", interrupt=False)
                return
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._user_speaking = True
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._user_speaking = False
            self._user_quiet_since = self._now()
        elif isinstance(frame, InterruptionFrame):
            # Unterbrechung von anderer Stelle: nichts mehr zurueckhalten.
            self._clear_hint()
            await self.push_frame(frame, direction)
            if self._pending:
                self._start_fire("externe Unterbrechung", interrupt=False)
            return
        elif isinstance(frame, UserStartedSpeakingFrame) and direction == FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            if self._bot_speaking:
                self._arm()
            elif not self._firing:
                # nieeeki spricht noch nicht (Claude denkt) -> sofort abbrechen wie bisher.
                await self.push_frame(InterruptionFrame())
            return
        elif (
            isinstance(frame, LLMContextFrame)
            and direction == FrameDirection.DOWNSTREAM
            and (self._pending or self._firing)
        ):
            messages = frame.context.messages
            if messages and messages[-1].get("role") == "user":
                self._held_user_msgs.append(messages[-1])
            self._held.append(frame)
            return

        await self.push_frame(frame, direction)

    def hint(self, text: str):
        """Von der Start-Strategie: ein einzelnes echtes Wort, waehrend sie spricht.

        Sie haelt an der naechsten Wortgrenze inne und wartet ab, ob er weiterspricht.
        """
        if self._hold is None or not self._bot_speaking or self._pending or self._firing:
            return
        key = " ".join(_words(text))
        if key == self._resumed_text:
            return  # derselbe Ansatz hat schon einmal angehalten und kam nicht weiter
        self._hint_text = key
        self._last_hint = self._now()
        if self._hint_since is None and self._paused_since is None:
            self._hint_since = self._last_hint
            logger.debug(f"{self}: Nikolaj setzt an ({text!r}), nieeeki haelt an der naechsten Wortgrenze inne")
            self._ensure_watch()

    def on_word(self, text: str) -> str:
        """Vom PlaybackTracker, wenn der Ton eines Worts hinausgehen soll.

        Beginnt ein neues Wort, ist das vorige fertig: hier liegt eine Wortgrenze.
        PLAY: spielen. HOLD: sie haelt davor inne, das Wort kommt spaeter wieder. CUT: abgeschnitten,
        gilt als nicht gesprochen.
        """
        if self._firing:
            return CUT
        if self._pending:
            if self._hold is not None:
                self._hold.pause()
                self._start_fire("Wortgrenze")
                return CUT
            if self._last_word_ended_sentence:
                self._start_fire("Satzende")
                return CUT
            if self._words_left <= 0:
                self._start_fire("Wortlimit")
                return CUT
            self._words_left -= 1
        elif self._paused_since is not None:
            return HOLD
        elif self._hint_since is not None:
            self._pause("Wortgrenze")
            return HOLD
        self._last_word_ended_sentence = text.rstrip().endswith(SENTENCE_END)
        return PLAY

    def on_word_played(self, text: str) -> bool:
        """Alte Schnittstelle: True = abgeschnitten."""
        return self.on_word(text) == CUT

    def _arm(self):
        if self._pending or self._firing:
            return
        self._pending = True
        self._armed_at = self._now()
        self._words_left = self._max_words
        self._turns_at_arm = self._turns_recorded
        paused = self._paused_since is not None or (self._hold is not None and self._hold.paused)
        self._hint_since = None
        self._paused_since = None
        if self._hold is not None and paused:
            self._start_fire("Einwurf bestaetigt")
            return
        logger.debug(f"{self}: Nikolaj spricht, nieeeki hoert an der naechsten Wortgrenze auf")
        if self._hold is not None:
            self._ensure_watch()

    def _pause(self, reason: str):
        self._hint_since = None
        self._paused_since = self._now()
        if self._hold is not None:
            self._hold.pause()
        logger.info(f"{self}: nieeeki haelt inne ({reason}, Ansatz {self._hint_text!r})")

    def _resume(self, reason: str):
        self._paused_since = None
        self._resumed_text = self._hint_text
        if self._hold is not None:
            self._hold.resume()
        logger.info(f"{self}: nieeeki spricht weiter ({reason}, Ansatz {self._hint_text!r})")

    def _clear_hint(self, *, resume: bool = False):
        if resume and self._paused_since is not None and self._hold is not None:
            self._hold.resume()  # nie in eine neue Antwort hinein stumm bleiben
        self._hint_since = None
        self._paused_since = None

    def check_timers(self) -> bool:
        """Innehalten, Weitersprechen und Unterbrechen ohne Wortgrenze. True: weiter beobachten."""
        if self._firing or self._hold is None:
            return False
        now = self._now()
        if self._pending:
            if now - self._armed_at >= self._boundary_wait:
                # Keine Wortgrenze in Sicht (lange Pause zwischen zwei Saetzen): jetzt abbrechen.
                self._hold.pause()
                self._start_fire("ohne Wortgrenze")
                return False
            return True
        if self._hint_since is not None:
            if now - self._hint_since >= self._boundary_wait:
                self._pause("ohne Wortgrenze")
            return True
        if self._paused_since is not None:
            if now - self._paused_since >= self._max_pause:
                self._resume("Hoechstdauer")
                return False
            quiet_since = max(self._last_hint, self._user_quiet_since)
            if not self._user_speaking and now - quiet_since >= self._resume_after:
                self._resume("kein weiteres Wort")
                return False
            return True
        return False

    def _ensure_watch(self):
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = self.create_task(self._watch(), name="soft_interrupt_watch")

    async def _watch(self):
        try:
            while True:
                await asyncio.sleep(WATCH_SECS)
                if not self.check_timers():
                    return
        finally:
            self._watch_task = None

    def _start_fire(self, reason: str, *, interrupt: bool = True):
        if not self._pending:
            return
        self._pending = False
        self._firing = True
        # Eigener Task: der Aufrufer (Tracker) darf nicht blockieren, sonst haengt die Pipeline.
        self._fire_task = self.create_task(
            self._fire(reason, interrupt), name="soft_interrupt_fire"
        )

    async def _fire(self, reason: str, interrupt: bool):
        try:
            if interrupt:
                if self._hold is not None:
                    # Der Ton steht schon an der Wortgrenze; der Abbruch verwirft den Rest.
                    self._hold.pause()
                    logger.info(f"{self}: weiche Unterbrechung ({reason})")
                else:
                    # Die Oberflaeche hoert, was gerade erklingt: sie blendet in der naechsten
                    # Wortpause aus. Der Server bricht danach ab (Zeitstempel laufen dem Ton voraus).
                    await self.push_frame(RTVIServerMessageFrame(data={"type": "leiser_stopp"}))
                    await asyncio.sleep(self._playback_lag_secs)
                    logger.info(f"{self}: weiche Unterbrechung ({reason})")
                await self.push_frame(InterruptionFrame())
            else:
                logger.info(f"{self}: keine Unterbrechung noetig ({reason})")
            await self._wait_for_assistant_turn()
            self._move_held_user_messages_to_end()
        finally:
            self._firing = False
            self._fire_task = None
            held, self._held = self._held, []
            self._held_user_msgs = []
            for frame in held:
                await self.push_frame(frame)

    async def _wait_for_assistant_turn(self):
        deadline = asyncio.get_running_loop().time() + ASSISTANT_RECORD_TIMEOUT_SECS
        while self._turns_recorded <= self._turns_at_arm:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.warning(f"{self}: nieekis Antwort nicht rechtzeitig im Verlauf")
                return
            self._turn_recorded.clear()
            try:
                await asyncio.wait_for(self._turn_recorded.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    def _move_held_user_messages_to_end(self):
        """Nikolajs Einwurf hinter nieekis gesprochenen Text einsortieren."""
        if not self._held or not self._held_user_msgs:
            return
        context = self._held[-1].context
        held_ids = {id(m) for m in self._held_user_msgs}
        messages = list(context.messages)
        if not messages or id(messages[-1]) in held_ids:
            return  # steht schon am Ende, nichts zu tun
        rest = [m for m in messages if id(m) not in held_ids]
        moved = [m for m in messages if id(m) in held_ids]
        context.set_messages(rest + moved)

    async def cleanup(self):
        await super().cleanup()
        for task in (self._fire_task, self._watch_task):
            if task:
                await self.cancel_task(task)
        self._fire_task = None
        self._watch_task = None


class PlaybackTracker(FrameProcessor):
    """Sitzt hinter transport.output() und meldet dem Gate jedes Wort, wenn sein Ton hinausgeht.

    Mit AudioHold wartet ein Wort, bis der geschriebene Ton seine Stelle erreicht hat, und alles danach
    wartet mit (die Reihenfolge fuer den Verlauf bleibt). Haelt nieeeki inne, warten die Woerter mit.
    Ohne AudioHold gilt der Zeitstempel (Pipecat schickt das Wort, wenn seine Uhrzeit erreicht ist).
    """

    def __init__(
        self,
        gate: SoftInterruptGate,
        hold: AudioHold | None = None,
        *,
        max_wait_secs: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._gate = gate
        self._hold = hold
        self._max_wait = max_wait_secs  # Sicherung, falls nie Ton kommt
        self._now = monotonic
        self._queue: deque[tuple[float, Frame]] = deque()
        self._task: asyncio.Task | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, InterruptionFrame):
            self._queue.clear()  # nicht mehr gespielt -> nicht in den Verlauf
            if self._hold is not None:
                self._hold.cut()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, BotStoppedSpeakingFrame):
            await self._flush()  # der Ton ist durch, alles Wartende auch
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, SystemFrame):
            await self.push_frame(frame, direction)
            return
        if self._queue or self._must_wait(frame):
            self._queue.append((self._now(), frame))
            self._ensure_task()
            return
        if not await self._deliver(frame):
            self._queue.appendleft((self._now(), frame))
            self._ensure_task()

    def _must_wait(self, frame: Frame) -> bool:
        if self._hold is None or not isinstance(frame, TTSTextFrame) or not frame.pts:
            return False
        if self._hold.paused:
            return True
        played = self._hold.played_until_ns()
        return played is None or played + TOLERANCE_NS < frame.pts

    async def _deliver(self, frame: Frame) -> bool:
        """False: das Wort wartet, weil nieeeki davor innehaelt."""
        if isinstance(frame, TTSTextFrame):
            decision = self._gate.on_word(frame.text)
            if decision == HOLD:
                return False
            if decision == CUT:
                return True  # abgeschnitten -> nicht in den Gespraechsverlauf
        await self.push_frame(frame)
        return True

    def _ensure_task(self):
        if self._task is None or self._task.done():
            self._task = self.create_task(self._drain(), name="playback_tracker")

    async def _wait(self):
        if self._hold is not None:
            await self._hold.wait_progress(POLL_SECS)
        else:
            await asyncio.sleep(POLL_SECS)

    async def _drain(self):
        try:
            while self._queue:
                since, frame = self._queue[0]
                paused = self._hold is not None and self._hold.paused
                overdue = self._now() - since > self._max_wait and not paused
                if self._must_wait(frame) and not overdue:
                    await self._wait()
                    continue
                self._queue.popleft()
                if not await self._deliver(frame):
                    self._queue.appendleft((since, frame))
                    await self._wait()
        finally:
            self._task = None

    async def _flush(self):
        if self._task:
            await self.cancel_task(self._task)
            self._task = None
        while self._queue:
            _, frame = self._queue.popleft()
            if isinstance(frame, TTSTextFrame) and self._gate.on_word(frame.text) == CUT:
                continue
            await self.push_frame(frame)

    async def cleanup(self):
        await super().cleanup()
        if self._task:
            await self.cancel_task(self._task)
            self._task = None
