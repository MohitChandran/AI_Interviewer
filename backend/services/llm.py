import logging
import time
from dataclasses import dataclass

import httpx
from groq import DefaultHttpxClient, Groq

from backend.interview import prompts

logger = logging.getLogger(__name__)

FALLBACK_RESPONSE = "I'm sorry, could you please clarify that?"
# Keep the connection open between turns; httpx's 5 s default would reconnect every turn.
KEEPALIVE_SECONDS = 300


@dataclass
class LLMResult:
    """The reply plus everything worth recording about the call that produced it."""

    text: str
    model: str
    request_messages: list[dict[str, str]]
    latency_ms: int
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None


class InterviewerLLM:
    """Chat-completion wrapper that holds the interviewer's message history."""

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = 150,
        temperature: float = 0.7,
        history_messages: int = 12,
        reasoning_effort: str = "",
    ):
        self.client = Groq(
            api_key=api_key,
            http_client=DefaultHttpxClient(limits=httpx.Limits(keepalive_expiry=KEEPALIVE_SECONDS)),
        )
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.history_messages = history_messages
        self.reasoning_effort = reasoning_effort
        self.messages: list[dict[str, str]] = []

    def _complete(self, **kwargs) -> LLMResult:
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        request_messages = [dict(m) for m in self.messages]
        started = time.perf_counter()
        response = self.client.chat.completions.create(model=self.model, messages=self.messages, **kwargs)
        choice = response.choices[0]
        return LLMResult(
            text=choice.message.content,
            model=self.model,
            request_messages=request_messages,
            latency_ms=_elapsed_ms(started),
            finish_reason=choice.finish_reason,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
        )

    def _failed(self, fallback_text: str, error: Exception, started: float) -> LLMResult:
        return LLMResult(
            text=fallback_text,
            model=self.model,
            request_messages=[dict(m) for m in self.messages],
            latency_ms=_elapsed_ms(started),
            error=repr(error),
        )

    def start_interview(self, candidate_name: str, role: str, resume_data: dict) -> LLMResult:
        self.messages = [
            {"role": "system", "content": prompts.build_system_prompt(candidate_name, role, resume_data)},
            {"role": "user", "content": prompts.build_greeting_prompt(candidate_name, role)},
        ]
        result = self._complete()
        logger.info("Greeting (%d ms): %s", result.latency_ms, result.text)
        self.messages.append({"role": "assistant", "content": result.text})
        return result

    def generate_response(self, user_text: str, conversation_history: list[dict], resume_data: dict) -> LLMResult:
        started = time.perf_counter()
        try:
            self._trim_history()
            if resume_data and len(conversation_history) > 1:
                self._add_context_reminder(resume_data)

            self.messages.append({"role": "user", "content": user_text})
            result = self._complete(max_tokens=self.max_tokens, temperature=self.temperature)
            logger.info("Interviewer (%d ms): %s", result.latency_ms, result.text)
            self.messages.append({"role": "assistant", "content": result.text})
            return result
        except Exception as e:
            logger.exception("LLM response generation failed")
            return self._failed(FALLBACK_RESPONSE, e, started)

    def generate_closing(self, candidate_name: str) -> LLMResult:
        started = time.perf_counter()
        try:
            self.messages.append({"role": "user", "content": prompts.build_closing_prompt(candidate_name)})
            result = self._complete()
            logger.info("Closing (%d ms): %s", result.latency_ms, result.text)
            return result
        except Exception as e:
            logger.exception("LLM closing generation failed")
            return self._failed(f"Thank you {candidate_name}, we'll contact you soon.", e, started)

    def _trim_history(self) -> None:
        """Keep the system prompt plus the most recent messages to bound token usage."""
        if len(self.messages) > self.history_messages + 1:
            self.messages = [self.messages[0]] + self.messages[1:][-self.history_messages :]

    def _add_context_reminder(self, resume_data: dict) -> None:
        reminder = prompts.build_context_reminder(resume_data)
        system = self.messages[0]
        if system["role"] == "system" and reminder not in system["content"]:
            system["content"] += reminder


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
