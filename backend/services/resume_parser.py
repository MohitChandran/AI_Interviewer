import re
from typing import Any

import pdfplumber


class ResumeParser:
    """Extracts skills, projects, experience and education from a resume PDF."""

    @staticmethod
    def parse_pdf(file_path: str) -> dict[str, Any]:
        try:
            with pdfplumber.open(file_path) as pdf:
                # Extract all text from PDF
                full_text = ""
                for page in pdf.pages:
                    full_text += page.extract_text() + "\n"

                # Parse skills and projects
                skills = ResumeParser._extract_skills(full_text)
                projects = ResumeParser._extract_projects(full_text)
                experience = ResumeParser._extract_experience(full_text)
                education = ResumeParser._extract_education(full_text)

                return {
                    "full_text": full_text,
                    "skills": skills,
                    "projects": projects,
                    "experience": experience,
                    "education": education,
                    "success": True,
                }
        except Exception as e:
            return {
                "full_text": "",
                "skills": [],
                "projects": [],
                "experience": [],
                "education": "",
                "success": False,
                "error": str(e),
            }

    @staticmethod
    def _extract_skills(text: str) -> list[str]:
        """Extract skills from resume text."""
        skills = []
        skills_section = re.search(r"skills?:?\s*(.*?)(?:\n\n|\n[A-Z]|$)", text, re.IGNORECASE | re.DOTALL)

        if skills_section:
            skills_text = skills_section.group(1)
            # Common skill patterns
            skill_patterns = [
                r"\b(?:Python|Java|JavaScript|C\+\+|Ruby|Go|Rust|Swift|Kotlin|PHP)\b",
                r"\b(?:React|Angular|Vue|Node\.js|Express|Django|Flask|FastAPI)\b",
                r"\b(?:SQL|MongoDB|PostgreSQL|MySQL|Redis|Cassandra)\b",
                r"\b(?:AWS|Azure|GCP|Docker|Kubernetes|Jenkins|Git)\b",
                r"\b(?:Machine Learning|Deep Learning|AI|NLP|Computer Vision)\b",
                r"\b(?:TensorFlow|PyTorch|Scikit-learn|Keras)\b",
            ]

            for pattern in skill_patterns:
                matches = re.findall(pattern, skills_text, re.IGNORECASE)
                skills.extend(matches)

        return list(set(skills))[:10]  # Return unique skills, max 10

    @staticmethod
    def _extract_projects(text: str) -> list[str]:
        """Extract project names/descriptions from resume."""
        projects = []
        projects_section = re.search(r"projects?:?\s*(.*?)(?:\n\n[A-Z]|$)", text, re.IGNORECASE | re.DOTALL)

        if projects_section:
            project_text = projects_section.group(1)
            # Split by bullet points or newlines
            project_lines = re.split(r"\n+[\•\-\*]?\s*", project_text)
            projects = [p.strip() for p in project_lines if len(p.strip()) > 20][:3]

        return projects

    @staticmethod
    def _extract_experience(text: str) -> list[str]:
        """Extract work experience from resume."""
        experience = []
        exp_section = re.search(
            r"experience:?\s*(.*?)(?:\n\n[A-Z]|education|projects|skills|$)", text, re.IGNORECASE | re.DOTALL
        )

        if exp_section:
            exp_text = exp_section.group(1)
            exp_lines = re.split(r"\n+[\•\-\*]?\s*", exp_text)
            experience = [e.strip() for e in exp_lines if len(e.strip()) > 20][:3]

        return experience

    @staticmethod
    def _extract_education(text: str) -> str:
        """Extract education information."""
        edu_section = re.search(r"education:?\s*(.*?)(?:\n\n[A-Z]|$)", text, re.IGNORECASE | re.DOTALL)

        if edu_section:
            return edu_section.group(1).strip()[:200]

        return ""
