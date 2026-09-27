from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from backend.db import repository
from backend.db.models import EndReason, Interview, InterviewStatus, utcnow
from backend.services.resume_parser import ResumeParser

PARSED = {
    "full_text": "Jane Doe\nSkills: Python",
    "skills": ["Python"],
    "projects": ["Realtime chat app with WebSockets"],
    "experience": [],
    "education": "",
    "success": True,
}


@pytest.fixture(autouse=True)
def fake_parser(monkeypatch):
    monkeypatch.setattr(ResumeParser, "parse_pdf", staticmethod(lambda path: dict(PARSED)))


def create(client, content=b"%PDF-1.4 resume", **overrides):
    data = {"name": "Jane Doe", "email": "jane@example.com", "role": "Backend Engineer"}
    data.update(overrides)
    return client.post(
        "/api/interviews", data=data, files={"resume": ("../../etc/jane.pdf", content, "application/pdf")}
    )


def test_index_serves_frontend(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "AI Interview Bot" in response.text


def test_static_assets_are_served(client):
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/static/css/styles.css").status_code == 200


def test_health_checks_database(client):
    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert body["database"] is True
    assert set(body) >= {"groq_configured", "deepgram_configured", "elevenlabs_configured"}


def test_create_interview_persists_and_hides_private_fields(client, settings):
    response = create(client)
    assert response.status_code == 201
    interview_id = response.json()["interview_id"]
    assert response.json()["resume"] == {
        "skills": ["Python"],
        "projects": ["Realtime chat app with WebSockets"],
        "has_content": True,
    }

    # The untrusted client filename never touches the filesystem path.
    assert (settings.upload_dir / f"{interview_id}.pdf").exists()

    detail = client.get(f"/api/interviews/{interview_id}").json()
    assert detail["status"] == "created"
    assert detail["candidate_email"] == "jane@example.com"
    assert detail["parsed_resume"]["skills"] == ["Python"]
    assert "resume_text" not in detail and "resume_path" not in detail


def test_create_interview_validates_email(client):
    response = create(client, email="not-an-email")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "email"]


def test_rejects_files_that_are_not_pdfs(client, settings):
    # Named .pdf and sent as application/pdf, but the bytes say otherwise.
    response = create(client, content=b"MZ\x90\x00 not a pdf")
    assert response.status_code == 415
    assert list(settings.upload_dir.iterdir()) == []


def test_rejects_oversized_resumes(client, settings, monkeypatch):
    monkeypatch.setattr(settings, "max_resume_mb", 1)
    response = create(client, content=b"%PDF-" + b"0" * (2 * 2**20))
    assert response.status_code == 413
    assert list(settings.upload_dir.iterdir()) == []


def test_list_interviews_with_status_filter(client):
    first = create(client).json()["interview_id"]
    create(client, name="John")

    assert len(client.get("/api/interviews").json()) == 2
    created = client.get("/api/interviews", params={"status": "created"}).json()
    assert {i["id"] for i in created} >= {first}
    assert client.get("/api/interviews", params={"status": "completed"}).json() == []
    assert client.get("/api/interviews", params={"status": "bogus"}).status_code == 422


def test_delete_interview_removes_row_and_file(client, settings):
    interview_id = create(client).json()["interview_id"]

    assert client.delete(f"/api/interviews/{interview_id}").status_code == 204
    assert client.get(f"/api/interviews/{interview_id}").status_code == 404
    assert not Path(settings.upload_dir / f"{interview_id}.pdf").exists()
    assert client.delete(f"/api/interviews/{interview_id}").status_code == 404


def ws_error(client, interview_id):
    with client.websocket_connect(f"/ws/interviews/{interview_id}") as ws:
        return ws.receive_json()


def test_websocket_rejects_unknown_interview(client):
    assert ws_error(client, "nope") == {"type": "error", "message": "Interview not found"}


def test_websocket_rejects_expired_link(client):
    interview_id = create(client).json()["interview_id"]

    async def backdate():
        async with client.app.state.db_sessionmaker() as db:
            await db.execute(
                update(Interview).where(Interview.id == interview_id).values(created_at=utcnow() - timedelta(hours=1))
            )
            await db.commit()

    client.portal.call(backdate)

    assert ws_error(client, interview_id)["message"] == "This interview link has expired"
    assert client.get(f"/api/interviews/{interview_id}").json()["status"] == "expired"


def test_websocket_rejects_already_used_interview(client):
    interview_id = create(client).json()["interview_id"]

    async def mark_completed():
        async with client.app.state.db_sessionmaker() as db:
            await repository.start_interview(db, interview_id)
            await repository.end_interview(db, interview_id, InterviewStatus.COMPLETED, EndReason.TIME_LIMIT)

    client.portal.call(mark_completed)

    assert ws_error(client, interview_id)["message"] == "This interview can't be started (status: completed)"
