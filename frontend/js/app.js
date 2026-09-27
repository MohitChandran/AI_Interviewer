// Configuration
const API_BASE = window.location.origin;
const WS_PROTOCOL = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_BASE = `${WS_PROTOCOL}//${window.location.host}`;

// State
let interviewId = null;
let websocket = null;
let audioStream = null;
let audioContext = null;
let processor = null;
let source = null;
let isInterviewActive = false;
let candidateName = '';
let currentAudio = null;
let currentResponseId = null; // reply whose audio is playing; the server needs it back when done
let audioTimeout = null;
// A reply arrives as a text header plus one audio chunk per sentence, played back to back.
let playback = null; // { total, chunks: [], next, playing }

// DOM Elements
const candidateForm = document.getElementById('candidateForm');
const formSection = document.getElementById('formSection');
const interviewSection = document.getElementById('interviewSection');
const uploadStatus = document.getElementById('uploadStatus');
const chatWindow = document.getElementById('chatWindow');
const statusText = document.getElementById('statusText');
const statusIndicator = document.getElementById('statusIndicator');
const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const evaluationSection = document.getElementById('evaluationSection');
const evaluationBody = document.getElementById('evaluationBody');

// FastAPI returns a string for HTTPException and a list of field errors for validation failures.
function formatApiError(detail) {
    if (!detail) return 'Upload failed';
    if (typeof detail === 'string') return detail;
    return detail.map((d) => `${d.loc[d.loc.length - 1]}: ${d.msg}`).join(', ');
}

// Form Submission Handler
candidateForm.addEventListener('submit', async (e) => {
    e.preventDefault();

    const formData = new FormData();
    const name = document.getElementById('name').value.trim();
    const email = document.getElementById('email').value.trim();
    const role = document.getElementById('role').value.trim();
    const resume = document.getElementById('resume').files[0];

    candidateName = name;

    formData.append('name', name);
    formData.append('email', email);
    formData.append('role', role);
    formData.append('resume', resume);

    try {
        uploadStatus.textContent = 'Uploading resume...';
        uploadStatus.className = 'status-message info';
        document.getElementById('uploadBtn').disabled = true;

        const response = await fetch(`${API_BASE}/api/interviews`, {
            method: 'POST',
            body: formData,
        });

        const result = await response.json();

        if (response.ok) {
            interviewId = result.interview_id;
            uploadStatus.textContent = 'Upload successful! Ready to start interview.';
            uploadStatus.className = 'status-message success';

            // Show interview section
            setTimeout(() => {
                formSection.style.display = 'none';
                interviewSection.style.display = 'block';
            }, 1000);
        } else {
            throw new Error(formatApiError(result.detail));
        }
    } catch (error) {
        console.error('Upload error:', error);
        uploadStatus.textContent = `Error: ${error.message}`;
        uploadStatus.className = 'status-message error';
        document.getElementById('uploadBtn').disabled = false;
    }
});

// Start Interview
startBtn.addEventListener('click', async () => {
    try {
        updateStatus('Requesting microphone access...', 'warning');
        audioStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                echoCancellation: true,
                noiseSuppression: true,
                sampleRate: 16000,
                channelCount: 1,
            },
        });

        updateStatus('Connecting...', 'warning');
        connectWebSocket();

        startAudioStreaming();

        startBtn.style.display = 'none';
        stopBtn.style.display = 'inline-block';
    } catch (error) {
        console.error('Microphone access error:', error);
        addChatMessage('system', `Error: Could not access microphone. ${error.message}`);
        updateStatus('Error', 'error');
    }
});

// Stop Interview
stopBtn.addEventListener('click', () => {
    endInterview();
});

