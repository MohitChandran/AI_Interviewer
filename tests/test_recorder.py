from backend.db import repository
from backend.db.models import EndReason, InterviewStatus, LLMPurpose, Speaker
from backend.interview.recorder import InterviewRecorder
from backend.services.llm import LLMResult
from tests.test_repository import make_interview, reload


async def test_records_transcript_llm_calls_and_end(sessionmaker, db):
    interview = await make_interview(db)
    await repository.start_interview(db, interview.id)
    recorder = InterviewRecorder(interview.id, sessionmaker)

    await recorder.add_turn(Speaker.INTERVIEWER, "Ready?")
    await recorder.add_turn(Speaker.CANDIDATE, "Yes")
    await recorder.add_turn(Speaker.INTERVIEWER, "Tell me about your project")
    await recorder.mark_last_interviewer_turn_interrupted()
    await recorder.llm_call(
        LLMPurpose.RESPONSE,
        LLMResult(
            text="Tell me about your project",
            model="m",
            request_messages=[],
            latency_ms=120,
            finish_reason="stop",
            prompt_tokens=50,
            completion_tokens=8,
        ),
    )
    await recorder.finish(EndReason.CANDIDATE_ENDED)

    saved = await reload(sessionmaker, interview.id, with_details=True)
    assert [(t.seq, t.speaker, t.was_interrupted) for t in saved.turns] == [
        (1, Speaker.INTERVIEWER, False),
        (2, Speaker.CANDIDATE, False),
        (3, Speaker.INTERVIEWER, True),
    ]
    assert saved.llm_calls[0].completion_tokens == 8
    assert saved.status == InterviewStatus.COMPLETED
    assert saved.events[-1].type == "interview_ended"


async def test_disconnect_marks_abandoned(sessionmaker, db):
    interview = await make_interview(db)
    await repository.start_interview(db, interview.id)

    await InterviewRecorder(interview.id, sessionmaker).finish(EndReason.DISCONNECTED)

    assert (await reload(sessionmaker, interview.id)).status == InterviewStatus.ABANDONED


async def test_database_errors_never_reach_the_interview(sessionmaker, caplog):
    # No such interview: the foreign key rejects the insert, and the recorder must swallow it.
    recorder = InterviewRecorder("missing-interview", sessionmaker)

    await recorder.add_turn(Speaker.CANDIDATE, "hello")
    await recorder.event("anything")

    assert "Failed to record turn" in caplog.text
