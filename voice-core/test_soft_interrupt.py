"""Tests fuer soft_interrupt.py: wann nieeeki innehaelt, weiterspricht oder sich unterbrechen laesst.

Ausfuehren:  uv run python -m unittest -v test_soft_interrupt.py   (im Ordner voice-core/)
21.10.: Ein einziges Wortstueck (" Al", " Du") unterbrach sie, und dann sprach sie noch bis zum
Satzende oder drei Woerter weiter (bis 2,5 s ueber Nikolaj hinweg).
22.10.: Ein einzelnes Wort laesst sie an der Wortgrenze innehalten; kommt nichts nach, spricht sie
genau dort weiter (Fortsetzen nach falscher Unterbrechung).
"""

import asyncio
import contextlib
import unittest
from unittest import mock

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from playback import AudioHold
from soft_interrupt import (
    CUT,
    HOLD,
    PLAY,
    MeaningfulWordsUserTurnStartStrategy,
    PlaybackTracker,
    SoftInterruptGate,
    asks_so_far,
    is_answer,
    is_stop_request,
    meaningful_word_count,
)


def interim(text):
    return InterimTranscriptionFrame(text=text, user_id="u", timestamp="")


def word(text, pts):
    frame = TTSTextFrame(text=text, aggregated_by="word")
    frame.pts = pts
    return frame


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class WortTests(unittest.TestCase):
    def test_rueckmeldungen_zaehlen_nicht(self):
        self.assertEqual(meaningful_word_count("mhm ja genau okay"), 0)
        self.assertEqual(meaningful_word_count(" Al"), 1)
        self.assertEqual(meaningful_word_count("Leg das Repository an"), 4)

    def test_sofort_woerter(self):
        for text in ["Stopp!", "warte mal", "Nein.", "Moment", "halt"]:
            with self.subTest(text=text):
                self.assertTrue(is_stop_request(text))
        for text in ["ja genau", "Leg an", "mhm", ""]:
            with self.subTest(text=text):
                self.assertFalse(is_stop_request(text))

    def test_antwort_auf_ihre_frage(self):
        for text in ["Ja", " ja genau", "Okay.", "Klar", "mach"]:
            with self.subTest(text=text):
                self.assertTrue(is_answer(text))
        for text in ["mhm", "aha", "Ja, aber", "Ja?", "Bitte?", "Nein", "Leg das an", "J", ""]:
            with self.subTest(text=text):
                self.assertFalse(is_answer(text))

    def test_fragebeginn_ohne_fragezeichen(self):
        for text in ["Soll ich das tun", "Darf ich", "Moechtest du", "Passt das?"]:
            with self.subTest(text=text):
                self.assertTrue(asks_so_far(text))
        for text in ["Ich kann das", "Erledigt.", ""]:
            with self.subTest(text=text):
                self.assertFalse(asks_so_far(text))


class StartTests(unittest.IsolatedAsyncioTestCase):
    async def strategy(self, on_partial=None):
        s = MeaningfulWordsUserTurnStartStrategy(
            min_words=2, enable_interruptions=False, on_partial=on_partial
        )
        await s.process_frame(BotStartedSpeakingFrame())
        return s

    async def triggered(self, text):
        s = await self.strategy()
        with mock.patch.object(s, "trigger_user_turn_started") as started, \
             mock.patch.object(s, "trigger_reset_aggregation"):
            await s.process_frame(interim(text))
        return started.await_count > 0

    async def test_ein_wortstueck_unterbricht_nicht(self):
        for text in [" Al", " Du", " Leg", "mhm", "ja genau", "okay okay"]:
            with self.subTest(text=text):
                self.assertFalse(await self.triggered(text))

    async def test_zwei_echte_woerter_oder_stopp(self):
        for text in [" Leg das", "Du hast", "Stopp", " warte", "Nein"]:
            with self.subTest(text=text):
                self.assertTrue(await self.triggered(text))

    async def test_ein_echtes_wort_meldet_einen_ansatz(self):
        partial = mock.Mock()
        s = await self.strategy(on_partial=partial)
        with mock.patch.object(s, "trigger_user_turn_started"), \
             mock.patch.object(s, "trigger_reset_aggregation"):
            await s.process_frame(interim("mhm ja genau"))  # Zuhoeren: haelt sie nicht an
            await s.process_frame(interim(" Danke"))
            await s.process_frame(interim(" Leg das"))  # zwei Woerter: Unterbrechung, kein Ansatz
        partial.assert_called_once_with(" Danke")


