/**
 * bookDetail.js — Selected book dashboard.
 *
 * This view is intentionally shown before the recorder so previously saved
 * recordings, chapters, transcriptions, and summaries are never hidden behind
 * a new empty recording session.
 *
 * Exported:
 *   renderBookDetail(container, { bookId, onBack, onNewRecording, onViewResults })
 */

import { api, ApiError, processRecording } from "../api.js";
import { showToast } from "../app.js";

/**
 * @param {HTMLElement} container
 * @param {object} opts
 * @param {number} opts.bookId
 * @param {() => void} opts.onBack
 * @param {(book: object) => void} opts.onBookLoaded
 * @param {(book: object) => void} opts.onNewRecording
 * @param {(recordingId: number) => void} opts.onViewResults
 */
export async function renderBookDetail(
  container,
  options
) {
  const { bookId, onBack, onBookLoaded, onNewRecording, onViewResults } = options;

  container.innerHTML = `
    <div class="loading-spinner">
      <div class="spinner"></div>
      <p>Loading book results…</p>
    </div>
  `;

  let book;
  try {
    book = await api.get(`/api/books/${bookId}`);
    book.recordings = await hydrateRecordingDetails(book.recordings || []);
    onBookLoaded?.(book);
  } catch (err) {
    container.innerHTML = `
      <div class="card" style="color:var(--color-danger)">
        <strong>Could not load book:</strong> ${escHtml(err.detail || err.message)}
      </div>
      <div class="view-actions">
        <button class="btn-ghost" id="btn-back-err">← Back to Books</button>
      </div>`;
    container.querySelector("#btn-back-err").addEventListener("click", onBack);
    return;
  }

  container.innerHTML = buildBookDetailHTML(book);

  container.querySelector("#btn-back")?.addEventListener("click", onBack);
  container.querySelector("#btn-new-recording")?.addEventListener("click", () => {
    onNewRecording(book);
  });

  container.querySelectorAll("[data-action='view-results']").forEach((btn) => {
    btn.addEventListener("click", () => {
      onViewResults(Number(btn.dataset.recordingId));
    });
  });

  container.querySelectorAll("[data-action='process-recording']").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const recordingId = Number(btn.dataset.recordingId);
      btn.disabled = true;
      btn.textContent = "Processing…";

      try {
        const result = await processRecording(recordingId, "ru");
        showToast(`Processing complete: ${result.chapters?.length ?? 0} chapter(s).`, "success");
        onViewResults(recordingId);
      } catch (err) {
        const detail = err instanceof ApiError ? err.detail : err.message;
        showToast(`Processing failed: ${detail}`, "error", 6000);
        btn.disabled = false;
        btn.textContent = "Process (Russian)";
      }
    });
  });

  container.querySelectorAll("[data-action='delete-recording']").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const recordingId = Number(btn.dataset.recordingId);
      const recording = findRecording(book, recordingId);

      if (!confirm(recordingDeleteMessage(recording))) return;

      btn.disabled = true;
      btn.textContent = "Deleting…";

      try {
        await api.delete(`/api/recordings/${recordingId}`);
        showToast(`Recording #${recordingId} deleted.`, "success");
        await renderBookDetail(container, options);
      } catch (err) {
        showToast(`Delete failed: ${err.detail || err.message}`, "error", 6000);
        btn.disabled = false;
        btn.textContent = "Delete";
      }
    });
  });

  container.querySelectorAll("[data-action='restart-recording']").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const recordingId = Number(btn.dataset.recordingId);
      const recording = findRecording(book, recordingId);

      if (!confirm(recordingRestartMessage(recording))) return;

      btn.disabled = true;
      btn.textContent = "Restarting…";

      try {
        await api.delete(`/api/recordings/${recordingId}`);
        showToast(`Recording #${recordingId} deleted. Starting a clean recording…`, "success");
        onNewRecording(book);
      } catch (err) {
        showToast(`Restart failed: ${err.detail || err.message}`, "error", 6000);
        btn.disabled = false;
        btn.textContent = "Restart";
      }
    });
  });
}

