import asyncio

import pytest

from backend.config import Settings
from backend.interview.session import InterviewSession, TurnState, split_sentences
from backend.services.llm import LLMResult


class FakeRecorder:
    def __getattr__(self, name):
        async def noop(*args, **kwargs):
            return None

        return noop


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class FakeLLM:
    def __init__(self, reply="Next question?"):
        self.reply = reply
        self.user_turns = []

    def generate_response(self, user_text, history, resume):
        self.user_turns.append(user_text)
        return LLMResult(text=self.reply, model="m", request_messages=[], latency_ms=1)


class FakeTTS:
    def __init__(self):
        self.calls = 0
        self.hold_after_first = None  # an asyncio.Event: later sentences wait until it is set

    async def synthesize(self, text):
        self.calls += 1
        if self.calls > 1 and self.hold_after_first is not None:
            await self.hold_after_first.wait()
        return f"audio:{text}".encode()

    async def aclose(self):
        pass


@pytest.fixture
def session():
    s = InterviewSession("Jane", "Engineer", {}, Settings(barge_in_min_words=2), FakeRecorder())
    s.llm = FakeLLM()
    s.tts = FakeTTS()
    s.websocket = FakeWebSocket()
    s.interview_active = True
    return s


async def wait_until(condition, timeout=2.0):
    """Replies run LLM/TTS in worker threads, so poll instead of assuming one loop tick is enough."""

    async def poll():
        while not condition():  # noqa: ASYNC110 - polling state set by worker threads
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def test_interim_results_are_replaced_not_appended(session):
    session._on_transcript_received("So I was", False)
    session._on_transcript_received("So I was building", False)
    session._on_transcript_received("So I was building a chatbot.", True)
    session._on_transcript_received("It used", False)

    assert session._pending_speech() == "So I was building a chatbot. It used"


async def test_silence_sends_one_reply_with_the_final_text(session):
    session._on_transcript_received("I built a chatbot.", True)
    session._on_silence_detected()
    session._on_silence_detected()  # a second silence while thinking must not start another reply
    await wait_until(lambda: session.state == TurnState.SPEAKING)

    assert session.llm.user_turns == ["I built a chatbot."]
    transcript, header, chunk = session.websocket.sent
    assert transcript == {"type": "candidate_transcript", "text": "I built a chatbot."}
    assert header == {"type": "ai_response", "response_id": 1, "text": "Next question?", "chunks": 1}
    assert chunk["type"] == "ai_audio" and chunk["index"] == 0
    assert session.state == TurnState.SPEAKING


async def test_waits_briefly_for_the_final_transcript(session):
    session._on_transcript_received("I fine tuned", False)
    session._on_silence_detected()
    await asyncio.sleep(0)
    session._on_transcript_received("I fine-tuned Llama.", True)
    await wait_until(lambda: session.state == TurnState.SPEAKING)

    assert session.llm.user_turns == ["I fine-tuned Llama."]


async def test_speech_while_thinking_is_kept_for_next_turn_not_an_interruption(session):
    session.state = TurnState.THINKING
    session._on_transcript_received("to deploy it", True)

    assert session.state == TurnState.THINKING
    assert not any(m["type"] == "stop_ai_audio" for m in session.websocket.sent)
    assert session._pending_speech() == "to deploy it"


async def test_barge_in_needs_enough_words(session):
    session.state = TurnState.SPEAKING
    session._response_id = 3

    session._on_transcript_received("mm", False)
    assert session.state == TurnState.SPEAKING

    session._on_transcript_received("wait Nikki", False)
    await wait_until(lambda: session.websocket.sent)
    assert session.state == TurnState.LISTENING
    assert session.websocket.sent[-1] == {"type": "stop_ai_audio", "response_id": 3}
    assert session._pending_speech() == "wait Nikki"


def test_stale_audio_finished_messages_are_ignored(session):
    session.state = TurnState.SPEAKING
    session._response_id = 5

    session.on_ai_audio_completed(4)
    assert session.state == TurnState.SPEAKING

    session.on_ai_audio_completed(5)
    assert session.state == TurnState.LISTENING


def test_split_sentences():
    assert split_sentences("Great work! How did you deploy it? Did you use Docker?") == [
        "Great work!",
        "How did you deploy it?",
        "Did you use Docker?",
    ]
    assert split_sentences("Version 2.5 of the model helped") == ["Version 2.5 of the model helped"]


async def test_reply_is_sent_sentence_by_sentence(session):
    session.llm.reply = "Nice. How did you deploy it? Did you use Docker?"
    session._on_transcript_received("I built a chatbot.", True)
    session._on_silence_detected()
    await wait_until(lambda: len(session.websocket.sent) == 5)

    _, header, *chunks = session.websocket.sent
    assert header["chunks"] == 3
    assert [c["index"] for c in chunks] == [0, 1, 2]


async def test_interruption_stops_the_remaining_sentences(session):
    session.llm.reply = "Nice. How did you deploy it? Did you use Docker?"
    session.tts.hold_after_first = asyncio.Event()
    session._on_transcript_received("I built a chatbot.", True)
    session._on_silence_detected()
    await wait_until(lambda: len(session.websocket.sent) == 3)  # transcript, header, first sentence

    session._on_transcript_received("sorry one more thing", False)
    session.tts.hold_after_first.set()
    await asyncio.sleep(0.05)

    types = [m["type"] for m in session.websocket.sent]
    assert types == ["candidate_transcript", "ai_response", "ai_audio", "stop_ai_audio"]