class FrageTests(unittest.IsolatedAsyncioTestCase):
    """Ein "Ja" waehrend ihrer Frage ist die Antwort und kein Rueckmeldelaut (04.11.2026)."""

    async def triggered(self, text, *, asking, speaking=True):
        s = MeaningfulWordsUserTurnStartStrategy(
            min_words=2, enable_interruptions=False, asking=lambda: asking
        )
        if speaking:
            await s.process_frame(BotStartedSpeakingFrame())
        with mock.patch.object(s, "trigger_user_turn_started") as started, \
             mock.patch.object(s, "trigger_reset_aggregation") as reset:
            await s.process_frame(interim(text))
        return started.await_count > 0, reset.await_count > 0

    async def test_ja_waehrend_ihrer_frage_ist_die_antwort(self):
        for text in ["Ja", " ja genau", "Okay", "Klar", "mach"]:
            with self.subTest(text=text):
                started, reset = await self.triggered(text, asking=True)
                self.assertTrue(started)
                self.assertFalse(reset)  # das Transkript bleibt erhalten

    async def test_ohne_frage_bleibt_ja_ein_rueckmeldelaut(self):
        for text in ["Ja", "ja genau", "Okay"]:
            with self.subTest(text=text):
                self.assertEqual(await self.triggered(text, asking=False), (False, True))

    async def test_zuhoeren_bleibt_auch_bei_einer_frage_zuhoeren(self):
        for text in ["mhm", "aha", "hmm", " J"]:
            with self.subTest(text=text):
                self.assertEqual(await self.triggered(text, asking=True), (False, True))

    async def test_ohne_sprechende_nieeeki_greift_die_basisklasse(self):
        # Spricht sie nicht und laeuft kein Werkzeug, startet wie bisher jedes Wort den Satz ("asking" egal).
        started, _ = await self.triggered("Ja", asking=True, speaking=False)
        self.assertTrue(started)

    async def test_waehrend_werkzeug_bleibt_okay_zuhoeren(self):
        # Review 04.11.: Der Guard "sie spricht" schuetzt genau diesen Fall. Ihre letzte Frage steht noch
        # als Frage da, sie spricht aber nicht, ein Werkzeug laeuft: "okay" darf es nicht abbrechen.
        s = MeaningfulWordsUserTurnStartStrategy(
            min_words=2, enable_interruptions=False, asking=lambda: True
        )
        await s.process_frame(
            FunctionCallInProgressFrame(function_name="git_klonen", tool_call_id="t1", arguments={})
        )
        with mock.patch.object(s, "trigger_user_turn_started") as started, \
             mock.patch.object(s, "trigger_reset_aggregation"):
            await s.process_frame(interim("okay"))
        self.assertEqual(started.await_count, 0)

    async def test_aber_und_nachfragen_sind_keine_antwort(self):
        for text in ["Ja, aber", "Ja?", "Bitte?"]:  # "lieber nicht" (zwei echte Woerter) unterbricht ohnehin
            with self.subTest(text=text):
                started, _ = await self.triggered(text, asking=True)
                self.assertFalse(started)

    async def test_ohne_asking_verhalten_wie_bisher(self):
        s = MeaningfulWordsUserTurnStartStrategy(min_words=2, enable_interruptions=False)
        await s.process_frame(BotStartedSpeakingFrame())
        with mock.patch.object(s, "trigger_user_turn_started") as started, \
             mock.patch.object(s, "trigger_reset_aggregation"):
            await s.process_frame(interim("Ja"))
        self.assertEqual(started.await_count, 0)


