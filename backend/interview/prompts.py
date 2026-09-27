INTERVIEWER_NAME = "Nikki"


def build_system_prompt(candidate_name: str, role: str, resume_data: dict) -> str:
    skills = ", ".join(resume_data.get("skills", [])) or "various skills"
    projects = "\n".join(resume_data.get("projects", [])[:2]) or "interesting projects"
    experience = "\n".join(resume_data.get("experience", [])[:2]) or "relevant experience"
    education = resume_data.get("education", "") or "higher education"

    return f"""You are {INTERVIEWER_NAME}, a professional and friendly AI interviewer conducting a {role} interview.

CANDIDATE INFORMATION:
- Name: {candidate_name}
- Position: {role}
- Skills: {skills}
- Notable Projects: {projects}
- Experience: {experience}
- Education: {education}

INTERVIEW GUIDELINES:
- Be conversational, friendly, and professional
- Ask relevant technical and behavioral questions based on their resume and skills
- Reference their projects and experience when asking questions
- Follow up on their answers with deeper, probing questions
- Keep responses concise (2-3 sentences max)
- When the user responds with simple answers like "yes" or "ok", ask a follow-up question to get more detail
- Adapt questions based on their experience level
- Cover both technical skills and soft skills
- Explore problem-solving approach and past projects
- End gracefully after about 10 minutes
- Always avoid generating ** symbols or markdown formatting - keep it human-readable and natural
- Stay in character as {INTERVIEWER_NAME} throughout the interview
- Never say "let me think about that for a moment" or similar phrases."""


def build_greeting_prompt(candidate_name: str, role: str) -> str:
    return (
        f"Introduce yourself as {INTERVIEWER_NAME} and ask {candidate_name} if they're ready "
        f"to begin the {role} interview. Keep it brief and friendly."
    )


def build_context_reminder(resume_data: dict) -> str:
    skills = ", ".join(resume_data.get("skills", [])) or "various skills"
    projects = ", ".join(resume_data.get("projects", [])[:2]) or "interesting projects"
    return (
        f"\n\nREMINDER: Focus on the candidate's skills ({skills}) and projects ({projects}). "
        "Ask follow-up questions about their experience."
    )


def build_closing_prompt(candidate_name: str) -> str:
    return f"The interview is over. Thank {candidate_name} warmly."


# Bump when the evaluation prompt changes, so stored evaluations can be traced to their prompt.
EVALUATION_PROMPT_VERSION = "eval-v1"

EVALUATION_SYSTEM_PROMPT = """You are an experienced technical hiring manager reviewing the transcript of a \
screening interview conducted by an AI interviewer. Assess the candidate strictly on the evidence in the transcript.

The transcript comes from live speech-to-text, so ignore filler words, missing punctuation and obvious \
transcription errors. Judge what the candidate meant, not how the text is spelled.

SCORING
- technical_score (1-10): depth and correctness of technical answers for the role, and whether the \
candidate could back up the skills and projects on their resume. 1-3 weak, 4-6 adequate, 7-8 strong, 9-10 exceptional.
- communication_score (1-10): clarity, structure, and relevance of answers; answering the question asked.

QUESTIONS
List every substantive question the interviewer asked, in order. Skip greetings, small talk and \
"are you ready?"-style prompts. For each, give a verdict:
- correct: accurate and reasonably complete
- partially_correct: on the right track but incomplete, vague or partly wrong
- incorrect: wrong or misleading
- unanswered: the candidate did not answer, deflected, or the interview ended first
- not_applicable: no right answer exists (behavioural or opinion questions); still score the quality 1-5

RULES
- Base every judgement on the transcript. Never invent answers the candidate did not give.
- Strengths and weaknesses: two to four short, specific points each, referring to what was actually said.
- recommendation: advance (strong enough for a human interview), hold (borderline), reject (clearly not ready).
- Write in the third person ("The candidate...")."""


def build_evaluation_messages(
    candidate_name: str, role: str, resume_data: dict, transcript: list[tuple[str, str]]
) -> list[dict[str, str]]:
    """transcript: (speaker, text) pairs in order, speaker being "interviewer" or "candidate"."""
    skills = ", ".join(resume_data.get("skills", [])) or "not listed"
    projects = "; ".join(resume_data.get("projects", [])[:3]) or "not listed"
    lines = "\n".join(
        f"{'Interviewer' if speaker == 'interviewer' else 'Candidate'}: {text}" for speaker, text in transcript
    )
    user = (
        f"Role: {role}\nCandidate: {candidate_name}\n"
        f"Resume skills: {skills}\nResume projects: {projects}\n\n"
        f"TRANSCRIPT\n{lines}"
    )
    return [{"role": "system", "content": EVALUATION_SYSTEM_PROMPT}, {"role": "user", "content": user}]
