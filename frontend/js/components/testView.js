/**
 * testView.js — Book-level generated tests and quiz sessions.
 *
 * Exported:
 *   renderTestView(container, { book, onBack, onBookLoaded })
 */

import {
  api,
  generateBookTests,
  getBookTestAvailability,
  sampleBookTests,
  submitBookTests,
} from "../api.js";
import { showToast } from "../app.js";

/**
 * @param {HTMLElement} container
 * @param {object} opts
 * @param {object} opts.book
 * @param {(bookId?: number) => void} opts.onBack
 * @param {(book: object) => void} opts.onBookLoaded
 */
export async function renderTestView(container, { book, onBack, onBookLoaded }) {
  container.innerHTML = `
    <div class="loading-spinner">
      <div class="spinner"></div>
      <p>Loading tests…</p>
    </div>
  `;

  let loadedBook = book;
  let availability;
  try {
    loadedBook = await api.get(`/api/books/${book.id}`);
    availability = await getBookTestAvailability(book.id);
    onBookLoaded?.(loadedBook);
  } catch (err) {
    container.innerHTML = `
      <div class="card" style="color:var(--color-danger)">
        <strong>Could not load tests:</strong> ${escHtml(err.detail || err.message)}
      </div>
      <div class="view-actions">
        <button class="btn-ghost" id="btn-back-err">← Back to Book</button>
      </div>`;
    container.querySelector("#btn-back-err")?.addEventListener("click", () => onBack(book.id));
    return;
  }

  const state = {
    book: loadedBook,
    availability,
    sample: null,
    result: null,
    answers: new Map(),
    controls: controlsFromGenerationState(availability),
  };

  render(container, state, onBack);
}

function render(container, state, onBack) {
  container.innerHTML = buildTestViewHTML(state);

  container.querySelector("#btn-back")?.addEventListener("click", () => onBack(state.book.id));

  container.querySelector("#btn-generate-tests")?.addEventListener("click", async () => {
    await handleGenerate(container, state, onBack);
  });

  container.querySelector("#btn-start-quiz")?.addEventListener("click", async () => {
    await handleStartQuiz(container, state, onBack);
  });

  container.querySelector("#btn-submit-quiz")?.addEventListener("click", async () => {
    await handleSubmitQuiz(container, state, onBack);
  });

  container.querySelector("#btn-reset-quiz")?.addEventListener("click", () => {
    state.sample = null;
    state.result = null;
    state.answers = new Map();
    render(container, state, onBack);
  });

  container.querySelectorAll("input[type='radio'][data-question-id]").forEach((input) => {
    input.addEventListener("change", () => {
      state.answers.set(Number(input.dataset.questionId), Number(input.value));
      updateSubmitState(container, state);
    });
  });

  updateSubmitState(container, state);
}

async function handleGenerate(container, state, onBack) {
  const controls = readControls(container);
  state.controls = controls;
  const btn = container.querySelector("#btn-generate-tests");
  btn.disabled = true;
  btn.textContent = "Generating…";

  try {
    const result = await generateBookTests(state.book.id, {
      chapter_id: controls.chapterId,
      target_count: controls.targetCount,
      replace_existing: true,
    });
    state.availability = {
      book_id: state.book.id,
      total_questions: result.total_questions,
      chapters: result.chapters,
      generation_state: result.generation_state,
    };
    state.sample = null;
    state.result = null;
    state.answers = new Map();
    showToast(`Generated ${result.generated_questions} test question(s).`, "success");
    render(container, state, onBack);
  } catch (err) {
    showToast(`Test generation failed: ${err.detail || err.message}`, "error", 7000);
    btn.disabled = false;
    btn.textContent = "Generate tests";
  }
}

async function handleStartQuiz(container, state, onBack) {
  const controls = readControls(container);
  state.controls = controls;
  const btn = container.querySelector("#btn-start-quiz");
  btn.disabled = true;
  btn.textContent = "Sampling…";

  try {
    state.sample = await sampleBookTests(state.book.id, {
      chapter_id: controls.chapterId,
      sample_size: controls.sampleSize,
    });
    state.result = null;
    state.answers = new Map();
    render(container, state, onBack);
  } catch (err) {
    showToast(`Could not start quiz: ${err.detail || err.message}`, "error", 7000);
    btn.disabled = false;
    btn.textContent = "Start quiz";
  }
}

async function handleSubmitQuiz(container, state, onBack) {
  syncAnswersFromDOM(container, state);
  const questions = state.sample?.questions || [];
  const unanswered = questions.filter((question) => !state.answers.has(Number(question.id)));
  if (!questions.length || unanswered.length) {
    showToast(`Answer all quiz questions before submitting (${questions.length - unanswered.length}/${questions.length}).`, "error", 5000);
    updateSubmitState(container, state);
    return;
  }

  const answers = questions.map((question) => ({
    question_id: question.id,
    option_id: state.answers.get(Number(question.id)),
  }));

  const btn = container.querySelector("#btn-submit-quiz");
  btn.disabled = true;
  btn.textContent = "Checking…";

  try {
    state.result = await submitBookTests(state.book.id, answers);
    render(container, state, onBack);
  } catch (err) {
    showToast(`Quiz submission failed: ${err.detail || err.message}`, "error", 7000);
    btn.disabled = false;
    btn.textContent = "Submit answers";
  }
}