class WerkzeugTests(unittest.IsolatedAsyncioTestCase):
    """Ein "okay" auf ihr "Moment." darf das laufende Werkzeug nicht abbrechen."""

    async def asyncSetUp(self):
        self.clock = Clock()
        self.partial = mock.Mock()
        self.s = MeaningfulWordsUserTurnStartStrategy(
            min_words=2, enable_interruptions=False, on_partial=self.partial, monotonic=self.clock
        )
        await self.s.process_frame(
            FunctionCallInProgressFrame(function_name="git_klonen", tool_call_id="t1", arguments={})
        )

    async def triggered(self, text):
        with mock.patch.object(self.s, "trigger_user_turn_started") as started, \
             mock.patch.object(self.s, "trigger_reset_aggregation"):
            await self.s.process_frame(interim(text))
        return started.await_count > 0

    async def test_rueckmeldung_waehrend_werkzeug_startet_keinen_satz(self):
        for text in ["okay", "ja", "mhm", " Danke"]:
            with self.subTest(text=text):
                self.assertFalse(await self.triggered(text))
        self.partial.assert_not_called()  # kein Ton, also nichts zum Innehalten

    async def test_echter_einwurf_oder_abbrechen_schon(self):
        for text in ["Nimm lieber Python", "abbrechen"]:
            with self.subTest(text=text):
                self.assertTrue(await self.triggered(text))

    async def test_kurz_nach_dem_werkzeug_noch_geschuetzt_dann_normal(self):
        await self.s.process_frame(
            FunctionCallResultFrame(function_name="git_klonen", tool_call_id="t1", arguments={}, result="ok")
        )
        self.assertFalse(await self.triggered("okay"))  # ihre Antwort kommt gleich
        self.clock.t += 5
        self.assertTrue(await self.triggered("okay"))  # danach startet jedes Wort wieder den Satz


class GrenzeTests(unittest.TestCase):
    def test_ohne_innehalten_stoppt_an_der_naechsten_wortgrenze(self):
        gate = SoftInterruptGate(playback_lag_secs=0)  # Standard: max_words=0
        gate.on_word("Ich")
        with mock.patch.object(gate, "_start_fire") as fire:
            gate._arm()
            cut = gate.on_word_played("lege")  # das naechste Wort beginnt: hier ist Schluss
        fire.assert_called_once()
        self.assertTrue(cut)


