import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import repository
from backend.db.engine import SessionMaker
from backend.db.models import EndReason, InterviewStatus, LLMPurpose, Speaker
from backend.services.llm import LLMResult

logger = logging.getLogger(__name__)

T = TypeVar("T")

COMPLETED_REASONS = {EndReason.TIME_LIMIT, EndReason.CANDIDATE_ENDED}


class InterviewRecorder:
    """Persists one live interview's transcript, LLM calls and events.

    Every method swallows database errors: losing a log row is bad, but dropping a
    candidate's live interview because the database hiccupped is worse.
    """

    def __init__(self, interview_id: str, sessionmaker: SessionMaker):
        self.interview_id = interview_id
        self._sessionmaker = sessionmaker
        # This recorder is the only writer of turns for its interview, so a local counter is safe.
        self._next_seq = 1
        self._last_interviewer_turn_id: int | None = None

    async def _write(self, action: str, fn: Callable[[AsyncSession], Awaitable[T]]) -> T | None:
        try:
            async with self._sessionmaker() as db:
                return await fn(db)
        except Exception:
            logger.exception("Failed to record %s", action)
            return None

    async def add_turn(self, speaker: Speaker, text: str) -> None:
        seq = self._next_seq
        self._next_seq += 1
        turn = await self._write(
            "turn",
            lambda db: repository.add_turn(db, interview_id=self.interview_id, seq=seq, speaker=speaker, text=text),
        )
        if turn is not None and speaker == Speaker.INTERVIEWER:
            self._last_interviewer_turn_id = turn.id

    async def mark_last_interviewer_turn_interrupted(self) -> None:
        turn_id = self._last_interviewer_turn_id
        if turn_id is not None:
            await self._write("interruption", lambda db: repository.mark_turn_interrupted(db, turn_id))

    async def llm_call(self, purpose: LLMPurpose, result: LLMResult) -> None:
        await self._write(
            "LLM call",
            lambda db: repository.add_llm_call(
                db,
                interview_id=self.interview_id,
                purpose=purpose,
                model=result.model,
                request_messages=result.request_messages,
                response_text=result.text,
                finish_reason=result.finish_reason,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=result.latency_ms,
                error=result.error,
            ),
        )

    async def event(self, type: str, **payload: Any) -> None:
        await self._write(
            f"event {type}", lambda db: repository.add_event(db, self.interview_id, type, payload or None)
        )

    async def finish(self, end_reason: EndReason) -> None:
        status = InterviewStatus.COMPLETED if end_reason in COMPLETED_REASONS else InterviewStatus.ABANDONED
        await self._write(
            "interview end", lambda db: repository.end_interview(db, self.interview_id, status, end_reason)
        )
        await self.event("interview_ended", status=status.value, end_reason=end_reason.value)
        logger.info("Interview ended: status=%s reason=%s", status.value, end_reason.value)
