"""SSE `delta` streaming — incremental assistant text between status and result.

Covers the three layers added for token streaming:
  1. AssistantMessageStreamExtractor — peels assistant_message text out of raw
     streamed JSON tokens (escapes/keys split across chunks, cap, no-key case).
  2. OpenAI provider streaming — llm_json(on_delta=…) forwards fragments and still
     returns the parsed JSON; an aborted stream falls back to the retry ladder and
     still yields a valid result.
  3. Synthesizer gating + SSE endpoint — deltas only on the plain conversational
     lana path; the terminal result frame is unchanged and matches the deltas;
     template/tool paths emit no deltas; a failing turn still emits an error frame.
"""

import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as main
from app.models import SendMessageResponse
from app.orchestrator import llm as llm_mod
from app.orchestrator.stream_extract import AssistantMessageStreamExtractor
from app.turn_timing import TurnTimer


def _chunked(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


class ExtractorTests(unittest.TestCase):
    def _run(self, raw: str, size: int) -> str:
        ex = AssistantMessageStreamExtractor()
        return "".join(ex.feed(c) for c in _chunked(raw, size))

    def test_single_chunk(self) -> None:
        raw = '{"assistant_message": "Hey there!", "status": "continue"}'
        self.assertEqual(self._run(raw, len(raw)), "Hey there!")

    def test_char_by_char_with_escapes(self) -> None:
        raw = '{"assistant_message": "Hi \\"mom\\" — caf\\u00e9 \\u2764 ok", "ui": {}}'
        self.assertEqual(self._run(raw, 1), 'Hi "mom" — café ❤ ok')

    def test_surrogate_pair_split_across_chunks(self) -> None:
        raw = '{"assistant_message":"see \\ud83d\\ude00 you"}'
        for size in (1, 3, 7):
            self.assertEqual(self._run(raw, size), "see \U0001f600 you")

    def test_key_not_first(self) -> None:
        raw = '{"status":"continue","assistant_message":"Hello!","ui":null}'
        self.assertEqual(self._run(raw, 4), "Hello!")

    def test_no_assistant_message_key(self) -> None:
        raw = '{"status":"continue","message":"nope"}'
        self.assertEqual(self._run(raw, 3), "")

    def test_stops_at_closing_quote(self) -> None:
        ex = AssistantMessageStreamExtractor()
        out = ex.feed('{"assistant_message":"done."} trailing garbage')
        self.assertEqual(out, "done.")
        self.assertTrue(ex.done)
        self.assertEqual(ex.feed(' more {"assistant_message":"other"}'), "")

    def test_caps_at_max_chars(self) -> None:
        ex = AssistantMessageStreamExtractor(max_chars=5)
        raw = '{"assistant_message":"abcdefghij"}'
        out = "".join(ex.feed(c) for c in _chunked(raw, 2))
        self.assertEqual(out, "abcde")


class _FakeDelta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeStreamChoice:
    def __init__(self, content: str | None) -> None:
        self.delta = _FakeDelta(content)


class _FakeChunk:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeStreamChoice(content)]


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    """Scripted chat.completions.create — streaming and blocking calls recorded."""

    def __init__(self, stream_chunks, blocking_text: str, abort_after: int | None = None):
        self._stream_chunks = stream_chunks
        self._blocking_text = blocking_text
        self._abort_after = abort_after
        self.calls: list[dict] = []

    def create(self, **params):
        self.calls.append(params)
        if not params.get("stream"):
            return _FakeResponse(self._blocking_text)
        chunks = self._stream_chunks
        abort_after = self._abort_after

        def _iter():
            for i, c in enumerate(chunks):
                if abort_after is not None and i >= abort_after:
                    raise RuntimeError("connection reset mid-stream")
                yield _FakeChunk(c)

        return _iter()


class _FakeClient:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.chat = type("_Chat", (), {"completions": completions})()