function buildTestViewHTML(state) {
  const chapters = state.availability?.chapters || [];
  const completedWithTranscripts = chapters.filter((chapter) => chapter.has_transcription && chapter.recording_status === "completed");
  const totalQuestions = state.availability?.total_questions || 0;
  const sample = state.sample;
  const result = state.result;
  const controls = state.controls || defaultControls(totalQuestions);
  const generationState = state.availability?.generation_state || null;

  return `
    <div class="test-view">
      <div class="test-view-header">
        <div>
          <h2>🧠 Tests: ${escHtml(state.book.title)}</h2>
          <div class="sub">${totalQuestions} generated question(s) · ${completedWithTranscripts.length} completed chapter(s) available</div>
        </div>
      </div>

      <section class="card test-controls-card">
        <div class="test-controls-grid">
          <label>
            Chapter scope
            <select id="test-chapter-select">
              <option value="" ${controls.chapterId ? "" : "selected"}>All completed chapters</option>
              ${completedWithTranscripts.map((chapter) => `
                <option value="${chapter.chapter_id}" ${controls.chapterId === chapter.chapter_id ? "selected" : ""}>
                  ${escHtml(chapterLabel(chapter))} · ${chapter.question_count} question(s)
                </option>`).join("")}
            </select>
          </label>
          <label>
            Questions to generate per chapter
            <input type="number" min="1" max="30" value="${controls.targetCount}" id="test-target-count" />
          </label>
          <label>
            Quiz sample size
            <input type="number" min="1" max="100" value="${controls.sampleSize}" id="test-sample-size" />
          </label>
        </div>
        <div class="test-control-actions">
          <button class="btn-primary" id="btn-generate-tests" ${completedWithTranscripts.length ? "" : "disabled"}>Generate tests</button>
          <button class="btn-warning" id="btn-start-quiz" ${totalQuestions ? "" : "disabled"}>Start quiz</button>
        </div>
        <p class="test-help-text">Questions are generated from stored chapter transcriptions and should focus on core ideas, not tiny details.</p>
        ${generationState ? buildGenerationStateHTML(generationState, chapters) : ""}
      </section>

      ${buildAvailabilityHTML(chapters)}
      ${sample ? buildQuizHTML(sample, state.answers, result) : buildEmptyQuizHTML(totalQuestions)}

      <div class="view-actions">
        <button class="btn-ghost" id="btn-back">← Back to Book</button>
      </div>
    </div>
  `;
}

function buildAvailabilityHTML(chapters) {
  if (!chapters.length) {
    return `
      <div class="empty-state test-empty-state">
        <div class="empty-icon">📚</div>
        <h3>No chapters yet</h3>
        <p>Process at least one recording before generating tests.</p>
      </div>`;
  }

  return `
    <section class="test-availability-list">
      ${chapters.map((chapter) => `
        <article class="test-availability-card ${chapter.question_count ? "has-tests" : ""}">
          <div>
            <strong>${escHtml(chapterLabel(chapter))}</strong>
            <div class="text-muted text-sm">Recording #${chapter.recording_id} · ${escHtml(chapter.recording_status)}</div>
          </div>
          <span class="test-count-pill">${chapter.question_count} test(s)</span>
        </article>`).join("")}
    </section>
  `;
}

function buildEmptyQuizHTML(totalQuestions) {
  return `
    <section class="card test-session-card">
      <h3>Quiz session</h3>
      <p class="text-muted">${totalQuestions ? "Choose a sample size and start a quiz." : "Generate tests first, then start a quiz sample."}</p>
    </section>`;
}

function buildQuizHTML(sample, answers, result) {
  const resultByQuestion = new Map((result?.results || []).map((item) => [item.question_id, item]));
  const scoreHTML = result ? `
    <div class="test-score-card ${result.score_percent >= 70 ? "is-good" : "is-low"}">
      <strong>${result.correct}/${result.total}</strong>
      <span>${result.score_percent}% correct</span>
    </div>` : "";

  return `
    <section class="test-session-card">
      <div class="test-session-heading">
        <div>
          <h3>Quiz session</h3>
          <p>${sample.returned_size} question(s) sampled${sample.chapter_id ? " from one chapter" : " across the book"}.</p>
        </div>
        ${scoreHTML}
      </div>

      <div class="test-question-list">
        ${sample.questions.map((question, index) => buildQuestionHTML(question, index, answers, resultByQuestion.get(question.id))).join("")}
      </div>

      <div class="test-session-actions">
        ${result ? "" : `<button class="btn-primary" id="btn-submit-quiz" type="button">Submit answers</button>`}
        <button class="btn-ghost" id="btn-reset-quiz">New sample</button>
      </div>
    </section>`;
}

