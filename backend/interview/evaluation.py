import asyncio
import logging
from typing import Any

from backend.config import Settings
from backend.db import repository
from backend.db.engine import SessionMaker
from backend.db.models import LLMPurpose, Rating, Speaker, Verdict
from backend.interview import prompts
from backend.services.evaluator import CandidateEvaluator, EvaluatorOutput

logger = logging.getLogger(__name__)

# Below this many candidate answers there isn't enough evidence to judge anyone fairly.
MIN_CANDIDATE_TURNS = 3
TECHNICAL_WEIGHT = 0.6
COMMUNICATION_WEIGHT = 0.4
RATING_THRESHOLDS = [(80, Rating.GOOD), (60, Rating.OK), (40, Rating.SHOULD_IMPROVE)]

# asyncio only keeps weak references to tasks, so a fire-and-forget task can be garbage
# collected mid-run. Holding them here keeps them alive until they finish.
_running: set[asyncio.Task] = set()


def rating_for(overall_score: int) -> Rating:
    for threshold, rating in RATING_THRESHOLDS:
        if overall_score >= threshold:
            return rating
    return Rating.BAD


def score_evaluation(output: EvaluatorOutput) -> dict[str, Any]:
    """The LLM scores each dimension; the overall score and label are decided here, in code,
    so the same scores always produce the same rating."""
    weighted = output.technical_score * TECHNICAL_WEIGHT + output.communication_score * COMMUNICATION_WEIGHT
    overall = round(weighted * 10)
    verdicts = [q.verdict for q in output.questions]
    return {
        "overall_score": overall,
        "rating": rating_for(overall),
        "technical_score": output.technical_score,
        "communication_score": output.communication_score,
        "questions_asked": len(verdicts),
        "questions_answered": sum(v != Verdict.UNANSWERED.value for v in verdicts),
        "answered_correctly": sum(v == Verdict.CORRECT.value for v in verdicts),
        "summary": output.summary,
        "strengths": output.strengths,
        "weaknesses": output.weaknesses,
        "recommendation": output.recommendation,
    }


async def begin_evaluation(interview_id: str, sessionmaker: SessionMaker, settings: Settings) -> None:
    """Creates a pending evaluation row now, then fills it in the background.

    The row exists before this returns, so a client polling right after the interview
    sees "pending" instead of "not found".
    """
    async with sessionmaker() as db:
        evaluation = await repository.start_evaluation(
            db,
            interview_id,
            model=settings.evaluation_model,
            prompt_version=prompts.EVALUATION_PROMPT_VERSION,
        )
    task = asyncio.create_task(_evaluate(interview_id, evaluation.id, sessionmaker, settings))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def _evaluate(interview_id: str, evaluation_id: int, sessionmaker: SessionMaker, settings: Settings) -> None:
    try:
        async with sessionmaker() as db:
            interview = await repository.get_interview(db, interview_id, with_details=True)
        transcript = [(t.speaker.value, t.text) for t in interview.turns]
        answers = sum(t.speaker == Speaker.CANDIDATE for t in interview.turns)

        if answers < MIN_CANDIDATE_TURNS:
            logger.info("Only %d candidate answer(s); not enough to evaluate", answers)
            async with sessionmaker() as db:
                await repository.complete_evaluation(
                    db,
                    evaluation_id,
                    questions=[],
                    rating=Rating.INSUFFICIENT_DATA,
                    summary=(
                        f"The interview ended after {answers} answer(s), which is not enough to assess the candidate."
                    ),
                )
            return

        evaluator = CandidateEvaluator(
            api_key=settings.groq_api_key,
            model=settings.evaluation_model,
            reasoning_effort=settings.evaluation_reasoning_effort,
            max_tokens=settings.evaluation_max_tokens,
        )
        messages = prompts.build_evaluation_messages(
            interview.candidate_name, interview.role, interview.parsed_resume, transcript
        )
        result, output = await asyncio.to_thread(evaluator.evaluate, messages)

        async with sessionmaker() as db:
            await repository.add_llm_call(
                db,
                interview_id=interview_id,
                purpose=LLMPurpose.EVALUATION,
                model=result.model,
                request_messages=result.request_messages,
                response_text=result.text,
                finish_reason=result.finish_reason,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                latency_ms=result.latency_ms,
                error=result.error,
            )
            if output is None:
                await repository.fail_evaluation(db, evaluation_id, result.error or "No output")
                return
            fields = score_evaluation(output)
            await repository.complete_evaluation(
                db, evaluation_id, questions=[q.model_dump() for q in output.questions], **fields
            )
        logger.info(
            "Evaluation done in %d ms: %s (%d/100)", result.latency_ms, fields["rating"].value, fields["overall_score"]
        )
    except Exception as e:
        logger.exception("Evaluation failed")
        async with sessionmaker() as db:
            await repository.fail_evaluation(db, evaluation_id, repr(e))