class OpenAiStreamingTests(unittest.TestCase):
    _JSON = '{"assistant_message": "Warm hello from Lana.", "status": "continue", "ui": {"bucket": null}}'

    def _llm_json(self, completions: _FakeCompletions, on_delta):
        with patch.object(llm_mod, "provider", return_value="openai"), patch.object(
            llm_mod, "_openai_client", return_value=_FakeClient(completions)
        ):
            return llm_mod.llm_json(
                model="gpt-4o-mini",
                system="sys",
                user_payload="payload",
                on_delta=on_delta,
            )

    def test_streams_deltas_and_parses_same_result(self) -> None:
        completions = _FakeCompletions(_chunked(self._JSON, 6), self._JSON)
        deltas: list[str] = []
        data = self._llm_json(completions, deltas.append)
        self.assertEqual(data["assistant_message"], "Warm hello from Lana.")
        self.assertEqual("".join(deltas), "Warm hello from Lana.")
        self.assertGreater(len(deltas), 1)  # flushed per chunk, not one blob
        self.assertTrue(completions.calls[0].get("stream"))

    def test_no_on_delta_stays_blocking(self) -> None:
        completions = _FakeCompletions(_chunked(self._JSON, 6), self._JSON)
        data = self._llm_json(completions, None)
        self.assertEqual(data["assistant_message"], "Warm hello from Lana.")
        self.assertEqual(len(completions.calls), 1)
        self.assertNotIn("stream", completions.calls[0])

    def test_aborted_stream_retries_blocking_and_still_yields_result(self) -> None:
        # Stream dies after 2 chunks → partial JSON → retry ladder re-asks without
        # streaming and the turn still resolves with valid parsed content.
        completions = _FakeCompletions(
            _chunked(self._JSON, 6),
            '{"assistant_message": "Recovered fine.", "status": "continue"}',
            abort_after=2,
        )
        deltas: list[str] = []
        data = self._llm_json(completions, deltas.append)
        self.assertEqual(data["assistant_message"], "Recovered fine.")
        self.assertGreaterEqual(len(completions.calls), 2)
        self.assertNotIn("stream", completions.calls[1])  # retry does not re-stream


class SynthesizerGatingTests(unittest.TestCase):
    _RAW = {"assistant_message": "Hi neighbor.", "status": "continue", "ui": {}}

    def _synthesize(self, *, tool_result, timer):
        from app.orchestrator.synthesizer import synthesize_turn

        with patch(
            "app.orchestrator.synthesizer.llm_json", return_value=dict(self._RAW)
        ) as mock_llm, patch(
            "app.orchestrator.synthesizer.build_system_prompt", return_value="sys"
        ), patch(
            "app.orchestrator.synthesizer.load_prompt", return_value="synth"
        ):
            reply, *_ = synthesize_turn(
                purpose="lana",
                utterance="hello",
                routing={"outcome": "R", "intent_class": "companionship"},
                core_block={},
                history=[],
                tool_result=tool_result,
                session_ctx={},
                timer=timer,
            )
        return reply, mock_llm

    def test_plain_chat_turn_streams(self) -> None:
        deltas: list[str] = []
        timer = TurnTimer()
        timer.set_delta_emitter(deltas.append)
        reply, mock_llm = self._synthesize(tool_result=None, timer=timer)
        on_delta = mock_llm.call_args.kwargs["on_delta"]
        self.assertIsNotNone(on_delta)
        on_delta("Hi ")
        on_delta("neighbor.")
        self.assertEqual(deltas, ["Hi ", "neighbor."])
        self.assertEqual(reply, "Hi neighbor.")

    def test_tool_turn_does_not_stream(self) -> None:
        timer = TurnTimer()
        timer.set_delta_emitter(lambda _t: None)
        tool_result = {
            "tool": "find_peers",
            "peer_matches": [{"matching_peer_label": "Mom of toddlers"}],
            "summary": "Found 1 neighbor like you.",
        }
        reply, mock_llm = self._synthesize(tool_result=tool_result, timer=timer)
        self.assertIsNone(mock_llm.call_args.kwargs["on_delta"])
        # Result content unchanged: backend summary still wins over synth text.
        self.assertEqual(reply, "Found 1 neighbor like you.")

    def test_blocking_endpoint_turn_does_not_stream(self) -> None:
        reply, mock_llm = self._synthesize(tool_result=None, timer=TurnTimer())
        self.assertIsNone(mock_llm.call_args.kwargs["on_delta"])
        self.assertEqual(reply, "Hi neighbor.")