function buildQuestionHTML(question, index, answers, result) {
  const selectedId = answers.get(question.id);
  const explanationText = result?.is_correct ? result.explanation : (result?.wrong_explanation || result?.explanation);
  return `
    <article class="test-question-card ${result ? (result.is_correct ? "is-correct" : "is-wrong") : ""}">
      <div class="test-question-meta">
        <span class="chunk-badge">Question ${index + 1}</span>
        <span>${escHtml(question.difficulty)}</span>
        ${question.concept_tags ? `<span>${escHtml(question.concept_tags)}</span>` : ""}
      </div>
      <h4>${escHtml(question.question_text)}</h4>
      <div class="test-option-list">
        ${(question.options || []).map((option) => buildOptionHTML(question, option, selectedId, result)).join("")}
      </div>
      ${result ? `
        <div class="test-explanation ${result.is_correct ? "is-correct" : "is-wrong"}">
          <strong>${result.is_correct ? "Correct" : "Why your answer is wrong"}:</strong> ${escHtml(explanationText)}
        </div>` : ""}
    </article>`;
}

function buildOptionHTML(question, option, selectedId, result) {
  const isSelected = selectedId === option.id;
  const isCorrect = result?.correct_option_id === option.id;
  const stateClass = result ? (isCorrect ? "is-correct" : (isSelected ? "is-wrong" : "")) : "";
  return `
    <label class="test-option ${stateClass}">
      <input type="radio" name="question-${question.id}" data-question-id="${question.id}" value="${option.id}" ${isSelected ? "checked" : ""} ${result ? "disabled" : ""} />
      <span>${escHtml(option.option_text)}</span>
    </label>`;
}

function readControls(container) {
  const chapterRaw = container.querySelector("#test-chapter-select")?.value || "";
  const targetRaw = container.querySelector("#test-target-count")?.value || "10";
  const sampleRaw = container.querySelector("#test-sample-size")?.value || "10";
  return {
    chapterId: chapterRaw ? Number(chapterRaw) : null,
    targetCount: clampInt(targetRaw, 1, 30, 10),
    sampleSize: clampInt(sampleRaw, 1, 100, 10),
  };
}

function defaultControls(totalQuestions = 0) {
  return {
    chapterId: null,
    targetCount: 10,
    sampleSize: Math.min(Math.max(totalQuestions || 10, 1), 10),
  };
}

function controlsFromGenerationState(availability) {
  const generationState = availability?.generation_state;
  if (!generationState) return defaultControls(availability?.total_questions || 0);
  return {
    chapterId: generationState.chapter_id || null,
    targetCount: clampInt(generationState.target_count, 1, 30, 10),
    sampleSize: Math.min(Math.max(availability?.total_questions || 10, 1), 10),
  };
}

function buildGenerationStateHTML(generationState, chapters) {
  const chapter = chapters.find((item) => item.chapter_id === generationState.chapter_id);
  const scope = chapter ? chapterLabel(chapter) : "all completed chapters";
  const completedAt = generationState.completed_at ? formatDateTime(generationState.completed_at) : null;
  const updatedAt = generationState.updated_at ? formatDateTime(generationState.updated_at) : null;
  return `
    <div class="test-generation-state ${escHtml(generationState.status)}">
      <strong>Generation state:</strong>
      <span>${escHtml(generationState.status)}</span>
      <span>scope: ${escHtml(scope)}</span>
      <span>target: ${generationState.target_count}/chapter</span>
      <span>generated: ${generationState.generated_questions}</span>
      ${completedAt ? `<span>completed: ${escHtml(completedAt)}</span>` : ""}
      ${!completedAt && updatedAt ? `<span>updated: ${escHtml(updatedAt)}</span>` : ""}
      ${generationState.error_message ? `<span class="test-generation-error">${escHtml(generationState.error_message)}</span>` : ""}
    </div>`;
}

function updateSubmitState(container, state) {
  const btn = container.querySelector("#btn-submit-quiz");
  if (!btn || state.result) return;
  const questions = state.sample?.questions || [];
  syncAnswersFromDOM(container, state);
  const answeredCount = questions.filter((question) => state.answers.has(Number(question.id))).length;
  if (questions.length === 0) {
    btn.setAttribute("disabled", "disabled");
  } else {
    btn.disabled = false;
    btn.removeAttribute("disabled");
    btn.classList.remove("disabled");
    btn.removeAttribute("aria-disabled");
  }
  btn.textContent = answeredCount === questions.length
    ? "Submit answers"
    : `Submit answers (${answeredCount}/${questions.length})`;
}

function syncAnswersFromDOM(container, state) {
  container.querySelectorAll("input[type='radio'][data-question-id]:checked").forEach((input) => {
    state.answers.set(Number(input.dataset.questionId), Number(input.value));
  });
}

function chapterLabel(chapter) {
  return chapter.chapter_title || `Chapter ${chapter.chapter_number}`;
}

function clampInt(value, min, max, fallback) {
  const parsed = parseInt(value, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, parsed));
}

function formatDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function escHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
