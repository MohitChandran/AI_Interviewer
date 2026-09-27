import logging
import time
from typing import Literal

import httpx
from groq import DefaultHttpxClient, Groq
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.services.llm import LLMResult

logger = logging.getLogger(__name__)


class StrictModel(BaseModel):
    # Groq's strict JSON-schema mode requires additionalProperties: false on every object.
    model_config = ConfigDict(extra="forbid")


class QuestionEvaluation(StrictModel):
    question: str = Field(description="The interviewer's question, briefly paraphrased")
    answer_summary: str = Field(description="One-sentence summary of the candidate's answer")
    verdict: Literal["correct", "partially_correct", "incorrect", "unanswered", "not_applicable"] = Field(
        description="not_applicable is for questions with no right answer, e.g. behavioural ones"
    )
    score: int = Field(ge=1, le=5, description="Answer quality, 1 (poor or none) to 5 (excellent)")
    feedback: str = Field(description="One or two sentences of specific, constructive feedback")


class EvaluatorOutput(StrictModel):
    """The exact JSON the LLM must return; enforced by the API and validated again here."""

    technical_score: int = Field(ge=1, le=10)
    communication_score: int = Field(ge=1, le=10)
    summary: str = Field(description="Three to four sentence overall assessment")
    strengths: list[str]
    weaknesses: list[str]
    recommendation: Literal["advance", "hold", "reject"]
    questions: list[QuestionEvaluation]


RESPONSE_SCHEMA = {"name": "evaluation", "schema": EvaluatorOutput.model_json_schema(), "strict": True}


class CandidateEvaluator:
    def __init__(self, api_key: str, model: str, reasoning_effort: str, max_tokens: int):
        self.client = Groq(
            api_key=api_key,
            http_client=DefaultHttpxClient(timeout=httpx.Timeout(120.0, connect=10.0)),
        )
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens

    def evaluate(self, messages: list[dict[str, str]]) -> tuple[LLMResult, EvaluatorOutput | None]:
        """Blocking. Returns the call record, plus the parsed evaluation (None if it failed)."""
        kwargs = {"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}
        started = time.perf_counter()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=self.max_tokens,
                temperature=0.2,  # grading should be consistent, not creative
                response_format={"type": "json_schema", "json_schema": RESPONSE_SCHEMA},
                **kwargs,
            )
        except Exception as e:
            logger.exception("Evaluation request failed")
            return self._result(messages, started, error=repr(e)), None

        choice = response.choices[0]
        result = self._result(
            messages,
            started,
            text=choice.message.content or "",
            finish_reason=choice.finish_reason,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
        )
        try:
            return result, EvaluatorOutput.model_validate_json(result.text)
        except ValidationError as e:
            # e.g. finish_reason "length": the JSON was cut off before it closed.
            logger.error("Evaluation output failed validation (finish_reason=%s): %s", choice.finish_reason, e)
            result.error = f"invalid output: {e}"
            return result, None

    def _result(self, messages, started: float, text: str = "", **fields) -> LLMResult:
        return LLMResult(
            text=text,
            model=self.model,
            request_messages=messages,
            latency_ms=int((time.perf_counter() - started) * 1000),
            **fields,
        )
