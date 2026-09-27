import asyncio
import logging
from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from pydantic import EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.schemas import EvaluationOut, InterviewCreated, InterviewDetail, InterviewSummary, ResumeSummary
from backend.config import get_settings
from backend.db import repository
from backend.db.engine import SessionMaker, get_db, get_sessionmaker
from backend.db.models import InterviewStatus, new_id
from backend.interview.evaluation import begin_evaluation
from backend.logging_config import interview_context
from backend.services.resume_parser import ResumeParser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/interviews", tags=["interviews"])

PDF_MAGIC = b"%PDF-"
CHUNK_BYTES = 64 * 1024


async def _save_pdf(upload: UploadFile, path: Path, max_bytes: int) -> None:
    """Streams the upload to disk, rejecting non-PDFs and oversized files without buffering them."""
    head = await upload.read(len(PDF_MAGIC))
    if head != PDF_MAGIC:  # check the content itself; the filename and Content-Type are client-controlled
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "The resume must be a PDF file")

    written = len(head)
    async with await anyio.open_file(path, "wb") as out:
        await out.write(head)
        while chunk := await upload.read(CHUNK_BYTES):
            written += len(chunk)
            if written > max_bytes:
                break
            await out.write(chunk)
    if written > max_bytes:
        await anyio.Path(path).unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"The resume must be under {max_bytes // 2**20} MB"
        )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InterviewCreated)
async def create_interview(
    name: str = Form(..., min_length=1, max_length=200),
    email: EmailStr = Form(...),
    role: str = Form(..., min_length=1, max_length=200),
    resume: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    interview_id = new_id()
    with interview_context(interview_id):
        settings = get_settings()
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        # Name the file by ID: the client's filename is untrusted (could contain "../").
        resume_path = settings.upload_dir / f"{interview_id}.pdf"
        await _save_pdf(resume, resume_path, settings.max_resume_mb * 2**20)

        try:
            parsed = await asyncio.to_thread(ResumeParser.parse_pdf, str(resume_path))
            full_text = parsed.pop("full_text", "")
            await repository.create_interview(
                db,
                id=interview_id,
                candidate_name=name.strip(),
                candidate_email=str(email),
                role=role.strip(),
                resume_filename=resume.filename or "resume.pdf",
                resume_path=str(resume_path),
                resume_text=full_text,
                parsed_resume=parsed,
            )
        except Exception:
            await anyio.Path(resume_path).unlink(missing_ok=True)
            logger.exception("Failed to create interview")
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not process the resume") from None

        logger.info("Interview created for role %r (resume parsed: %s)", role, parsed.get("success"))
        return InterviewCreated(
            interview_id=interview_id,
            resume=ResumeSummary(
                skills=parsed.get("skills", []),
                projects=parsed.get("projects", []),
                has_content=bool(full_text),
            ),
        )


@router.get("", response_model=list[InterviewSummary])
async def list_interviews(
    status_filter: InterviewStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    return await repository.list_interviews(db, status=status_filter, limit=limit, offset=offset)


@router.get("/{interview_id}", response_model=InterviewDetail)
async def get_interview(interview_id: str, db: AsyncSession = Depends(get_db)):
    interview = await repository.get_interview(db, interview_id, with_details=True)
    if interview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interview not found")
    return interview


@router.delete("/{interview_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_interview(interview_id: str, db: AsyncSession = Depends(get_db)):
    interview = await repository.get_interview(db, interview_id)
    if interview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interview not found")
    if interview.status == InterviewStatus.IN_PROGRESS:
        raise HTTPException(status.HTTP_409_CONFLICT, "Interview is in progress")

    await repository.delete_interview(db, interview)
    await anyio.Path(interview.resume_path).unlink(missing_ok=True)
    with interview_context(interview_id):
        logger.info("Interview deleted")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{interview_id}/evaluation", response_model=EvaluationOut)
async def get_evaluation(interview_id: str, db: AsyncSession = Depends(get_db)):
    """Poll this after an interview: status is "pending" until the evaluation is ready."""
    evaluation = await repository.get_evaluation(db, interview_id)
    if evaluation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No evaluation for this interview")
    return evaluation


@router.post("/{interview_id}/evaluation", status_code=status.HTTP_202_ACCEPTED)
async def regenerate_evaluation(
    interview_id: str,
    db: AsyncSession = Depends(get_db),
    sessionmaker: SessionMaker = Depends(get_sessionmaker),
):
    """(Re)generates the evaluation, e.g. after a failure or a prompt change."""
    interview = await repository.get_interview(db, interview_id)
    if interview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Interview not found")
    if interview.status not in (InterviewStatus.COMPLETED, InterviewStatus.ABANDONED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Interview is {interview.status.value}")
    with interview_context(interview_id):
        await begin_evaluation(interview_id, sessionmaker, get_settings())
    return {"status": "pending"}
