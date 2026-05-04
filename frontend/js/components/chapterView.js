/**
 * chapterView.js — Displays transcription and summary results grouped by chapters.
 *
 * Exported:
 *   renderChapterView(container, { recordingId, onBack, onBookLoaded, onNewRecording })
 */

import { api } from "../api.js";
import { showToast } from "../app.js";
import { renderMermaidDiagrams, renderSummaryWithTranscriptGraphs } from "../summaryRenderer.js";

/**
 * @param {HTMLElement} container
 * @param {object}   opts
 * @param {number}   opts.recordingId
 * @param {(bookId?: number) => void} opts.onBack — go back to selected book or book list
 * @param {(book: object) => void} opts.onBookLoaded — update router state/breadcrumb with recording's book
 * @param {(book: object) => void} opts.onNewRecording — start another recording for same book
 */
export async function renderChapterView(container, { recordingId, onBack, onBookLoaded, onNewRecording }) {
  container.innerHTML = `
    <div class="loading-spinner">
      <div class="spinner"></div>
      <p>Loading results…</p>
    </div>
  `;

  let recording;
  let book = null;
  try {
    recording = await api.get(`/api/recordings/${recordingId}`);
    book = await api.get(`/api/books/${recording.book_id}`);
    onBookLoaded?.(book);
  } catch (err) {
    container.innerHTML = `
      <div class="card" style="color:var(--color-danger)">
        <strong>Could not load recording:</strong> ${escHtml(err.detail || err.message)}
      </div>
      <div class="view-actions">
        <button class="btn-ghost" id="btn-back-err">← Back</button>
      </div>`;
    container.querySelector("#btn-back-err").addEventListener("click", () => onBack(recording?.book_id));
    return;
  }

  // Build the view
  container.innerHTML = buildChapterViewHTML(recording);
  renderMermaidDiagrams(container);

  // Wire chapter accordion items
  container.querySelectorAll(".chapter-summary-row").forEach((row) => {
    row.addEventListener("click", () => {
      const item = row.closest(".chapter-item");
      item.classList.toggle("is-open");
    });
  });

  // Open the first chapter by default
  const firstItem = container.querySelector(".chapter-item");
  if (firstItem) firstItem.classList.add("is-open");

  // Buttons
  container.querySelector("#btn-back")?.addEventListener("click", () => onBack(recording.book_id));

  const btnNew = container.querySelector("#btn-new-recording");
  if (btnNew && recording) {
    // We need the book to re-record — fetch it via recording's book_id
    btnNew.addEventListener("click", async () => {
      try {
        const loadedBook = book || await api.get(`/api/books/${recording.book_id}`);
        onNewRecording(loadedBook);
      } catch (err) {
        showToast("Could not load book: " + (err.detail || err.message), "error");
      }
    });
  }

  // Export button
  container.querySelector("#btn-export")?.addEventListener("click", () => {
    exportToText(recording);
  });
}

// ---------------------------------------------------------------------------
// HTML builders
// ---------------------------------------------------------------------------

function buildChapterViewHTML(recording) {
  const statusBadge = `<span class="status-badge ${recording.status}">${recording.status}</span>`;
  const durationStr = recording.duration_seconds
    ? formatDuration(recording.duration_seconds)
    : "N/A";

  const chaptersHTML = buildChaptersHTML(recording.chapters || []);

  const audioPlayerHTML = recording.audio_url
    ? `
    <div class="audio-player-card card">
      <h3 class="audio-player-title">🔊 Recording Audio</h3>
      <audio controls preload="metadata" class="audio-player"
             src="${escHtml(recording.audio_url)}">
        Your browser does not support the audio element.
      </audio>
    </div>`
    : "";

  return `
    <div class="chapter-view">
    <div class="chapter-view-header">
      <h2>Recording Results</h2>
      <div class="sub">
        ${statusBadge}
        &nbsp;·&nbsp; ${recording.chapters?.length ?? 0} chapter(s)
        &nbsp;·&nbsp; Duration: ${escHtml(durationStr)}
        ${recording.processed_at
          ? `&nbsp;·&nbsp; Processed ${formatDate(recording.processed_at)}`
          : ""}
      </div>
    </div>

    ${audioPlayerHTML}

    ${chaptersHTML}

    <div class="view-actions">
      <button class="btn-ghost" id="btn-back">← Back to Book</button>
      <button class="btn-primary" id="btn-new-recording">🎙 Record Again</button>
      <button class="btn-ghost" id="btn-export">⬇ Export Text</button>
    </div>
    </div>
  `;
}

