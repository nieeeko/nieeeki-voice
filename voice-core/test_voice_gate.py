"""Tests fuer voice_gate.py: wer Werkzeuge ausloesen darf, Pruefen je Aeusserung, Einlernen.

Ausfuehren:  uv run python -m unittest -v test_voice_gate.py   (im Ordner voice-core/)

Das Modell ist hier ein Ersatz: positiver Ton = Nikolaj, negativer = fremde Stimme.
"""

import asyncio
import unittest
from unittest import mock

import numpy as np

from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.processors.frameworks.rtvi import RTVIProcessor

import voice_gate
from voice_gate import (
    ENROLL_PROMPT,
    ENROLL_SECS,
    FREMD,
    KURZ,
    NIKOLAJ,
    UNKLAR,
    SpeakerVerifier,
    VoiceGate,
    gate_schemas,
    gated,
    install_tool_gate,
    watch_typed_text,
)
from voiceprint import DIM

RATE = 16000
OWNER = np.eye(DIM, dtype=np.float32)[0]
STRANGER = np.eye(DIM, dtype=np.float32)[1]


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeModel:
    """Ueberwiegend positiver Ton = Nikolaj, sonst fremd (eindeutige Abdruecke)."""

    def __init__(self):
        self.calls = 0

    async def get(self):
        return self

    def embed(self, pcm: bytes, rate: int):
        self.calls += 1
        return OWNER if np.frombuffer(pcm, dtype=np.int16).astype(np.int64).sum() > 0 else STRANGER


def ready_gate(mode="streng", clock=None) -> VoiceGate:
    gate = VoiceGate(mode=mode, clock=clock or Clock())
    gate.set_voiceprint(OWNER)
    gate.model_ok = True
    return gate


class Params:
    def __init__(self):
        self.result_callback = mock.AsyncMock()


