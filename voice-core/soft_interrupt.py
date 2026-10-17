"""Weiche Unterbrechung: nieeeki spricht den Satz zu Ende, statt mitten im Wort abzubrechen.

Ablauf
  * Die Start-Strategie meldet Nikolajs Turn OHNE sofortige Unterbrechung
    (MinWordsUserTurnStartStrategy(enable_interruptions=False)).
  * SoftInterruptGate (zwischen user_agg und llm) merkt: Nikolaj redet, waehrend nieeeki spricht.
  * PlaybackTracker (nach transport.output) meldet jedes Wort, sobald es laut Zeitstempel beginnt.
  * Unterbrochen wird an der naechsten Satzgrenze, spaetestens aber nach dem laufenden Wort plus
    max_words weiteren Woertern, also immer an einer Wortgrenze.
  * Die Wort-Zeitstempel laufen dem hoerbaren Ton voraus (Puffer im WebRTC-Pfad). Deshalb wird
    die Unterbrechung um playback_lag_secs verzoegert; Woerter in dieser Spanne gelten als nicht
    gesprochen.
  * Endet Nikolajs Turn vorher, haelt das Gate die neue Anfrage an Claude zurueck, bis nieeekis
    gesprochener Text im Verlauf steht, und sortiert Nikolajs Einwurf dahinter ein. Sonst sieht
    Claude die eigene Antwort nicht und faengt von vorn an.
  * Spricht nieeeki noch nicht (Claude denkt noch), wird wie bisher sofort unterbrochen.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
    MinWordsUserTurnStartStrategy,
)

SENTENCE_END = (".", "!", "?", "…", ":")

# Rueckmeldelaute: zeigen Zuhoeren, sind kein Einwurf. Alles andere zaehlt ("Doch", "Nein", "Stopp").
BACKCHANNEL_WORDS = frozenset(
    {
        "mhm", "mhmm", "mm", "hm", "hmm", "ähm", "äh", "aha", "ah", "oh", "ok", "okay",
        "ja", "jaja", "jo", "genau", "stimmt", "richtig", "klar", "super", "gut", "cool",
        "achso", "ach", "so", "verstehe", "uh", "huh",
    }
)


def meaningful_word_count(text: str) -> int:
    """Zaehlt echte Woerter. Einzelne Buchstaben sind Bruchstuecke der Live-Erkennung
    ("J" vor "Ja", "H" vor "Hi") und zaehlen nicht."""
    words = (w.strip(".,!?…:;-\"'").lower() for w in text.split())
    return sum(1 for w in words if len(w) >= 2 and w not in BACKCHANNEL_WORDS)


class MeaningfulWordsUserTurnStartStrategy(MinWordsUserTurnStartStrategy):
    """Wie MinWords, aber waehrend nieeeki spricht zaehlen Rueckmeldelaute nicht mit.

    "mhm" oder "ja genau" lassen sie weiterreden, ein einzelnes "Doch" oder "Stopp" unterbricht.
    Spricht sie nicht, startet wie bisher jedes Wort den Turn.
    """

    async def _handle_transcription(self, frame) -> ProcessFrameResult:
        if not self._bot_speaking:
            return await super()._handle_transcription(frame)
        count = meaningful_word_count(frame.text)
        if count >= self._min_words:
            logger.debug(f"{self}: Einwurf erkannt ({frame.text!r})")
            await self.trigger_user_turn_started()
            return ProcessFrameResult.STOP
        await self.trigger_reset_aggregation()
        return ProcessFrameResult.CONTINUE
ASSISTANT_RECORD_TIMEOUT_SECS = 1.0


class SoftInterruptGate(FrameProcessor):
    """Verzoegert Unterbrechungen bis zur naechsten hoerbaren Satz- bzw. Wortgrenze."""

    def __init__(
        self,
        *,
        max_words: int = 3,
        playback_lag_secs: float = 0.5,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._max_words = max_words
        self._playback_lag_secs = playback_lag_secs
        self._bot_speaking = False
        self._pending = False  # Nikolaj spricht, Unterbrechung steht aus
        self._firing = False  # Grenze gefunden, Unterbrechung laeuft
        self._last_word_ended_sentence = False
        self._words_left = 0  # so viele Woerter darf sie nach dem Einwurf noch sprechen
        self._held: list[Frame] = []
        self._held_user_msgs: list[dict] = []
        self._fire_task: asyncio.Task | None = None
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
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            if self._pending:
                await self.push_frame(frame, direction)
                self._start_fire("Antwort zu Ende", interrupt=False)
                return
        elif isinstance(frame, InterruptionFrame):
            # Unterbrechung von anderer Stelle: nichts mehr zurueckhalten.
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

    def on_word_played(self, text: str) -> bool:
        """Vom PlaybackTracker aufgerufen, wenn laut Zeitstempel ein neues Wort beginnt.

        Beginnt ein neues Wort, ist das vorige fertig: hier liegt eine Wortgrenze.
        Gibt True zurueck, wenn das Wort abgeschnitten wird (gilt dann als nicht gesprochen).
        """
        if self._firing:
            return True
        if self._pending:
            if self._last_word_ended_sentence:
                self._start_fire("Satzende")
                return True
            if self._words_left <= 0:
                self._start_fire("Wortlimit")
                return True
            self._words_left -= 1
        self._last_word_ended_sentence = text.rstrip().endswith(SENTENCE_END)
        return False

    def _arm(self):
        if self._pending or self._firing:
            return
        self._pending = True
        self._words_left = self._max_words
        self._turns_at_arm = self._turns_recorded
        logger.debug(f"{self}: Nikolaj spricht, nieeeki beendet erst den Satz")

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
                # Die Oberflaeche hoert, was gerade erklingt: sie blendet in der naechsten Wortpause
                # aus. Der Server bricht danach ab (Zeitstempel laufen dem Ton voraus).
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
        if self._fire_task:
            await self.cancel_task(self._fire_task)
            self._fire_task = None


class PlaybackTracker(FrameProcessor):
    """Sitzt hinter transport.output() und meldet dem Gate jedes abgespielte Wort."""

    def __init__(self, gate: SoftInterruptGate, **kwargs):
        super().__init__(**kwargs)
        self._gate = gate

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSTextFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._gate.on_word_played(frame.text):
                return  # abgeschnitten -> nicht in den Gespraechsverlauf
        await self.push_frame(frame, direction)