function buildBookDetailHTML(book) {
  const recordings = sortRecordings(book.recordings || []);

  return `
    <div class="book-detail">
      <div class="book-detail-header">
        <div>
          <h2>${escHtml(book.title)}</h2>
          ${book.author ? `<div class="author">by ${escHtml(book.author)}</div>` : ""}
          <div class="book-detail-meta">
            Added ${formatDate(book.created_at)} · ${recordings.length} recording(s)
          </div>
        </div>
        <button class="btn-primary" id="btn-new-recording">🎙 New Recording</button>
      </div>

      <section class="book-results-section">
        <div class="section-heading-row">
          <div>
            <h3>Existing Recordings &amp; Results</h3>
            <p>Previously saved chapters, transcriptions, and summaries stay accessible here.</p>
          </div>
        </div>
        ${buildRecordingsHTML(recordings)}
      </section>

      <div class="view-actions">
        <button class="btn-ghost" id="btn-back">← Back to Books</button>
      </div>
    </div>
  `;
}

function buildRecordingsHTML(recordings) {
  if (!recordings.length) {
    return `
      <div class="empty-state book-recordings-empty">
        <div class="empty-icon">🎙</div>
        <h3>No recordings yet</h3>
        <p>Use <strong>New Recording</strong> to create the first transcription and chapter summary for this book.</p>
      </div>
    `;
  }

  const cards = recordings.map((recording) => buildRecordingCardHTML(recording)).join("\n");
  return `<div class="recording-results-list">${cards}</div>`;
}

function buildRecordingCardHTML(recording) {
  const status = recording.status || "unknown";
  const duration = recording.duration_seconds ? formatDuration(recording.duration_seconds) : "N/A";
  const created = formatDateTime(recording.created_at);
  const processed = recording.processed_at ? formatDateTime(recording.processed_at) : null;
  const recordingTitle = deriveRecordingTitle(recording);
  const chapterPreview = buildChapterTitlePreviewHTML(recording);

  return `
    <article class="recording-result-card">
      <div class="recording-result-main">
        <div class="recording-result-title-row">
          <h4>${escHtml(recordingTitle)}</h4>
          <span class="status-badge ${escHtml(status)}">${escHtml(status)}</span>
        </div>

        ${chapterPreview}

        <div class="recording-result-meta">
          <span>Recording #${recording.id}</span>
          <span>Created ${escHtml(created)}</span>
          <span>Duration: ${escHtml(duration)}</span>
          ${processed ? `<span>Processed ${escHtml(processed)}</span>` : ""}
        </div>

        <p class="recording-result-note">${escHtml(statusDescription(status))}</p>
        ${buildInlineResultsHTML(recording)}
      </div>

      <div class="recording-result-actions">
        ${buildRecordingActionHTML(recording)}
      </div>
    </article>
  `;
}

function buildInlineResultsHTML(recording) {
  if (recording.status !== "completed") return "";

  if (recording.detail_error) {
    return `
      <div class="recording-inline-results is-error">
        Could not load stored chapters for this recording: ${escHtml(recording.detail_error)}
      </div>
    `;
  }

  const chapters = recording.chapters || [];
  if (!chapters.length) {
    return `
      <div class="recording-inline-results text-muted text-sm">
        No stored chapters returned for this completed recording.
      </div>
    `;
  }

  return `
    <div class="recording-inline-results">
      <h5>Stored Chapters &amp; Transcriptions</h5>
      <div class="inline-chapter-list">
        ${chapters.map((chapter) => buildInlineChapterHTML(chapter)).join("\n")}
      </div>
    </div>
  `;
}