class GateRuleTests(unittest.IsolatedAsyncioTestCase):
    def test_einstufung(self):
        gate = VoiceGate()
        self.assertEqual(gate.classify(0.7, 2.0, False), NIKOLAJ)
        self.assertEqual(gate.classify(0.9, 0.5, False), KURZ)  # zu kurz fuer "Nikolaj"
        self.assertEqual(gate.classify(0.4, 0.5, False), KURZ)
        self.assertEqual(gate.classify(0.1, 0.5, False), FREMD)  # auch kurz klar fremd
        self.assertEqual(gate.classify(0.1, 2.0, False), FREMD)
        self.assertEqual(gate.classify(0.1, 2.0, True), UNKLAR)  # nieeeki sprach gleichzeitig
        self.assertEqual(gate.classify(0.4, 2.0, False), UNKLAR)

    def test_vertrauen_haelt_ueber_kurze_folgesaetze_und_laeuft_ab(self):
        clock = Clock()
        gate = ready_gate(clock=clock)
        self.assertIsNotNone(gate.reason())  # noch nichts gehoert
        gate.record(NIKOLAJ)
        gate.record(KURZ)  # kurzes "Ja"
        self.assertIsNone(gate.reason())
        clock.t += gate.trust_secs + 1
        self.assertIsNotNone(gate.reason())

    def test_unklar_mit_genug_sprache_entzieht_das_vertrauen(self):
        gate = ready_gate()
        gate.record(NIKOLAJ)
        gate.record(UNKLAR)  # z. B. Gast mit Wert 0,4
        self.assertEqual(gate.reason(), "Stimme nicht sicher erkannt")

    def test_aeltere_aeusserung_ueberschreibt_getippten_text_nicht(self):
        clock = Clock()
        gate = ready_gate(clock=clock)
        heard = clock()  # Gast spricht ...
        clock.t += 1
        gate.typed()  # ... Nikolaj tippt, waehrend die Pruefung noch laeuft
        gate.record(FREMD, heard)
        self.assertIsNone(gate.reason())
        gate.record(FREMD, clock())  # spricht der Gast danach, zaehlt es
        self.assertEqual(gate.reason(), "fremde Stimme")

    def test_ungueltige_zahlen_aus_env(self):
        with mock.patch.dict("os.environ", {"NIEEEKI_VOICE_TRUST_SECS": "nan", "NIEEEKI_VOICE_OWNER_MIN": "x"}):
            gate = VoiceGate.from_env()
        self.assertEqual(gate.trust_secs, 120.0)
        self.assertEqual(gate.owner_min, 0.5)

    def test_fremde_stimme_sperrt_bis_nikolaj_tippt(self):
        gate = ready_gate()
        gate.record(NIKOLAJ)
        gate.record(FREMD)
        self.assertEqual(gate.reason(), "fremde Stimme")
        gate.typed()
        self.assertIsNone(gate.reason())

    async def test_streng_sperrt_mit_ergebnis_fuer_claude(self):
        gate = ready_gate()
        gate.record(FREMD)
        blocked = await gate.check("programm_oeffnen")
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(blocked["grund"].startswith("Stimmpruefung"))

    async def test_offene_werkzeuge_und_modus_beobachten_sperren_nie(self):
        gate = ready_gate()
        gate.record(FREMD)
        self.assertIsNone(await gate.check("websuche"))
        gate.mode = "beobachten"
        self.assertIsNone(await gate.check("programm_oeffnen"))
        gate.mode = "aus"
        self.assertIsNone(await gate.check("programm_oeffnen"))

    async def test_streng_ohne_abdruck_nur_getippt(self):
        gate = VoiceGate(mode="streng", clock=Clock())
        gate.set_voiceprint(None)
        gate.model_ok = True
        self.assertEqual(gate.status, "fehlt")
        self.assertIn("kein Stimmabdruck", (await gate.check("programm_oeffnen"))["grund"])
        gate.typed()
        self.assertIsNone(await gate.check("programm_oeffnen"))

    async def test_streng_sperrt_solange_nichts_geladen_ist(self):
        gate = VoiceGate(mode="streng")  # Abdruck laedt noch
        self.assertEqual(gate.status, "laedt")
        self.assertIsNotNone(await gate.check("programm_oeffnen"))
        gate.set_voiceprint(None, ok=False)  # Datenbank nicht erreichbar
        self.assertEqual(gate.status, "kaputt")
        self.assertIsNotNone(await gate.check("programm_oeffnen"))

    async def test_wartet_auf_die_laufende_pruefung(self):
        gate = ready_gate()

        async def verify():
            await asyncio.sleep(0.05)
            gate.record(NIKOLAJ)

        gate.track(asyncio.create_task(verify()))
        self.assertIsNone(await gate.check("programm_oeffnen"))

    async def test_zu_lange_pruefung_sperrt_statt_altes_ergebnis(self):
        gate = ready_gate()
        gate.record(NIKOLAJ)
        slow = asyncio.create_task(asyncio.sleep(1))
        gate.track(slow)
        with mock.patch.object(voice_gate, "PENDING_WAIT_SECS", 0.02):
            blocked = await gate.check("programm_oeffnen")
        slow.cancel()
        self.assertIn("dauert zu lange", blocked["grund"])

    def test_unbekannter_modus_ist_streng(self):
        with mock.patch.dict("os.environ", {"NIEEEKI_VOICE_CHECK": "strenge"}):
            self.assertEqual(VoiceGate.from_env().mode, "streng")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(VoiceGate.from_env().mode, "beobachten")


class ToolWrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_gesperrt_ruft_den_handler_nicht_auf(self):
        gate = ready_gate()
        gate.record(FREMD)
        handler = mock.AsyncMock()
        params = Params()
        await gated("programm_oeffnen", handler, gate)(params)
        handler.assert_not_awaited()
        self.assertEqual(params.result_callback.await_args.args[0]["status"], "blocked")

    async def test_handler_fuer_alle_werkzeuge_prueft_den_aufgerufenen_namen(self):
        gate = ready_gate()
        gate.record(FREMD)
        handler = mock.AsyncMock()
        wrapped = gated(None, handler, gate)
        params = Params()
        params.function_name = "programm_oeffnen"
        await wrapped(params)
        handler.assert_not_awaited()
        params.function_name = "aktuelle_zeit"
        await wrapped(params)
        handler.assert_awaited_once()

    async def test_erlaubt_ruft_den_handler_auf(self):
        gate = ready_gate()
        gate.record(NIKOLAJ)
        handler = mock.AsyncMock()
        params = Params()
        await gated("programm_oeffnen", handler, gate)(params)
        handler.assert_awaited_once_with(params)

    def test_offen_und_schon_geprueft_bleiben_unveraendert(self):
        gate = ready_gate()
        handler = mock.AsyncMock()
        self.assertIs(gated("aktuelle_zeit", handler, gate), handler)
        wrapped = gated("merken", handler, gate)
        self.assertIs(gated("merken", wrapped, gate), wrapped)

    def test_tool_options_bleiben_erhalten(self):
        @tool_options(timeout_secs=30)
        async def handler(params):
            pass

        self.assertEqual(gated("bildschirm_ansehen", handler, ready_gate())._pipecat_timeout_secs, 30)

    def test_registrierung_am_llm_laeuft_durch_das_gate(self):
        gate = ready_gate()
        llm = mock.Mock()
        original = llm.register_function
        install_tool_gate(llm, gate)
        handler = mock.AsyncMock()
        llm.register_function("merken", handler, timeout_secs=5)
        name, registered = original.call_args.args
        self.assertEqual(name, "merken")
        self.assertIs(registered._nieeeki_gate, gate)
        self.assertEqual(original.call_args.kwargs, {"timeout_secs": 5})
        llm.register_function("aktuelle_zeit", handler)
        self.assertIs(original.call_args.args[1], handler)

    def test_echtes_llm_registriert_eingepackte_handler(self):
        from pipecat.services.anthropic.llm import AnthropicLLMService

        gate = ready_gate()
        llm = AnthropicLLMService(api_key="test")
        install_tool_gate(llm, gate)
        llm.register_function("merken", mock.AsyncMock())
        self.assertIs(llm._functions["merken"].handler._nieeeki_gate, gate)
        # Host-Werkzeuge kommen mit Handler im Schema; Pipecat gleicht sie bei jedem Kontext ab
        tools = gate_schemas(
            ToolsSchema(standard_tools=[FunctionSchema("programm_oeffnen", "x", {}, [], handler=mock.AsyncMock())]),
            gate,
        )
        llm._sync_registered_tool_handlers(tools)
        first = llm._functions["programm_oeffnen"].handler
        llm._sync_registered_tool_handlers(tools)
        self.assertIs(llm._functions["programm_oeffnen"].handler, first)
        self.assertIs(first._nieeeki_gate, gate)

    def test_host_werkzeuge_werden_genau_einmal_eingepackt(self):
        gate = ready_gate()
        raw = mock.AsyncMock()
        tools = ToolsSchema(standard_tools=[FunctionSchema("programm_oeffnen", "x", {}, [], handler=raw)])
        schema = gate_schemas(tools, gate).standard_tools[0]
        self.assertIsNot(schema.handler, raw)
        # Pipecat registriert bei jedem Kontext neu: derselbe Handler, kein neues Einpacken
        self.assertIs(gated(schema.name, schema.handler, gate), schema.handler)


class TypedTextTests(unittest.IsolatedAsyncioTestCase):
    def test_pipecat_hat_den_haken_noch(self):
        self.assertTrue(hasattr(RTVIProcessor, "_handle_send_text"))

    async def test_getippter_text_zaehlt_wie_nikolaj(self):
        gate = ready_gate()
        gate.record(FREMD)
        rtvi = mock.Mock()
        original = mock.AsyncMock()
        rtvi._handle_send_text = original
        self.assertTrue(watch_typed_text(rtvi, gate))
        await rtvi._handle_send_text("data")
        original.assert_awaited_once_with("data")
        self.assertIsNone(gate.reason())


