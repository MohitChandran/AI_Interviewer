import enum
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, MetaData, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator):
    """Stores datetimes as UTC and always returns timezone-aware values.

    SQLite has no timezone support and would otherwise hand back naive datetimes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("Naive datetimes are not allowed; use utcnow()")
            value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    # Deterministic constraint names, so Alembic can find and alter them later.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    type_annotation_map = {datetime: UTCDateTime, dict[str, Any]: JSON, list[dict[str, Any]]: JSON, list[str]: JSON}


def _enum(enum_cls: type[enum.Enum]) -> Enum:
    # Store the readable value ("in_progress"), not the member name ("IN_PROGRESS").
    return Enum(
        enum_cls,
        native_enum=False,
        length=30,
        values_callable=lambda members: [m.value for m in members],
    )


class InterviewStatus(str, enum.Enum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    EXPIRED = "expired"


class EndReason(str, enum.Enum):
    TIME_LIMIT = "time_limit"
    CANDIDATE_ENDED = "candidate_ended"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    SERVER_RESTART = "server_restart"


class Speaker(str, enum.Enum):
    INTERVIEWER = "interviewer"
    CANDIDATE = "candidate"


class LLMPurpose(str, enum.Enum):
    GREETING = "greeting"
    RESPONSE = "response"
    CLOSING = "closing"
    EVALUATION = "evaluation"


class EvaluationStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class Rating(str, enum.Enum):
    GOOD = "good"
    OK = "ok"
    SHOULD_IMPROVE = "should_improve"
    BAD = "bad"
    INSUFFICIENT_DATA = "insufficient_data"


class Recommendation(str, enum.Enum):
    ADVANCE = "advance"
    HOLD = "hold"
    REJECT = "reject"


class Verdict(str, enum.Enum):
    CORRECT = "correct"
    PARTIALLY_CORRECT = "partially_correct"
    INCORRECT = "incorrect"
    UNANSWERED = "unanswered"
    NOT_APPLICABLE = "not_applicable"  # e.g. behavioural questions with no right answer


class Interview(Base):
    __tablename__ = "interviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_name: Mapped[str] = mapped_column(String(200))
    candidate_email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(200))

    resume_filename: Mapped[str] = mapped_column(String(255))
    resume_path: Mapped[str] = mapped_column(String(500))
    resume_text: Mapped[str] = mapped_column(Text, default="")
    parsed_resume: Mapped[dict[str, Any]] = mapped_column(default=dict)

    status: Mapped[InterviewStatus] = mapped_column(_enum(InterviewStatus), default=InterviewStatus.CREATED, index=True)
    end_reason: Mapped[EndReason | None] = mapped_column(_enum(EndReason))

    # Snapshot of which providers ran this interview, for comparing results later.
    llm_model: Mapped[str | None] = mapped_column(String(100))
    tts_voice: Mapped[str | None] = mapped_column(String(100))
    stt_model: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]

    # lazy="raise": in async code an implicit lazy load would fail obscurely, so make
    # it fail loudly and force callers to eager-load with selectinload().
    turns: Mapped[list["Turn"]] = relationship(
        back_populates="interview",
        order_by="Turn.seq",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )
    llm_calls: Mapped[list["LLMCall"]] = relationship(
        back_populates="interview",
        order_by="LLMCall.id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="interview",
        order_by="Event.id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )
    evaluation: Mapped[Optional["Evaluation"]] = relationship(
        back_populates="interview",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None


class Turn(Base):
    """One utterance in the transcript, in conversation order."""

    __tablename__ = "turns"
    __table_args__ = (UniqueConstraint("interview_id", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_id: Mapped[str] = mapped_column(ForeignKey("interviews.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int]
    speaker: Mapped[Speaker] = mapped_column(_enum(Speaker))
    text: Mapped[str] = mapped_column(Text)
    was_interrupted: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    interview: Mapped[Interview] = relationship(back_populates="turns")


class LLMCall(Base):
    """Every request made to the LLM, with the exact messages sent and usage returned."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_id: Mapped[str] = mapped_column(ForeignKey("interviews.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[LLMPurpose] = mapped_column(_enum(LLMPurpose))
    model: Mapped[str] = mapped_column(String(100))
    request_messages: Mapped[list[dict[str, Any]]]
    response_text: Mapped[str | None] = mapped_column(Text)
    finish_reason: Mapped[str | None] = mapped_column(String(30))
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    latency_ms: Mapped[int]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    interview: Mapped[Interview] = relationship(back_populates="llm_calls")


class Event(Base):
    """Timeline of notable moments (connected, interrupted, TTS failed, ...)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_id: Mapped[str] = mapped_column(ForeignKey("interviews.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(50), index=True)
    payload: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    interview: Mapped[Interview] = relationship(back_populates="events")


class Evaluation(Base):
    """The post-interview assessment of a candidate. One per interview; regenerating replaces it."""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    interview_id: Mapped[str] = mapped_column(ForeignKey("interviews.id", ondelete="CASCADE"), unique=True)
    status: Mapped[EvaluationStatus] = mapped_column(_enum(EvaluationStatus), default=EvaluationStatus.PENDING)
    # Which prompt and model produced this, so results from different versions can be told apart.
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(20))

    rating: Mapped[Rating | None] = mapped_column(_enum(Rating), index=True)
    overall_score: Mapped[int | None]  # 0-100, computed in code from the dimension scores
    technical_score: Mapped[int | None]  # 1-10, from the LLM
    communication_score: Mapped[int | None]  # 1-10, from the LLM
    questions_asked: Mapped[int | None]
    questions_answered: Mapped[int | None]
    answered_correctly: Mapped[int | None]
    summary: Mapped[str | None] = mapped_column(Text)
    strengths: Mapped[list[str] | None]
    weaknesses: Mapped[list[str] | None]
    recommendation: Mapped[Recommendation | None] = mapped_column(_enum(Recommendation))
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None]

    interview: Mapped[Interview] = relationship(back_populates="evaluation")
    questions: Mapped[list["QuestionAssessment"]] = relationship(
        back_populates="evaluation",
        order_by="QuestionAssessment.seq",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


class QuestionAssessment(Base):
    """How the candidate did on one question the interviewer asked."""

    __tablename__ = "question_assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int]
    question: Mapped[str] = mapped_column(Text)
    answer_summary: Mapped[str] = mapped_column(Text)
    verdict: Mapped[Verdict] = mapped_column(_enum(Verdict))
    score: Mapped[int]  # 1-5
    feedback: Mapped[str] = mapped_column(Text)

    evaluation: Mapped[Evaluation] = relationship(back_populates="questions")