function buildInlineChapterHTML(chapter) {
  const title = chapter.title || `Chapter ${chapter.chapter_number}`;
  const transcription = chapter.transcription?.raw_text || "No transcription stored.";
  const summary = chapter.summary?.summary_text || "";

  return `
    <div class="inline-chapter-card">
      <div class="inline-chapter-title">
        <span class="chunk-badge">Chunk ${chapter.chapter_number}</span>
        ${escHtml(title)}
      </div>
      <div class="inline-chapter-block">
        <strong>Transcription</strong>
        <pre>${escHtml(transcription)}</pre>
      </div>
      ${summary ? `
        <div class="inline-chapter-block inline-summary-block">
          <strong>Summary</strong>
          <pre>${escHtml(summary)}</pre>
        </div>` : ""}
    </div>
  `;
}

function buildRecordingActionHTML(recording) {
  const destructiveActions = `
    <button class="btn-ghost btn-sm" data-action="restart-recording" data-recording-id="${recording.id}">
      Restart
    </button>
    <button class="btn-danger btn-sm" data-action="delete-recording" data-recording-id="${recording.id}">
      Delete
    </button>
  `;

  if (recording.status === "completed") {
    return `
      <div class="recording-action-stack">
        <button class="btn-primary" data-action="view-results" data-recording-id="${recording.id}">
          View Chapters &amp; Transcription
        </button>
        <div class="recording-secondary-actions">${destructiveActions}</div>
      </div>
    `;
  }

  if (recording.status === "ready") {
    return `
      <div class="recording-action-stack">
        <button class="btn-primary" data-action="process-recording" data-recording-id="${recording.id}">
          Process (Russian)
        </button>
        <div class="recording-secondary-actions">${destructiveActions}</div>
      </div>
    `;
  }

  if (recording.status === "processing") {
    return `
      <div class="recording-action-stack">
        <button class="btn-ghost" disabled>Processing…</button>
        <div class="recording-secondary-actions">${destructiveActions}</div>
      </div>
    `;
  }

  if (recording.status === "recording") {
    return `
      <div class="recording-action-stack">
        <button class="btn-ghost" disabled>Recording not finalized</button>
        <div class="recording-secondary-actions">${destructiveActions}</div>
      </div>
    `;
  }

  if (recording.status === "error") {
    return `
      <div class="recording-action-stack">
        <button class="btn-ghost" disabled>Needs retry</button>
        <div class="recording-secondary-actions">${destructiveActions}</div>
      </div>
    `;
  }

  return `
    <div class="recording-action-stack">
      <button class="btn-ghost" disabled>No action</button>
      <div class="recording-secondary-actions">${destructiveActions}</div>
    </div>
  `;
}

function deriveRecordingTitle(recording) {
  const titles = getDisplayChapterTitles(recording);
  if (!titles.length) return `Recording #${recording.id}`;

  const visible = titles.slice(0, 2).join(" · ");
  const suffix = titles.length > 2 ? ` +${titles.length - 2}` : "";
  return `${visible}${suffix}`;
}

function buildChapterTitlePreviewHTML(recording) {
  const titles = getDisplayChapterTitles(recording);
  if (!titles.length) return "";

  return `
    <div class="recording-chapter-title-preview">
      ${titles.map((title) => `<span>${escHtml(title)}</span>`).join("")}
    </div>
  `;
}

function getDisplayChapterTitles(recording) {
  const chapters = recording.chapters || [];
  return chapters
    .map((chapter) => deriveChapterTitle(chapter))
    .filter(Boolean);
}

function deriveChapterTitle(chapter) {
  const explicit = normalizeTitle(chapter.title);
  if (explicit && !isGenericChapterTitle(explicit, chapter.chapter_number)) return explicit;

  const parsed = extractChapterTitleFromText(chapter.transcription?.raw_text || "", chapter.chapter_number);
  if (parsed) return parsed;

  return explicit || `Chapter ${chapter.chapter_number}`;
}

