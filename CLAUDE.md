# CLAUDE.md

AI voice interviewer: a candidate uploads a resume PDF, then an LLM persona ("Nikki") runs a live spoken interview over a WebSocket. Stack: FastAPI backend, vanilla JS frontend, Groq (LLM), Deepgram (STT), ElevenLabs (TTS), WebRTC VAD, SQLite via async SQLAlchemy 2.0 + Alembic.

## Commands

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime + test + lint deps
python -m backend.main                # dev server on :8000 with reload
pytest                                # unit + API tests (no network, temp SQLite per test)
ruff check . && ruff format .         # lint + format; CI fails on either
alembic upgrade head                  # apply migrations (the app also does this at startup)
alembic revision --autogenerate -m "..."   # after editing backend/db/models.py; review the file
docker build -t interview-bot . && docker run --env-file .env -p 8000:8000 interview-bot
```

Config comes from `.env` via `backend/config.py` (pydantic-settings, unknown keys ignored). See `.env.example`. Never print or commit `.env` values.

## Layout

- `backend/main.py`: `create_app` plus `lifespan` (runs migrations, builds the engine, stores the sessionmaker on `app.state.db_sessionmaker`, marks leftover `in_progress` interviews abandoned).
- `backend/logging_config.py`: `setup_logging`, the `interview_id_var` contextvar and `interview_context()`. A handler filter stamps the ID on every record.
- `backend/api/routes.py`: index page and `/health` (includes a DB ping).
- `backend/api/interviews.py`: REST resource `/api/interviews` (create via multipart upload, list, detail, delete). `schemas.py` holds the response models.
- `backend/api/websocket.py`: `/ws/interviews/{id}`. Claims the interview atomically, runs the receive loop, and in `finally` always records how it ended. `AudioProcessor` drains audio off the socket.
- `backend/db/`: `models.py` (tables and enums), `engine.py` (engine, SQLite pragmas, `get_db`/`get_sessionmaker` dependencies), `repository.py` (every query), `migrations.py` (programmatic `alembic upgrade`).
- `backend/interview/session.py`: `InterviewSession`, the per-interview state machine tying STT, VAD, LLM and TTS together. Calls the recorder at key moments.
- `backend/interview/recorder.py`: `InterviewRecorder`, which writes turns, LLM calls, events and the final status for one interview. Short session per write, and it never raises.
- `backend/interview/prompts.py`: all LLM prompt text, including the versioned evaluation rubric.
- `backend/interview/evaluation.py`: scoring rules (`rating_for`, `score_evaluation`) and the background grading task.
- `backend/services/`: thin provider wrappers (`llm.py`, `stt.py`, `tts.py`, `vad.py`, `resume_parser.py`). Keep provider SDK calls inside these modules, and keep them free of DB code (`llm.py` returns an `LLMResult` with metadata; the caller records it).
- `frontend/js/app.js`: form upload, mic capture as 16 kHz Int16 PCM, MP3 playback, WebSocket client.

## Interview lifecycle

`created` (upload) → `in_progress` (WebSocket claims it with `UPDATE ... WHERE status='created'`) → `completed` (time limit or candidate clicked End) or `abandoned` (disconnect, error, server restart). Unstarted links older than `INTERVIEW_LINK_TTL_MINUTES` become `expired` when someone tries to open them. Interviews are never deleted automatically; the resume is saved as `uploads/{id}.pdf`.

## Turn-taking flow

`InterviewSession.state` is a three-state machine (`TurnState`):

- **LISTENING** (candidate's turn): PCM frames go to the VAD. After `SILENCE_THRESHOLD_SECONDS` of silence following speech, the state switches to THINKING synchronously (so a second silence can't start a second reply) and `_respond()` runs.
- **THINKING**: `_take_candidate_turn()` waits up to 1 s for Deepgram to finalize the last words, then the LLM runs, then `_speak()`: sentence 1 is synthesized, then `ai_response` (text, `response_id`, `chunks`) and the first `ai_audio` are sent and the state becomes SPEAKING; later sentences are synthesized and sent while earlier ones play. Sending stops if the candidate interrupts. Speech arriving now is kept for the next turn; it is not an interruption.
- **SPEAKING**: a transcript of at least `BARGE_IN_MIN_WORDS` words interrupts: `stop_ai_audio`, the interviewer turn is marked interrupted, and the state goes back to LISTENING. The client echoes `response_id` in `ai_audio_completed`; completions for any other ID are ignored.

Every chunk always goes to Deepgram. Only `is_final` results are accumulated (`_final_parts`); an interim result replaces the previous interim (`_interim`) and is never appended.

Groq calls are blocking and always run through `asyncio.to_thread`. TTS is async (`httpx.AsyncClient`).

## Evaluation

When the WebSocket closes (any end reason), `begin_evaluation()` creates a `pending` row, then grades in a background task (references kept in `evaluation._running`). `CandidateEvaluator` sends the full transcript with `EVALUATION_SYSTEM_PROMPT` and a strict `json_schema` response format generated from `EvaluatorOutput`. `score_evaluation()` computes the overall score and rating in code. The frontend polls `GET /api/interviews/{id}/evaluation` every 2 s and renders it with `textContent` only. Bump `EVALUATION_PROMPT_VERSION` when the rubric changes.

## Gotchas

- **The pinned Deepgram SDK is load-bearing.** `deepgram-sdk==2.12` uses the old v2 API (`Deepgram(...).transcription.live`); upgrading means rewriting `stt.py`. ElevenLabs is called over REST, so there is no SDK.
- **Keep-alive matters for latency.** httpx drops idle connections after 5 s by default, shorter than a candidate's answer, so each turn would pay a new TLS handshake (~200–400 ms). The Groq and ElevenLabs clients set `keepalive_expiry=300`.
- Groq strict JSON schema mode requires `additionalProperties: false` on every object, which is why evaluator models use `extra="forbid"`. Set `max_tokens` generously: reasoning tokens count against it, and truncated JSON fails validation.
- `webrtcvad` imports `pkg_resources`, so `setuptools<81` is required.
- Starlette 0.27's `TestClient` breaks on httpx 0.28, so dev requirements pin `httpx==0.27.2`.
- The default Groq model `openai/gpt-oss-120b` is a reasoning model: hidden reasoning counts against `max_tokens`. Without `LLM_REASONING_EFFORT=low`, replies get cut off mid-sentence. Set it empty for non-reasoning models. Groq retires models often, so check `client.models.list()` if you get a 404.
- ElevenLabs free-tier API keys can't use library voices, only default ones. The default voice is `EXAVITQu4vr4xnSDxMaL`.
- Async SQLAlchemy can't lazy-load. Relationships use `lazy="raise"`, so load them with `selectinload` (see `repository.get_interview(with_details=True)`). The sessionmaker uses `expire_on_commit=False`; don't call `expire_all()` and then touch attributes (it raises `MissingGreenlet`). In tests, read back through a fresh session (`reload()` in `test_repository.py`).
- SQLite needs `PRAGMA foreign_keys=ON` per connection, or `ON DELETE CASCADE` silently does nothing. `engine.py` sets it together with WAL mode.
- Alembic's `env.py` runs `asyncio.run`, so `upgrade_to_head` must be called from a thread (`asyncio.to_thread`) inside async code. The app's call sets `configure_logger=False` so Alembic's `fileConfig` doesn't wipe the app's log handlers.
- Alembic uses `render_as_batch=True` because SQLite can't `ALTER` columns. `UTCDateTime` renders as `sa.DateTime(timezone=True)` in migrations. `tests/test_migrations.py` fails if the models and migrations drift apart.
- All datetimes are UTC and timezone-aware (`models.utcnow()`); `UTCDateTime` rejects naive values.
- Live interview state (sockets, VAD, flags) is in-process only, so run a single worker. Startup marks any `in_progress` rows as abandoned, which would be wrong with several workers.

## Known issues (not yet fixed)

- Speech during THINKING waits until the candidate's next turn instead of cancelling the in-flight reply, so a long pause mid-answer can still produce a reply to the first half.
- Echo from speakers (no headphones) can trigger barge-in if it transcribes to 2+ words.
- `/api/interviews` has no authentication, so anyone who can reach the server can list candidates' names, emails and transcripts.
- The frontend captures audio with the deprecated `ScriptProcessorNode` (works everywhere; `AudioWorklet` is the replacement).

## Conventions

- Use `logging` (module-level `logger`), never `print`.
- Frontend: render any user- or LLM-provided text with `textContent` (the `el()` helper), never `innerHTML`.
- No blocking I/O inside `async def`: use `anyio` for files and `asyncio.to_thread` for sync SDKs (ruff's `ASYNC` rules catch most cases).
- Put new settings in `Settings` with a default and document them in `.env.example` and the README config table.
- Tests mock provider clients and never hit the network. Keep it that way.
- New queries go in `repository.py`; routes and the session never build SQL themselves.
- Schema changes: edit `models.py`, run `alembic revision --autogenerate`, review the generated file, commit both.
- Pass `interview_id` through `interview_context()`, not as a log argument; tasks and threads started inside inherit it.
