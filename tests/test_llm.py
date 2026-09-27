from types import SimpleNamespace

from backend.services.llm import FALLBACK_RESPONSE, InterviewerLLM

RESUME = {"skills": ["Python"], "projects": ["Chat app built with WebSockets"]}


class FakeCompletions:
    def __init__(self, reply="Tell me more."):
        self.reply = reply
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=42, completion_tokens=7),
        )


def make_llm(reply="Tell me more.", history=4):
    llm = InterviewerLLM(api_key="test", model="test-model", history_messages=history)
    completions = FakeCompletions(reply)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return llm, completions


def test_start_interview_builds_system_prompt_from_resume():
    llm, _ = make_llm("Hi, I'm Nikki!")
    greeting = llm.start_interview("Jane", "Backend Engineer", RESUME)

    assert greeting.text == "Hi, I'm Nikki!"
    assert llm.messages[0]["role"] == "system"
    assert "Jane" in llm.messages[0]["content"]
    assert "Python" in llm.messages[0]["content"]
    assert llm.messages[-1] == {"role": "assistant", "content": "Hi, I'm Nikki!"}


def test_history_is_trimmed_but_system_prompt_kept():
    llm, _ = make_llm(history=4)
    llm.start_interview("Jane", "Engineer", RESUME)
    for i in range(10):
        llm.generate_response(f"answer {i}", [{}, {}], RESUME)

    assert llm.messages[0]["role"] == "system"
    # 4 retained history messages + the new user turn + assistant reply
    assert len(llm.messages) <= 1 + 4 + 2


def test_generate_response_passes_generation_params():
    llm, completions = make_llm()
    llm.start_interview("Jane", "Engineer", RESUME)
    llm.generate_response("hello", [{}, {}], RESUME)

    call = completions.calls[-1]
    assert call["max_tokens"] == llm.max_tokens
    assert call["temperature"] == llm.temperature


def test_reasoning_effort_only_sent_when_configured():
    llm, completions = make_llm()
    llm.start_interview("Jane", "Engineer", RESUME)
    assert "reasoning_effort" not in completions.calls[-1]

    llm.reasoning_effort = "low"
    llm.generate_response("hello", [{}, {}], RESUME)
    assert completions.calls[-1]["reasoning_effort"] == "low"


def test_generate_response_falls_back_on_error():
    llm, completions = make_llm()
    llm.start_interview("Jane", "Engineer", RESUME)
    completions.reply = RuntimeError("boom")
    result = llm.generate_response("hello", [], RESUME)
    assert result.text == FALLBACK_RESPONSE
    assert "boom" in result.error


def test_result_carries_call_metadata():
    llm, _ = make_llm("Tell me more.")
    llm.start_interview("Jane", "Engineer", RESUME)
    result = llm.generate_response("I built a chat app", [{}, {}], RESUME)

    assert (result.model, result.finish_reason) == ("test-model", "stop")
    assert (result.prompt_tokens, result.completion_tokens) == (42, 7)
    assert result.latency_ms >= 0
    # The recorded request is a snapshot: it ends with this user turn, not the reply appended after.
    assert result.request_messages[-1] == {"role": "user", "content": "I built a chat app"}