function isGenericChapterTitle(title, chapterNumber) {
  const escapedNumber = String(chapterNumber ?? "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const generic = [
    /^untitled$/i,
    /^chapter\s*\d+$/i,
    /^chunk\s*\d+$/i,
    /^глава\s*\d+$/i,
    /^без\s+названия$/i,
  ];

  if (escapedNumber) {
    generic.push(new RegExp(`^chapter\\s*${escapedNumber}$`, "i"));
    generic.push(new RegExp(`^chunk\\s*${escapedNumber}$`, "i"));
    generic.push(new RegExp(`^глава\\s*${escapedNumber}$`, "i"));
  }

  return generic.some((pattern) => pattern.test(title.trim()));
}

function extractChapterTitleFromText(text, chapterNumber) {
  const firstText = normalizeTitle(text).slice(0, 260);
  if (!firstText) return "";

  const patterns = [
    /\b(глава\s+(?:\d+|[а-яё]+)(?:\s*[-—:.,]?\s*[^.!?\n]{0,80})?)/i,
    /\b(chapter\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)(?:\s*[-—:.,]?\s*[^.!?\n]{0,80})?)/i,
    /\b(new\s+chapter(?:\s*[-—:.,]?\s*[^.!?\n]{0,80})?)/i,
    /\b(next\s+chapter(?:\s*[-—:.,]?\s*[^.!?\n]{0,80})?)/i,
  ];

  for (const pattern of patterns) {
    const match = firstText.match(pattern);
    if (match?.[1]) return trimTitle(match[1]);
  }

  const firstLine = trimTitle(firstText.split(/[.!?\n]/)[0] || "");
  if (firstLine.length >= 4 && firstLine.length <= 90) {
    return firstLine;
  }

  return chapterNumber ? `Chapter ${chapterNumber}` : "";
}

function normalizeTitle(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function trimTitle(value) {
  return normalizeTitle(value)
    .replace(/^[\s:;,.!?—-]+/, "")
    .replace(/[\s:;,.!?—-]+$/, "")
    .slice(0, 120);
}

function findRecording(book, recordingId) {
  return (book.recordings || []).find((recording) => recording.id === recordingId) || { id: recordingId };
}

function recordingDeleteMessage(recording) {
  return [
    `Delete recording #${recording.id}?`,
    "",
    "This will permanently remove its audio, chapters, transcriptions, and summaries.",
  ].join("\n");
}

function recordingRestartMessage(recording) {
  return [
    `Restart recording #${recording.id}?`,
    "",
    "The current recording and its audio/chapters/transcriptions/summaries will be deleted.",
    "A new clean recording screen for this book will open immediately.",
  ].join("\n");
}

function sortRecordings(recordings) {
  return [...recordings].sort((a, b) => {
    const aTime = Date.parse(a.created_at || "") || 0;
    const bTime = Date.parse(b.created_at || "") || 0;
    return bTime - aTime;
  });
}

async function hydrateRecordingDetails(recordings) {
  return Promise.all(recordings.map(async (recording) => {
    if (recording.status !== "completed") return recording;

    try {
      return await api.get(`/api/recordings/${recording.id}`);
    } catch (err) {
      return {
        ...recording,
        detail_error: err.detail || err.message,
      };
    }
  }));
}

function statusDescription(status) {
  switch (status) {
    case "completed":
      return "Chapters, transcription, summary, and recording audio are stored. Open them to view the existing results.";
    case "ready":
      return "Audio is saved and ready for transcription/summarization. Processing will create chapter records.";
    case "processing":
      return "Transcription and summarization are currently running.";
    case "recording":
      return "This recording session was created but has not been finalized with uploaded audio.";
    case "error":
      return "Processing or audio finalization failed. Existing failed state is shown instead of being hidden.";
    default:
      return "Recording is stored with an unknown status.";
  }
}

function escHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatDuration(totalSeconds) {
  const total = Math.floor(totalSeconds || 0);
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatDate(isoStr) {
  if (!isoStr) return "—";
  try {
    return new Date(isoStr).toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch (_) { return isoStr; }
}

function formatDateTime(isoStr) {
  if (!isoStr) return "—";
  try {
    return new Date(isoStr).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch (_) { return isoStr; }
}