function buildChaptersHTML(chapters) {
  if (!chapters || chapters.length === 0) {
    return `
      <div class="empty-state">
        <div class="empty-icon">📄</div>
        <h3>No chapters found</h3>
        <p>The recording may be empty or processing may have encountered an error.</p>
      </div>
    `;
  }

  const items = chapters.map((ch) => buildChapterItemHTML(ch)).join("\n");
  return `<div class="chapters-list">${items}</div>`;
}

function buildChapterItemHTML(chapter) {
  const transcriptionHTML = chapter.transcription
    ? `<details class="chapter-section transcription-details">
         <summary>
           <span>📝 Transcription</span>
           <span class="details-hint">show raw transcript</span>
         </summary>
         <pre>${escHtml(chapter.transcription.raw_text)}</pre>
       </details>`
    : `<div class="chapter-section text-muted text-sm">No transcription available.</div>`;

  const summaryHTML = chapter.summary
    ? `<section class="chapter-section summary-section" aria-label="Chapter summary">
         <div class="summary-section-header">
           <div class="summary-title-group">
             <span class="summary-icon-badge" aria-hidden="true">✨</span>
             <div>
               <h4>Smart Summary</h4>
               <p>Key ideas, quotes, and relationship maps distilled from this chapter.</p>
             </div>
           </div>
           <span class="summary-format-badge">Markdown + Graphs</span>
         </div>
         <div class="summary-content-shell">
           <div class="summary-markdown">${renderSummaryWithTranscriptGraphs(
             chapter.summary.summary_text,
             chapter.transcription?.raw_text || ""
           )}</div>
         </div>
         <div class="summary-meta-row">
           <span class="summary-model-chip">🤖 Model: ${escHtml(chapter.summary.model_used)}</span>
         </div>
       </section>`
    : `<div class="chapter-section text-muted text-sm">No summary available.</div>`;

  const titleText = escHtml(deriveChapterTitle(chapter));

  return `
    <div class="chapter-item">
      <div class="chapter-summary-row" role="button" tabindex="0"
           aria-expanded="false" aria-controls="ch-body-${chapter.id}">
        <div class="chapter-num-badge">${chapter.chapter_number}</div>
        <div class="chapter-title-text">
          <span class="chunk-badge">Chapter ${chapter.chapter_number}</span>
          ${titleText}
        </div>
        <span class="chapter-chevron">▼</span>
      </div>
      <div class="chapter-body" id="ch-body-${chapter.id}">
        ${summaryHTML}
        ${transcriptionHTML}
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Export helper
// ---------------------------------------------------------------------------

function exportToText(recording) {
  const chapters = recording.chapters || [];
  const lines = [
    `Recording ID: ${recording.id}`,
    `Book ID: ${recording.book_id}`,
    `Status: ${recording.status}`,
    `Chapters: ${chapters.length}`,
    `Duration: ${recording.duration_seconds ? formatDuration(recording.duration_seconds) : "N/A"}`,
    "",
    "=".repeat(60),
    "",
  ];

  for (const ch of chapters) {
    const title = deriveChapterTitle(ch);
    lines.push(`## ${title}`);
    lines.push("");

    if (ch.transcription) {
      lines.push("### Transcription");
      lines.push(ch.transcription.raw_text);
      lines.push("");
    }

    if (ch.summary) {
      lines.push("### Summary");
      lines.push(ch.summary.summary_text);
      lines.push("");
    }

    lines.push("-".repeat(60));
    lines.push("");
  }

  const blob = new Blob([lines.join("\n")], { type: "text/plain" });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement("a");
  a.href     = url;
  a.download = `recording-${recording.id}-summary.txt`;
  a.click();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function escHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
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

function formatDuration(totalSeconds) {
  if (!totalSeconds) return "0:00";
  const s = Math.floor(totalSeconds % 60);
  const m = Math.floor(totalSeconds / 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatDate(isoStr) {
  if (!isoStr) return "—";
  try {
    return new Date(isoStr).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch (_) { return isoStr; }
}