// WebSocket Connection
function connectWebSocket() {
    const wsUrl = `${WS_BASE}/ws/interviews/${interviewId}`;
    console.log('Connecting to WebSocket:', wsUrl);

    websocket = new WebSocket(wsUrl);

    websocket.onopen = () => {
        console.log('WebSocket connected');
        updateStatus('Connected - Starting interview...', 'success');
        isInterviewActive = true;
    };

    websocket.onmessage = async (event) => {
        try {
            const data = JSON.parse(event.data);
            console.log('Received message:', data.type);

            if (data.type === 'ai_response') {
                handleAIResponse(data);
            } else if (data.type === 'candidate_transcript') {
                addChatMessage('candidate', data.text);
            } else if (data.type === 'ai_audio') {
                handleAudioChunk(data);
            } else if (data.type === 'error') {
                addChatMessage('system', `Error: ${data.message}`);
                updateStatus('Error', 'error');
            } else if (data.type === 'interview_end') {
                handleInterviewEnd(data);
            } else if (data.type === 'stop_ai_audio') {
                // The candidate interrupted: cut the reply off and hand the turn back.
                finishPlayback();
            }
        } catch (error) {
            console.error('Error processing message:', error);
        }
    };

    websocket.onerror = (error) => {
        console.error('WebSocket error:', error);
        updateStatus('Connection error', 'error');
    };

    websocket.onclose = () => {
        console.log('WebSocket closed');
        if (isInterviewActive) {
            updateStatus('Disconnected', 'error');
        }
    };
}

// Start Audio Streaming using Web Audio API raw PCM capture
function startAudioStreaming() {
    if (!audioStream) return;

    try {
        audioContext = new AudioContext({ sampleRate: 16000 });
        source = audioContext.createMediaStreamSource(audioStream);

        // 2048 samples = 128 ms per message at 16 kHz: small enough for responsive turn-taking.
        processor = audioContext.createScriptProcessor(2048, 1, 1);

        processor.onaudioprocess = (e) => {
            const inputBuffer = e.inputBuffer.getChannelData(0);
            // Convert Float32Array [-1..1] to Int16 PCM little endian bytes
            const buffer = new ArrayBuffer(inputBuffer.length * 2);
            const view = new DataView(buffer);
            for (let i = 0; i < inputBuffer.length; i++) {
                let s = Math.max(-1, Math.min(1, inputBuffer[i]));
                view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
            }

            if (websocket && websocket.readyState === WebSocket.OPEN) {
                websocket.send(buffer);
            }
        };

        source.connect(processor);
        processor.connect(audioContext.destination);

        console.log('Audio streaming started (raw PCM)');
        updateStatus('Listening...', 'listening');
    } catch (error) {
        console.error('Audio streaming error:', error);
        addChatMessage('system', `Audio streaming error: ${error.message}`);
    }
}

function sendJson(message) {
    if (websocket && websocket.readyState === WebSocket.OPEN) {
        websocket.send(JSON.stringify(message));
    }
}

function stopAudioElement() {
    clearTimeout(audioTimeout);
    audioTimeout = null;
    if (currentAudio) {
        currentAudio.onended = null;
        currentAudio.onerror = null;
        currentAudio.pause();
        currentAudio.src = '';
        currentAudio = null;
    }
}

// Ends the current reply (played to the end, failed, timed out or interrupted) and tells the
// server exactly once, tagged with the reply's ID so a late message can't end a newer reply.
function finishPlayback() {
    if (currentResponseId === null) return;
    const responseId = currentResponseId;
    currentResponseId = null;
    playback = null;
    stopAudioElement();
    if (isInterviewActive) {
        updateStatus('Listening...', 'listening');
        sendJson({ type: 'ai_audio_completed', response_id: responseId });
    }
}

function playAudio(base64Audio, onDone) {
    if (!base64Audio) {
        onDone();
        return;
    }
    const audio = new Audio(`data:audio/mpeg;base64,${base64Audio}`);
    currentAudio = audio;
    audio.onended = onDone;
    audio.onerror = (error) => {
        console.error('Audio playback error:', error);
        onDone();
    };
    // Safety net in case the browser never reports the end of playback.
    audioTimeout = setTimeout(onDone, 60000);
    audio.play().catch((error) => {
        console.error('Failed to play audio:', error);
        onDone();
    });
}

