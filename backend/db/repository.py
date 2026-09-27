from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.db.models import (
    EndReason,
    Evaluation,
    EvaluationStatus,
    Event,
    Interview,
    InterviewStatus,
    LLMCall,
    QuestionAssessment,
    Turn,
    utcnow,
)


async def create_interview(db: AsyncSession, **fields: Any) -> Interview:
    interview = Interview(**fields)
    db.add(interview)
    await db.commit()
    return interview


async def get_interview(db: AsyncSession, interview_id: str, *, with_details: bool = False) -> Interview | None:
    stmt = select(Interview).where(Interview.id == interview_id)
    if with_details:
        stmt = stmt.options(
            selectinload(Interview.turns),
            selectinload(Interview.llm_calls),
            selectinload(Interview.events),
        )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_interviews(
    db: AsyncSession, *, status: InterviewStatus | None = None, limit: int = 50, offset: int = 0
) -> list[Interview]:
    stmt = select(Interview).order_by(Interview.created_at.desc()).limit(limit).offset(offset)
    if status is not None:
        stmt = stmt.where(Interview.status == status)
    return list((await db.execute(stmt)).scalars())


async def delete_interview(db: AsyncSession, interview: Interview) -> None:
    await db.delete(interview)
    await db.commit()


def is_link_expired(interview: Interview, ttl_minutes: int) -> bool:
    return interview.status == InterviewStatus.CREATED and utcnow() - interview.created_at > timedelta(
        minutes=ttl_minutes
    )


async def mark_expired(db: AsyncSession, interview_id: str) -> None:
    await db.execute(
        update(Interview)
        .where(Interview.id == interview_id, Interview.status == InterviewStatus.CREATED)
        .values(status=InterviewStatus.EXPIRED)
    )
    await db.commit()


async def start_interview(db: AsyncSession, interview_id: str, **provider_info: str | None) -> bool:
    """Atomically move created -> in_progress. Returns False if someone else got there first.

    The status check lives in the WHERE clause, so two tabs racing to open the same
    interview can't both succeed: the database applies the UPDATE to at most one.
    """
    result = await db.execute(
        update(Interview)
        .where(Interview.id == interview_id, Interview.status == InterviewStatus.CREATED)
        .values(status=InterviewStatus.IN_PROGRESS, started_at=utcnow(), **provider_info)
    )
    await db.commit()
    return result.rowcount == 1


async def end_interview(db: AsyncSession, interview_id: str, status: InterviewStatus, end_reason: EndReason) -> None:
    await db.execute(
        update(Interview)
        .where(Interview.id == interview_id, Interview.status == InterviewStatus.IN_PROGRESS)
        .values(status=status, end_reason=end_reason, ended_at=utcnow())
    )
    await db.commit()


async def abandon_in_progress_interviews(db: AsyncSession) -> int:
    """On startup, nothing can be live yet: anything still in_progress died with the last process."""
    result = await db.execute(
        update(Interview)
        .where(Interview.status == InterviewStatus.IN_PROGRESS)
        .values(status=InterviewStatus.ABANDONED, end_reason=EndReason.SERVER_RESTART, ended_at=utcnow())
    )
    await db.commit()
    return result.rowcount


async def add_turn(db: AsyncSession, **fields: Any) -> Turn:
    turn = Turn(**fields)
    db.add(turn)
    await db.commit()
    return turn


async def mark_turn_interrupted(db: AsyncSession, turn_id: int) -> None:
    await db.execute(update(Turn).where(Turn.id == turn_id).values(was_interrupted=True))
    await db.commit()


async def add_llm_call(db: AsyncSession, **fields: Any) -> LLMCall:
    call = LLMCall(**fields)
    db.add(call)
    await db.commit()
    return call


async def add_event(db: AsyncSession, interview_id: str, type: str, payload: dict | None = None) -> Event:
    event = Event(interview_id=interview_id, type=type, payload=payload)
    db.add(event)
    await db.commit()
    return event


async def start_evaluation(db: AsyncSession, interview_id: str, **fields: Any) -> Evaluation:
    """Creates a pending evaluation, replacing any previous one for the interview."""
    await db.execute(delete(Evaluation).where(Evaluation.interview_id == interview_id))
    evaluation = Evaluation(interview_id=interview_id, status=EvaluationStatus.PENDING, **fields)
    db.add(evaluation)
    await db.commit()
    return evaluation


async def complete_evaluation(db: AsyncSession, evaluation_id: int, questions: list[dict], **fields: Any) -> None:
    await db.execute(
        update(Evaluation)
        .where(Evaluation.id == evaluation_id)
        .values(status=EvaluationStatus.COMPLETED, completed_at=utcnow(), **fields)
    )
    db.add_all(QuestionAssessment(evaluation_id=evaluation_id, seq=i, **q) for i, q in enumerate(questions, 1))
    await db.commit()


async def fail_evaluation(db: AsyncSession, evaluation_id: int, error: str) -> None:
    await db.execute(
        update(Evaluation)
        .where(Evaluation.id == evaluation_id)
        .values(status=EvaluationStatus.FAILED, error=error, completed_at=utcnow())
    )
    await db.commit()


async def fail_pending_evaluations(db: AsyncSession) -> int:
    """On startup: evaluations still pending were running in a process that no longer exists."""
    result = await db.execute(
        update(Evaluation)
        .where(Evaluation.status == EvaluationStatus.PENDING)
        .values(status=EvaluationStatus.FAILED, error="Server restarted during evaluation", completed_at=utcnow())
    )
    await db.commit()
    return result.rowcount


async def get_evaluation(db: AsyncSession, interview_id: str) -> Evaluation | None:
    stmt = select(Evaluation).where(Evaluation.interview_id == interview_id).options(selectinload(Evaluation.questions))
    return (await db.execute(stmt)).scalar_one_or_none()
