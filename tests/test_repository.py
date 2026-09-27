from datetime import timedelta

from sqlalchemy import func, select

from backend.db import repository
from backend.db.models import EndReason, InterviewStatus, LLMCall, LLMPurpose, Speaker, Turn, utcnow


async def make_interview(db, **overrides):
    fields = dict(
        candidate_name="Jane Doe",
        candidate_email="jane@example.com",
        role="Backend Engineer",
        resume_filename="jane.pdf",
        resume_path="/tmp/jane.pdf",
        parsed_resume={"skills": ["Python"]},
    )
    fields.update(overrides)
    return await repository.create_interview(db, **fields)


async def reload(sessionmaker, interview_id, **kwargs):
    """Read back through a new session, so we see what was committed, not cached objects."""
    async with sessionmaker() as fresh:
        return await repository.get_interview(fresh, interview_id, **kwargs)


async def test_create_sets_defaults(db):
    interview = await make_interview(db)

    assert len(interview.id) == 36
    assert interview.status == InterviewStatus.CREATED
    assert interview.created_at.tzinfo is not None
    assert interview.duration_seconds is None


async def test_start_interview_only_succeeds_once(db, sessionmaker):
    interview = await make_interview(db)

    assert await repository.start_interview(db, interview.id, llm_model="m") is True
    # A second tab racing to open the same link loses.
    assert await repository.start_interview(db, interview.id, llm_model="m") is False

    reloaded = await reload(sessionmaker, interview.id)
    assert reloaded.status == InterviewStatus.IN_PROGRESS
    assert reloaded.started_at is not None
    assert reloaded.llm_model == "m"


async def test_end_interview_records_reason_and_duration(db, sessionmaker):
    interview = await make_interview(db)
    await repository.start_interview(db, interview.id)
    await repository.end_interview(db, interview.id, InterviewStatus.COMPLETED, EndReason.TIME_LIMIT)

    reloaded = await reload(sessionmaker, interview.id)
    assert reloaded.status == InterviewStatus.COMPLETED
    assert reloaded.end_reason == EndReason.TIME_LIMIT
    assert reloaded.duration_seconds >= 0


async def test_link_expiry(db, sessionmaker):
    fresh = await make_interview(db)
    stale = await make_interview(db, created_at=utcnow() - timedelta(minutes=31))

    assert not repository.is_link_expired(fresh, ttl_minutes=30)
    assert repository.is_link_expired(stale, ttl_minutes=30)

    await repository.mark_expired(db, stale.id)
    assert (await reload(sessionmaker, stale.id)).status == InterviewStatus.EXPIRED


async def test_abandon_in_progress_on_startup(db, sessionmaker):
    live = await make_interview(db)
    untouched = await make_interview(db)
    await repository.start_interview(db, live.id)

    assert await repository.abandon_in_progress_interviews(db) == 1

    reloaded = await reload(sessionmaker, live.id)
    assert reloaded.status == InterviewStatus.ABANDONED
    assert reloaded.end_reason == EndReason.SERVER_RESTART
    assert (await reload(sessionmaker, untouched.id)).status == InterviewStatus.CREATED


async def test_details_are_ordered_and_deleted_with_interview(db):
    interview = await make_interview(db)
    await repository.add_turn(db, interview_id=interview.id, seq=2, speaker=Speaker.CANDIDATE, text="I'm ready")
    await repository.add_turn(db, interview_id=interview.id, seq=1, speaker=Speaker.INTERVIEWER, text="Hi!")
    await repository.add_llm_call(
        db,
        interview_id=interview.id,
        purpose=LLMPurpose.GREETING,
        model="m",
        request_messages=[{"role": "user", "content": "hi"}],
        response_text="Hi!",
        latency_ms=5,
    )
    await repository.add_event(db, interview.id, "stt_connected")

    detailed = await repository.get_interview(db, interview.id, with_details=True)
    assert [t.text for t in detailed.turns] == ["Hi!", "I'm ready"]
    assert len(detailed.llm_calls) == 1
    assert detailed.events[0].type == "stt_connected"

    await repository.delete_interview(db, detailed)
    # ON DELETE CASCADE only works because engine.py turns on SQLite foreign keys.
    assert await db.scalar(select(func.count()).select_from(Turn)) == 0
    assert await db.scalar(select(func.count()).select_from(LLMCall)) == 0


async def test_list_filters_by_status_newest_first(db):
    older = await make_interview(db, created_at=utcnow() - timedelta(minutes=5))
    newer = await make_interview(db)
    await repository.start_interview(db, older.id)

    assert [i.id for i in await repository.list_interviews(db)] == [newer.id, older.id]
    assert [i.id for i in await repository.list_interviews(db, status=InterviewStatus.IN_PROGRESS)] == [older.id]