function handleAIResponse(data) {
    addChatMessage('ai', data.text);
    stopAudioElement();
    currentResponseId = data.response_id;
    playback = { total: data.chunks, chunks: [], next: 0, playing: false };
    updateStatus('AI speaking...', 'speaking');
}

function handleAudioChunk(data) {
    // Chunks of a reply that was already interrupted or replaced are dropped.
    if (!playback || data.response_id !== currentResponseId) return;
    playback.chunks[data.index] = data.audio;
    playNextChunk();
}

function playNextChunk() {
    if (!playback || playback.playing) return;
    if (playback.next >= playback.total) {
        finishPlayback();
        return;
    }
    const audio = playback.chunks[playback.next];
    if (audio === undefined) return; // not arrived yet; handleAudioChunk calls back in
    playback.next += 1;
    playback.playing = true;
    const current = playback;
    playAudio(audio, () => {
        if (playback !== current) return;
        stopAudioElement();
        playback.playing = false;
        playNextChunk();
    });
}

// Time limit reached: stop listening, play the goodbye, then close the session.
function handleInterviewEnd(data) {
    isInterviewActive = false;
    stopMicrophone();
    currentResponseId = null;
    playback = null;
    stopAudioElement();
    addChatMessage('ai', data.text);
    updateStatus('Wrapping up...', 'speaking');
    playAudio(data.audio, () => {
        stopAudioElement();
        endInterview();
    });
}

function stopMicrophone() {
    if (processor) processor.disconnect();
    if (source) source.disconnect();
    if (audioContext) audioContext.close();
    if (audioStream) audioStream.getTracks().forEach((track) => track.stop());
    processor = source = audioContext = audioStream = null;
}

function endInterview() {
    // Tell the server the candidate ended it, so it is recorded as completed, not abandoned.
    if (isInterviewActive) sendJson({ type: 'stop' });
    isInterviewActive = false;

    stopMicrophone();
    if (websocket) websocket.close();
    currentResponseId = null;
    stopAudioElement();

    updateStatus('Interview Complete', 'complete');
    stopBtn.style.display = 'none';
    addChatMessage('system', 'Interview session ended. Your evaluation is being prepared below.');
    loadEvaluation();
}

// ---- Evaluation -------------------------------------------------------------
// Everything below renders LLM-generated text, so it only ever uses textContent.

const RATING_LABELS = {
    good: 'Good', ok: 'OK', should_improve: 'Should Improve', bad: 'Bad', insufficient_data: 'Not enough data',
};
const VERDICT_LABELS = {
    correct: 'Correct', partially_correct: 'Partially correct', incorrect: 'Incorrect',
    unanswered: 'Unanswered', not_applicable: 'Open-ended',
};
const RECOMMENDATION_LABELS = { advance: 'Advance', hold: 'Hold', reject: 'Not ready yet' };
const EVALUATION_POLL_MS = 2000;
const EVALUATION_TIMEOUT_MS = 120000;
let evaluationRequested = false;

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

// The server evaluates in the background after the interview; poll until it's no longer pending.
async function loadEvaluation() {
    if (evaluationRequested || !interviewId) return;
    evaluationRequested = true;
    evaluationSection.hidden = false;
    evaluationBody.replaceChildren(
        el('p', 'evaluation-pending', 'Generating your evaluation… this usually takes 10–20 seconds.')
    );
    evaluationSection.scrollIntoView({ behavior: 'smooth' });

    const deadline = Date.now() + EVALUATION_TIMEOUT_MS;
    while (Date.now() < deadline) {
        try {
            const response = await fetch(`${API_BASE}/api/interviews/${interviewId}/evaluation`);
            if (response.ok) {
                const evaluation = await response.json();
                if (evaluation.status !== 'pending') {
                    renderEvaluation(evaluation);
                    return;
                }
            }
        } catch (error) {
            console.error('Evaluation poll failed:', error);
        }
        await new Promise((resolve) => setTimeout(resolve, EVALUATION_POLL_MS));
    }
    evaluationBody.replaceChildren(
        el('p', 'status-message error', 'The evaluation is taking longer than expected. Please check back later.')
    );
}