def audio(value: int, secs: float = 0.02) -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=np.full(int(RATE * secs), value, dtype=np.int16).tobytes(), sample_rate=RATE, num_channels=1)


class VerifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.gate = ready_gate(mode="streng", clock=self.clock)
        self.model = FakeModel()
        self.save = mock.AsyncMock(return_value=True)
        self.say = mock.AsyncMock()
        self.v = SpeakerVerifier(self.gate, self.model, save=self.save, say=self.say, clock=self.clock)
        self.sent = []
        self.v.push_frame = mock.AsyncMock(side_effect=lambda f, *a: self.sent.append(getattr(f, "data", None)))

    async def speak(self, value: int, secs: float):
        """VAD-Beginn, secs Sekunden Ton (20-ms-Pakete), VAD-Ende mit 0,2 s Stille."""
        self.v.on_speech_start(self.clock.t + 0.2, 0.2)
        for _ in range(int(secs / 0.02)):
            self.clock.t += 0.02
            await self.v.on_audio(audio(value))
        for _ in range(10):
            self.clock.t += 0.02
            await self.v.on_audio(audio(0))
        self.v.on_speech_stop(self.clock.t, 0.2)
        await self.gate._pending

    def messages(self, kind):
        return [m for m in self.sent if m and m.get("type") == kind]

    async def test_nikolaj_wird_erkannt_und_gemeldet(self):
        await self.speak(8000, 2.0)
        self.assertIsNone(self.gate.reason())
        self.assertEqual(self.messages("stimme")[-1]["ergebnis"], NIKOLAJ)

    async def test_fremde_stimme_sperrt(self):
        await self.speak(8000, 2.0)
        self.v.on_turn_end()  # Nikolajs Zug ist vorbei, jetzt spricht jemand anderes
        await self.speak(-8000, 2.0)
        self.assertEqual(self.gate.reason(), "fremde Stimme")

    async def test_fremd_waehrend_nieeeki_spricht_ist_unklar_und_sperrt(self):
        await self.speak(8000, 2.0)
        self.v.on_bot_speaking(True)
        await self.speak(-8000, 2.0)
        self.assertEqual(self.gate.reason(), "Stimme nicht sicher erkannt")
        self.assertEqual(self.messages("stimme")[-1]["ergebnis"], UNKLAR)

    async def test_kurzes_ja_nach_nikolaj_bleibt_erlaubt(self):
        await self.speak(8000, 2.0)
        self.v.on_turn_end()
        await self.speak(8000, 0.5)
        self.assertIsNone(self.gate.reason())
        self.assertEqual(self.messages("stimme")[-1]["ergebnis"], UNKLAR)  # Oberflaeche: unklar

    async def test_lange_pause_trennt_die_aeusserungen(self):
        await self.speak(8000, 2.0)
        self.clock.t += voice_gate.TURN_GAP_SECS + 1  # kein Zugende gemeldet, aber lange still
        await self.speak(-8000, 2.0)
        self.assertEqual(self.gate.reason(), "fremde Stimme")

    async def test_aeusserung_bleibt_hoechstens_8_sekunden(self):
        for _ in range(6):
            await self.speak(8000, 2.0)
        self.assertLessEqual(len(self.v._turn), int(voice_gate.MAX_VERIFY_SECS * RATE) * 2)

    async def test_fehlgeschlagene_pruefung_entzieht_das_vertrauen(self):
        await self.speak(8000, 2.0)
        self.v.on_turn_end()
        self.model.embed = mock.Mock(side_effect=RuntimeError("kaputt"))
        await self.speak(-8000, 2.0)
        self.assertEqual(self.gate.reason(), "Stimme nicht sicher erkannt")

    async def test_ohne_abdruck_wird_nicht_gerechnet(self):
        self.gate.set_voiceprint(None)
        await self.speak(8000, 2.0)
        self.assertEqual(self.model.calls, 0)

    async def enroll_audio(self, secs: float):
        for _ in range(int(secs / 0.02)):
            self.clock.t += 0.02
            await self.v.on_audio(audio(8000))

    async def test_einlernen_nach_ihrer_anleitung(self):
        self.gate.set_voiceprint(None)  # das erste Mal
        await self.v.start_enrollment()
        self.say.assert_awaited_once_with(ENROLL_PROMPT)
        # Waehrend sie die Anleitung spricht: Stille fuer die Spracherkennung, keine Aufnahme
        self.v.on_bot_speaking(True)
        frame = audio(8000)
        await self.v.on_audio(frame)
        self.assertEqual(frame.audio, bytes(len(frame.audio)))
        self.v.on_bot_speaking(False)
        await self.enroll_audio(ENROLL_SECS + 0.1)
        await asyncio.gather(*self.v._tasks)
        self.save.assert_awaited_once()
        self.assertEqual(self.messages("stimmabdruck")[-1]["status"], "fertig")
        self.assertIsNone(self.gate.reason())  # er hat gerade selbst gesprochen
        self.say.assert_awaited_with(voice_gate.ENROLL_DONE)
        # danach hoert die Spracherkennung wieder mit
        frame = audio(8000)
        await self.v.on_audio(frame)
        self.assertNotEqual(frame.audio, bytes(len(frame.audio)))

    async def test_neu_einlernen_streng_nur_wenn_er_gerade_erkannt_ist(self):
        await self.v.start_enrollment()  # Abdruck da, aber noch nichts gehoert
        self.assertEqual(self.messages("stimmabdruck")[-1]["status"], "fehler")
        frame = audio(8000)
        await self.v.on_audio(frame)
        self.assertNotEqual(frame.audio, bytes(len(frame.audio)))  # keine Aufnahme gestartet
        await self.speak(8000, 2.0)
        await self.v.start_enrollment()
        self.assertEqual(self.messages("stimmabdruck")[-1]["status"], "start")

    async def test_abbrechen_waehrend_er_rechnet_speichert_nichts(self):
        self.gate.set_voiceprint(None)
        await self.v.start_enrollment()
        self.v.on_bot_speaking(True)
        self.v.on_bot_speaking(False)
        await self.enroll_audio(ENROLL_SECS + 0.1)
        await self.v.cancel_enrollment()
        await asyncio.gather(*self.v._tasks)
        self.save.assert_not_awaited()
        self.assertEqual(self.messages("stimmabdruck")[-1]["status"], "abgebrochen")

    async def test_einlernen_bricht_ab_wenn_er_nichts_sagt(self):
        self.gate.set_voiceprint(None)
        await self.v.start_enrollment()
        self.v.on_bot_speaking(True)
        self.v.on_bot_speaking(False)
        for _ in range(int(voice_gate.ENROLL_MAX_SECS / 0.5) + 2):
            self.clock.t += 0.5
            await self.v.on_audio(audio(0))
        self.assertEqual(self.messages("stimmabdruck")[-1]["status"], "fehler")
        self.save.assert_not_awaited()

    async def test_einlernen_endet_auch_wenn_ihr_ende_nie_gemeldet_wird(self):
        self.gate.set_voiceprint(None)
        await self.v.start_enrollment()
        self.v.on_bot_speaking(True)  # BotStoppedSpeaking kommt nie
        for _ in range(int(voice_gate.ENROLL_MAX_SECS / 0.5) + 2):
            self.clock.t += 0.5
            await self.v.on_audio(audio(8000))
        self.assertEqual(self.messages("stimmabdruck")[-1]["status"], "fehler")
        frame = audio(8000)
        await self.v.on_audio(frame)
        self.assertNotEqual(frame.audio, bytes(len(frame.audio)))  # sie hoert wieder zu

    async def test_einlernen_abbrechen(self):
        self.gate.set_voiceprint(None)
        await self.v.start_enrollment()
        await self.v.cancel_enrollment()
        self.assertEqual(self.messages("stimmabdruck")[-1]["status"], "abgebrochen")
        frame = audio(8000)
        await self.v.on_audio(frame)
        self.assertNotEqual(frame.audio, bytes(len(frame.audio)))


if __name__ == "__main__":
    unittest.main()