def _sse_events(body: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


class StreamEndpointTests(unittest.TestCase):
    def _post(self):
        client = TestClient(main.app)
        return client.post(
            "/lana/sessions/sess-1/messages/stream",
            json={"message": "hi lana"},
            headers={"Authorization": "Bearer test"},
        )

    def test_deltas_between_status_and_identical_result(self) -> None:
        def fake_run(session_id, body, background_tasks, authorization, emit=None, emit_delta=None):
            emit("Reading your message…")
            emit("Composing…")
            for piece in ("Hey ", "there — ", "welcome!"):
                emit_delta(piece)
            return SendMessageResponse(
                session_id=session_id,
                status="continue",
                assistant_message="Hey there — welcome!",
            )

        with patch.object(main, "_run_lana_message", fake_run):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        events = _sse_events(resp.text)
        kinds = [e["type"] for e in events]
        self.assertEqual(
            kinds, ["status", "status", "delta", "delta", "delta", "result"]
        )
        deltas = "".join(e["text"] for e in events if e["type"] == "delta")
        turn = events[-1]["turn"]
        # Terminal result is the full, unchanged turn — identical to today's contract —
        # and its text matches what was streamed.
        self.assertEqual(turn["assistant_message"], "Hey there — welcome!")
        self.assertEqual(deltas, turn["assistant_message"])
        self.assertEqual(turn["session_id"], "sess-1")
        self.assertEqual(turn["status"], "continue")

    def test_template_path_emits_no_deltas_and_result_unchanged(self) -> None:
        # Canned/template replies never invoke a streaming synth call → no delta frames,
        # stream shape is exactly the pre-existing status→result contract.
        def fake_run(session_id, body, background_tasks, authorization, emit=None, emit_delta=None):
            emit("Reading your message…")
            return SendMessageResponse(
                session_id=session_id,
                status="continue",
                assistant_message="What ZIP code is your block? (e.g. 32827)",
            )

        with patch.object(main, "_run_lana_message", fake_run):
            resp = self._post()
        events = _sse_events(resp.text)
        self.assertEqual([e["type"] for e in events], ["status", "result"])
        self.assertEqual(
            events[-1]["turn"]["assistant_message"],
            "What ZIP code is your block? (e.g. 32827)",
        )

    def test_failed_turn_after_deltas_still_yields_terminal_frame(self) -> None:
        # Even if the turn dies mid-stream (deltas already sent), the client gets a
        # terminal frame — never a silently dropped connection.
        def fake_run(session_id, body, background_tasks, authorization, emit=None, emit_delta=None):
            emit_delta("Hey ")
            raise RuntimeError("synth exploded mid-stream")

        with patch.object(main, "_run_lana_message", fake_run):
            resp = self._post()
        events = _sse_events(resp.text)
        self.assertEqual([e["type"] for e in events], ["delta", "error"])
        self.assertEqual(events[-1]["detail"], "lana_message_failed")

    def test_empty_delta_is_not_framed(self) -> None:
        def fake_run(session_id, body, background_tasks, authorization, emit=None, emit_delta=None):
            emit_delta("")
            emit_delta("ok")
            return SendMessageResponse(
                session_id=session_id, status="continue", assistant_message="ok"
            )

        with patch.object(main, "_run_lana_message", fake_run):
            resp = self._post()
        events = _sse_events(resp.text)
        self.assertEqual([e["type"] for e in events], ["delta", "result"])


if __name__ == "__main__":
    unittest.main()
