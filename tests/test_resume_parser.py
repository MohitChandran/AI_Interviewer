from backend.services.resume_parser import ResumeParser

RESUME_TEXT = """Jane Doe
Skills: Python, FastAPI, Docker, PostgreSQL, React

Projects:
- Built a real-time chat platform using WebSockets and Redis pub/sub
- Developed an ML pipeline for fraud detection with PyTorch

Experience:
- Backend intern at Acme Corp building REST APIs for payments

Education: B.Tech in Computer Science, 2024
"""


def test_extract_skills_finds_known_technologies():
    skills = {s.lower() for s in ResumeParser._extract_skills(RESUME_TEXT)}
    assert {"python", "fastapi", "docker", "postgresql", "react"} <= skills


def test_extract_projects_keeps_long_lines():
    projects = ResumeParser._extract_projects(RESUME_TEXT)
    assert any("chat platform" in p for p in projects)
    assert all(len(p) > 20 for p in projects)


def test_extract_education():
    assert "Computer Science" in ResumeParser._extract_education(RESUME_TEXT)


def test_parse_pdf_missing_file_returns_failure():
    result = ResumeParser.parse_pdf("/nonexistent/resume.pdf")
    assert result["success"] is False
    assert result["skills"] == []
