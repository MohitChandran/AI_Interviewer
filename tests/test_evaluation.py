import pytest

from backend.config import Settings
from backend.db import repository
from backend.db.models import EvaluationStatus, LLMPurpose, Rating, Speaker
from backend.interview import evaluation
from backend.interview.evaluation import begin_evaluation, rating_for, score_evaluation
from backend.services.evaluator import EvaluatorOutput, QuestionEvaluation
from backend.services.llm import LLMResult
from tests.test_repository import make_interview


def make_output(technical=8, communication=7, verdicts=("correct", "partially_correct", "unanswered")):
    return EvaluatorOutput(
        technical_score=technical,
        communication_score=communication,
        summary="Solid backend fundamentals.",
        strengths=["Clear examples"],
        weaknesses=["Vague on scaling"],
        recommendation="advance",
        questions=[
            QuestionEvaluation(question=f"Q{i}", answer_summary="...", verdict=v, score=3, feedback="...")
            for i, v in enumerate(verdicts)
        ],
    )


@pytest.mark.parametrize(
    "score, rating",
    [
        (100, Rating.GOOD),
        (80, Rating.GOOD),
        (79, Rating.OK),
        (60, Rating.OK),
        (59, Rating.SHOULD_IMPROVE),
        (40, Rating.SHOULD_IMPROVE),
        (39, Rating.BAD),
        (0, Rating.BAD),
    ],
)
def test_rating_thresholds(score, rating):
    assert rating_for(score) == rating


def test_score_is_weighted_and_counts_questions():
    fields = score_evaluation(make_output(technical=8, communication=7))

    assert fields["overall_score"] == 76  # (8 * 0.6 + 7 * 0.4) * 10
    assert fields["rating"] == Rating.OK
    assert (fields["questions_asked"], fields["questions_answered"], fields["answered_correctly"]) == (3, 2, 1)


class FakeEvaluator:
    output = None

    def __init__(self, **kwargs):
        pass

    def evaluate(self, messages):
        text = self.output.model_dump_json() if self.output else ""
        result = LLMResult(
            text=text, model="m", request_messages=messages, latency_ms=10, error=None if self.output else "boom"
        )
        return result, self.output


@pytest.fixture
def fake_evaluator(monkeypatch):
    monkeypatch.setattr(evaluation, "CandidateEvaluator", FakeEvaluator)
    FakeEvaluator.output = make_output()
    return FakeEvaluator


async def interview_with_answers(db, answers):
    interview = await make_interview(db)
    seq = 0
    for i in range(answers):
        for speaker, text in [(Speaker.INTERVIEWER, f"Question {i}?"), (Speaker.CANDIDATE, f"Answer {i}.")]:
            seq += 1
            await repository.add_turn(db, interview_id=interview.id, seq=seq, speaker=speaker, text=text)
    return interview


async def run(interview_id, sessionmaker):
    await begin_evaluation(interview_id, sessionmaker, Settings())
    for task in list(evaluation._running):
        await task
    async with sessionmaker() as fresh:
        return await repository.get_evaluation(fresh, interview_id)


async def test_completed_evaluation_is_saved_with_questions(db, sessionmaker, fake_evaluator):
    interview = await interview_with_answers(db, answers=3)

    saved = await run(interview.id, sessionmaker)

    assert saved.status == EvaluationStatus.COMPLETED
    assert (saved.rating, saved.overall_score) == (Rating.OK, 76)
    assert [q.seq for q in saved.questions] == [1, 2, 3]
    async with sessionmaker() as fresh:
        detail = await repository.get_interview(fresh, interview.id, with_details=True)
    assert [c.purpose for c in detail.llm_calls] == [LLMPurpose.EVALUATION]


async def test_short_interview_is_insufficient_data_without_calling_the_llm(db, sessionmaker, fake_evaluator):
    FakeEvaluator.output = None  # would fail if called
    interview = await interview_with_answers(db, answers=2)

    saved = await run(interview.id, sessionmaker)

    assert saved.status == EvaluationStatus.COMPLETED
    assert saved.rating == Rating.INSUFFICIENT_DATA
    assert saved.questions == []


async def test_invalid_llm_output_marks_evaluation_failed(db, sessionmaker, fake_evaluator):
    FakeEvaluator.output = None
    interview = await interview_with_answers(db, answers=3)

    saved = await run(interview.id, sessionmaker)

    assert saved.status == EvaluationStatus.FAILED
    assert saved.error == "boom"


async def test_regenerating_replaces_the_previous_evaluation(db, sessionmaker, fake_evaluator):
    interview = await interview_with_answers(db, answers=3)
    await run(interview.id, sessionmaker)

    FakeEvaluator.output = make_output(technical=10, communication=10)
    saved = await run(interview.id, sessionmaker)

    assert saved.rating == Rating.GOOD
    assert len(saved.questions) == 3  # the old question rows went with the old evaluation


async def test_pending_evaluations_fail_on_startup(db):
    interview = await make_interview(db)
    await repository.start_evaluation(db, interview.id, model="m", prompt_version="v")

    assert await repository.fail_pending_evaluations(db) == 1


def test_evaluation_api_errors(client):
    assert client.get("/api/interviews/nope/evaluation").status_code == 404
    assert client.post("/api/interviews/nope/evaluation").status_code == 404
