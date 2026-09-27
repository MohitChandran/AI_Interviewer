from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.db.models import (
    EndReason,
    EvaluationStatus,
    InterviewStatus,
    LLMPurpose,
    Rating,
    Recommendation,
    Speaker,
    Verdict,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ResumeSummary(BaseModel):
    skills: list[str]
    projects: list[str]
    has_content: bool


class InterviewCreated(BaseModel):
    interview_id: str
    resume: ResumeSummary


class InterviewSummary(ORMModel):
    id: str
    candidate_name: str
    candidate_email: str
    role: str
    status: InterviewStatus
    end_reason: EndReason | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: float | None


class TurnOut(ORMModel):
    seq: int
    speaker: Speaker
    text: str
    was_interrupted: bool
    created_at: datetime


class LLMCallOut(ORMModel):
    """Call metadata only; full prompts stay in the database."""

    purpose: LLMPurpose
    model: str
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    error: str | None
    created_at: datetime


class EventOut(ORMModel):
    type: str
    payload: dict[str, Any] | None
    created_at: datetime


class InterviewDetail(InterviewSummary):
    parsed_resume: dict[str, Any]
    llm_model: str | None
    tts_voice: str | None
    stt_model: str | None
    turns: list[TurnOut]
    llm_calls: list[LLMCallOut]
    events: list[EventOut]


class QuestionAssessmentOut(ORMModel):
    seq: int
    question: str
    answer_summary: str
    verdict: Verdict
    score: int
    feedback: str


class EvaluationOut(ORMModel):
    status: EvaluationStatus
    rating: Rating | None
    overall_score: int | None
    technical_score: int | None
    communication_score: int | None
    questions_asked: int | None
    questions_answered: int | None
    answered_correctly: int | None
    summary: str | None
    strengths: list[str] | None
    weaknesses: list[str] | None
    recommendation: Recommendation | None
    error: str | None
    model: str
    prompt_version: str
    created_at: datetime
    completed_at: datetime | None
    questions: list[QuestionAssessmentOut]