function renderEvaluation(ev) {
    if (ev.status === 'failed') {
        evaluationBody.replaceChildren(el('p', 'status-message error', 'Sorry, the evaluation could not be generated.'));
        return;
    }

    const header = el('div', 'evaluation-header');
    header.append(el('span', `rating-badge rating-${ev.rating}`, RATING_LABELS[ev.rating] || ev.rating));
    if (ev.overall_score !== null) {
        const overall = el('div', 'overall-score');
        overall.append(el('span', 'overall-value', String(ev.overall_score)), el('span', 'overall-max', '/ 100'));
        header.append(overall);
    }
    const parts = [header, el('p', 'evaluation-summary', ev.summary)];

    if (ev.rating !== 'insufficient_data') {
        const bars = el('div', 'score-bars');
        bars.append(scoreBar('Technical', ev.technical_score), scoreBar('Communication', ev.communication_score));

        const stats = el('div', 'evaluation-stats');
        stats.append(
            stat(ev.questions_asked, 'Questions asked'),
            stat(ev.questions_answered, 'Answered'),
            stat(ev.answered_correctly, 'Correct'),
            stat(RECOMMENDATION_LABELS[ev.recommendation], 'Recommendation'),
        );

        const lists = el('div', 'evaluation-lists');
        lists.append(
            bulletList('Strengths', ev.strengths, 'strengths'),
            bulletList('Areas to improve', ev.weaknesses, 'weaknesses'),
        );
        parts.push(bars, stats, lists);

        if (ev.questions.length) parts.push(questionBreakdown(ev.questions));
    }
    evaluationBody.replaceChildren(...parts);
}

function scoreBar(label, value) {
    const row = el('div', 'score-row');
    const track = el('div', 'score-track');
    const fill = el('div', 'score-fill');
    fill.style.width = `${(value || 0) * 10}%`;
    track.append(fill);
    row.append(el('span', 'score-label', label), track, el('span', 'score-value', `${value}/10`));
    return row;
}

function stat(value, label) {
    const box = el('div', 'stat');
    box.append(el('span', 'stat-value', String(value ?? '–')), el('span', 'stat-label', label));
    return box;
}

function bulletList(title, items, className) {
    const box = el('div', `evaluation-list ${className}`);
    const list = el('ul');
    (items || []).forEach((item) => list.append(el('li', null, item)));
    box.append(el('h3', null, title), list);
    return box;
}

function questionBreakdown(questions) {
    const section = el('div', 'question-breakdown');
    section.append(el('h3', null, 'Question breakdown'));
    questions.forEach((q) => {
        const top = el('div', 'question-top');
        top.append(
            el('span', 'question-text', `${q.seq}. ${q.question}`),
            el('span', `verdict verdict-${q.verdict}`, VERDICT_LABELS[q.verdict] || q.verdict),
            el('span', 'question-score', `${q.score}/5`),
        );
        const item = el('div', 'question-item');
        item.append(top, el('p', 'question-answer', q.answer_summary), el('p', 'question-feedback', q.feedback));
        section.append(item);
    });
    return section;
}

// Add Chat Message
function addChatMessage(type, text) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `chat-message ${type}`;

    const labels = { ai: 'Nikki (AI)', candidate: candidateName, system: 'System' };
    // textContent, never innerHTML: the name, the transcript and the LLM's text are all untrusted.
    messageDiv.append(el('strong', null, `${labels[type]}:`), document.createTextNode(` ${text}`));
    chatWindow.appendChild(messageDiv);
    chatWindow.scrollTop = chatWindow.scrollHeight;
}

// Update Status
function updateStatus(text, state) {
    statusText.textContent = text;
    statusIndicator.className = `status-indicator ${state}`;
}

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    if (isInterviewActive) {
        endInterview();
    }
});