class FrageErkennenTests(unittest.IsolatedAsyncioTestCase):
    """SoftInterruptGate.asking(): fragt sie gerade (nach den Woertern, die schon erklungen sind)?"""

    def speak(self, gate, text):
        for w in text.split():
            gate.on_word(w)

    def test_mitten_in_der_frage(self):
        gate = SoftInterruptGate()
        self.speak(gate, "Soll ich Chrome oeffnen")  # das Fragezeichen fehlt noch
        self.assertTrue(gate.asking())

    def test_frage_zu_ende_gesprochen(self):
        gate = SoftInterruptGate()
        self.speak(gate, "Ich habe das gefunden. Passt das?")
        self.assertTrue(gate.asking())

    def test_nach_der_frage_kommt_eine_aussage(self):
        gate = SoftInterruptGate()
        self.speak(gate, "Soll ich das so machen? Das dauert nur kurz")
        self.assertFalse(gate.asking())

    def test_aussage_ist_keine_frage(self):
        gate = SoftInterruptGate()
        self.speak(gate, "Ich habe Chrome geoeffnet.")
        self.assertFalse(gate.asking())
        gate2 = SoftInterruptGate()
        self.speak(gate2, "Ich oeffne gleich")
        self.assertFalse(gate2.asking())

    def test_vor_dem_ersten_wort_keine_frage(self):
        self.assertFalse(SoftInterruptGate().asking())

    def test_fragebeginn_mitten_im_satz(self):
        # Review 04.11.: nicht nur "Soll ich" - auch "Darf ich", "Moechtest du", "Kann ich".
        for satz in ["Darf ich Chrome oeffnen", "Moechtest du das so", "Möchtest du", "Kann ich dir helfen",
                     "Okay, soll ich das tun"]:
            with self.subTest(satz=satz):
                gate = SoftInterruptGate()
                self.speak(gate, satz)
                self.assertTrue(gate.asking())

    def test_aussage_mit_modalverb_ist_keine_frage(self):
        for satz in ["Ich kann das oeffnen", "Du darfst das gern tun", "Das moechte ich nicht"]:
            with self.subTest(satz=satz):
                gate = SoftInterruptGate()
                self.speak(gate, satz)
                self.assertFalse(gate.asking())

    def test_fragezeichen_mit_ausrufezeichen_und_anfuehrungszeichen(self):
        for satz in ["Passt das?!", 'Er fragte "Passt das?"', "Passt das?”"]:
            with self.subTest(satz=satz):
                gate = SoftInterruptGate()
                self.speak(gate, satz)
                self.assertTrue(gate.asking())
                self.speak(gate, "Gut")  # nach der Frage kommt eine Aussage: Frage vorbei
                self.assertFalse(gate.asking())

    def test_wartende_woerter_zaehlen_nicht(self):
        # Review 04.11.: Haelt sie inne, wartet ihr Wort (HOLD) und ist noch nicht erklungen. Erst wenn es
        # nach dem Weitersprechen wirklich gespielt wird, gehoert es zur Frage.
        gate = SoftInterruptGate()
        self.speak(gate, "Ich habe das gefunden.")
        gate._pause("Test")
        self.assertEqual(gate.on_word("Passt"), HOLD)
        self.assertEqual(gate.on_word("das?"), HOLD)
        self.assertFalse(gate.asking())
        gate._resume("Test")
        self.speak(gate, "Passt das?")
        self.assertTrue(gate.asking())

    async def test_verdrahtung_gate_und_strategie(self):
        # bot.py: asking=soft_gate.asking. Mit echtem Gate: waehrend der Frage startet "Ja" den Satz,
        # nach der Frage (eine Aussage folgt) nicht mehr.
        gate = SoftInterruptGate()
        self.speak(gate, "Soll ich Chrome")
        self.assertTrue(await self.ja_startet(gate))
        self.speak(gate, "oeffnen? Das dauert nur kurz")
        self.assertFalse(await self.ja_startet(gate))

    async def ja_startet(self, gate):
        s = MeaningfulWordsUserTurnStartStrategy(
            min_words=2, enable_interruptions=False, asking=gate.asking
        )
        await s.process_frame(BotStartedSpeakingFrame())
        with mock.patch.object(s, "trigger_user_turn_started") as started, \
             mock.patch.object(s, "trigger_reset_aggregation"):
            await s.process_frame(interim("Ja"))
        return started.await_count > 0

    async def test_neue_antwort_beginnt_ohne_frage(self):
        gate = SoftInterruptGate()
        self.speak(gate, "Passt das?")
        self.assertTrue(gate.asking())
        # Der Prozessor ist nicht gestartet: Basisklasse und Weitergabe ersetzen, getestet wird nur das Gate.
        with mock.patch.object(FrameProcessor, "process_frame", new=mock.AsyncMock()), \
             mock.patch.object(gate, "push_frame", new=mock.AsyncMock()):
            await gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        self.assertFalse(gate.asking())


class InnehaltenTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.hold = AudioHold()
        self.gate = SoftInterruptGate(
            hold=self.hold,
            resume_after_secs=0.7,
            max_pause_secs=2.5,
            boundary_wait_secs=0.4,
            monotonic=self.clock,
        )
        self.gate._bot_speaking = True
        patches = [
            mock.patch.object(self.gate, "_ensure_watch"),
            mock.patch.object(self.gate, "_start_fire"),
        ]
        self.watch, self.fire = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def pause_on_hint(self, text=" Danke"):
        self.assertEqual(self.gate.on_word("Ich"), PLAY)
        self.gate.hint(text)
        self.assertFalse(self.hold.paused)  # erst an der naechsten Wortgrenze
        self.assertEqual(self.gate.on_word("lege"), HOLD)
        self.assertTrue(self.hold.paused)

    def test_ein_wort_haelt_an_der_wortgrenze_inne(self):
        self.pause_on_hint()
        self.assertEqual(self.gate.on_word("lege"), HOLD)  # wartet, solange sie innehaelt
        self.fire.assert_not_called()

    def test_kommt_nichts_nach_spricht_sie_dort_weiter(self):
        self.pause_on_hint()
        self.clock.t += 0.5
        self.assertTrue(self.gate.check_timers())
        self.assertTrue(self.hold.paused)
        self.clock.t += 0.3
        self.assertFalse(self.gate.check_timers())
        self.assertFalse(self.hold.paused)
        self.assertEqual(self.gate.on_word("lege"), PLAY)  # dasselbe Wort, jetzt hoerbar
        self.gate.hint(" Danke.")  # derselbe Ansatz noch einmal: kein zweites Innehalten
        self.assertEqual(self.gate.on_word("das"), PLAY)

    def test_spricht_er_noch_wartet_sie_hoechstens_max_pause(self):
        self.pause_on_hint()
        self.gate._user_speaking = True
        self.clock.t += 1.5
        self.assertTrue(self.gate.check_timers())
        self.assertTrue(self.hold.paused)
        self.clock.t += 1.1
        self.assertFalse(self.gate.check_timers())
        self.assertFalse(self.hold.paused)

    def test_zweites_wort_bricht_sofort_ab(self):
        self.pause_on_hint()
        self.gate._arm()
        self.fire.assert_called_once_with("Einwurf bestaetigt")

    def test_einwurf_ohne_ansatz_stoppt_an_der_wortgrenze(self):
        self.gate.on_word("Ich")
        self.gate._arm()
        self.fire.assert_not_called()
        self.assertEqual(self.gate.on_word("lege"), CUT)
        self.assertTrue(self.hold.paused)  # nichts mehr hinaus, bis der Abbruch den Rest verwirft
        self.fire.assert_called_once_with("Wortgrenze")

    def test_ohne_wortgrenze_haelt_sie_trotzdem_inne(self):
        self.gate.hint(" Danke")  # z. B. zwischen zwei Saetzen, kein Wort in Sicht
        self.clock.t += 0.45
        self.assertTrue(self.gate.check_timers())
        self.assertTrue(self.hold.paused)

    def test_rueckmeldung_ohne_sprechen_kein_innehalten(self):
        self.gate._bot_speaking = False
        self.gate.hint(" Danke")
        self.assertEqual(self.gate.on_word("Ich"), PLAY)
        self.assertFalse(self.hold.paused)


class FakeHold:
    def __init__(self):
        self.paused = False
        self.played = None
        self.cuts = 0

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def cut(self):
        self.cuts += 1
        self.paused = False

    def played_until_ns(self):
        return self.played

    async def wait_progress(self, timeout):
        await asyncio.sleep(0.005)


class TrackerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        p = mock.patch.object(FrameProcessor, "process_frame", new=mock.AsyncMock())
        p.start()
        self.addCleanup(p.stop)
        self.hold = FakeHold()
        self.gate = SoftInterruptGate(hold=self.hold)
        self.tracker = PlaybackTracker(self.gate, self.hold)
        self.pushed = []

        async def push(frame, direction=FrameDirection.DOWNSTREAM):
            self.pushed.append(frame)

        async def cancel(task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        self.tracker.push_frame = push
        self.tracker.create_task = lambda coro, name=None: asyncio.get_running_loop().create_task(coro)
        self.tracker.cancel_task = cancel

    async def send(self, frame):
        await self.tracker.process_frame(frame, FrameDirection.DOWNSTREAM)

    async def test_wort_kommt_erst_wenn_sein_ton_hinausgeht(self):
        first, end = word("Ich", 1_000_000_000), LLMFullResponseEndFrame()
        await self.send(first)
        await self.send(end)  # wartet hinter dem Wort, Reihenfolge bleibt
        await asyncio.sleep(0.03)
        self.assertEqual(self.pushed, [])
        self.hold.played = 1_000_000_000
        await asyncio.sleep(0.03)
        self.assertEqual(self.pushed, [first, end])

    async def test_abbruch_verwirft_nicht_gespielte_woerter(self):
        await self.send(word("noch", 2_000_000_000))
        interruption = InterruptionFrame()
        await self.send(interruption)
        self.hold.played = 3_000_000_000
        await asyncio.sleep(0.03)
        self.assertEqual(self.pushed, [interruption])
        self.assertEqual(self.hold.cuts, 1)

    async def test_ende_der_antwort_gibt_alles_frei(self):
        last = word("fertig.", 2_000_000_000)
        await self.send(last)
        stopped = BotStoppedSpeakingFrame()
        await self.send(stopped)
        self.assertEqual(self.pushed, [last, stopped])


if __name__ == "__main__":
    unittest.main()
