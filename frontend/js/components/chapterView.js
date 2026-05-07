/**
 * chapterView.js — Displays transcription and summary results grouped by chapters.
 *
 * Exported:
 *   renderChapterView(container, { recordingId, onBack, onBookLoaded, onNewRecording })
 */

import { api, updateRecordingChapter, updateRecordingChapterOrder } from "../api.js";
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
export async function renderChapterView(container, { recordingId, onBack, onBookLoaded, onNewRecording, openChapterId = null }) {
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
    row.addEventListener("click", (event) => {
      if (event.target.closest("button, input, textarea, label, a")) return;
      const item = row.closest(".chapter-item");
      item.classList.toggle("is-open");
    });
  });

  // Open the requested chapter, or the first chapter by default
  const initialOpenItem = openChapterId
    ? container.querySelector(`.chapter-item[data-chapter-id='${openChapterId}']`)
    : container.querySelector(".chapter-item");
  if (initialOpenItem) initialOpenItem.classList.add("is-open");

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

  container.querySelectorAll("[data-action='save-chapter']").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const chapterId = Number(btn.dataset.chapterId);
      const item = btn.closest(".chapter-item");
      const transcription = item.querySelector("[data-field='transcription']")?.value ?? "";
      const summary = item.querySelector("[data-field='summary']")?.value ?? "";
      const payload = {
        title: item.querySelector("[data-field='title']")?.value ?? "",
      };
      if (transcription.trim()) payload.transcription = transcription;
      if (summary.trim()) payload.summary = summary;

      btn.disabled = true;
      const originalLabel = btn.textContent;
      btn.textContent = "Saving…";
      try {
        recording = await updateRecordingChapter(recordingId, chapterId, payload);
        showToast("Chapter changes saved.", "success");
        await rerenderChapterView(container, recording, book, { onBack, onBookLoaded, onNewRecording }, chapterId);
      } catch (err) {
        showToast("Could not save chapter: " + (err.detail || err.message), "error", 6000);
        btn.disabled = false;
        btn.textContent = originalLabel;
      }
    });
  });

  container.querySelectorAll("[data-action='reset-chapter-form']").forEach((btn) => {
    btn.addEventListener("click", () => {
      const item = btn.closest(".chapter-item");
      const chapterId = Number(btn.dataset.chapterId);
      const chapter = (recording.chapters || []).find((ch) => ch.id === chapterId);
      if (!chapter || !item) return;
      item.querySelector("[data-field='title']").value = chapter.title || "";
      item.querySelector("[data-field='transcription']").value = chapter.transcription?.raw_text || "";
      item.querySelector("[data-field='summary']").value = chapter.summary?.summary_text || "";
      item.classList.remove("is-editing");
      showToast("Chapter form reset.", "info");
    });
  });

  container.querySelectorAll("[data-action='toggle-chapter-edit']").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      const item = btn.closest(".chapter-item");
      if (!item) return;
      const isEditing = item.classList.toggle("is-editing");
      item.classList.add("is-open");
      btn.textContent = isEditing ? "Hide Editor" : "Edit";
      btn.setAttribute("aria-expanded", String(isEditing));
    });
  });

  container.querySelectorAll("[data-action='move-chapter']").forEach((btn) => {
    btn.addEventListener("click", async (event) => {
      event.stopPropagation();
      const chapterId = Number(btn.dataset.chapterId);
      const direction = btn.dataset.direction;
      const nextOrder = getReorderedChapterIds(recording.chapters || [], chapterId, direction);
      if (!nextOrder) return;

      btn.disabled = true;
      try {
        recording = await updateRecordingChapterOrder(recordingId, nextOrder);
        showToast("Chapter order saved.", "success");
        await rerenderChapterView(container, recording, book, { onBack, onBookLoaded, onNewRecording }, chapterId);
      } catch (err) {
        showToast("Could not reorder chapters: " + (err.detail || err.message), "error", 6000);
        btn.disabled = false;
      }
    });
  });
}

async function rerenderChapterView(container, recording, book, options, openChapterId = null) {
  const freshOptions = {
    ...options,
    openChapterId,
    onBookLoaded: (loadedBook) => {
      options.onBookLoaded?.(loadedBook || book);
    },
  };
  await renderChapterView(container, { ...freshOptions, recordingId: recording.id });
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

  const items = chapters.map((ch, index) => buildChapterItemHTML(ch, index, chapters.length)).join("\n");
  return `<div class="chapters-list">${items}</div>`;
}

function buildChapterItemHTML(chapter, index, totalChapters) {
  const transcription = chapter.transcription?.raw_text || "";
  const summary = chapter.summary?.summary_text || "";
  const transcriptionHTML = chapter.transcription
    ? `<details class="chapter-section transcription-details">
         <summary>
           <span>📝 Transcription</span>
           <span class="details-hint">show raw transcript</span>
         </summary>
         <pre>${escHtml(transcription)}</pre>
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
              summary,
              transcription
            )}</div>
         </div>
         <div class="summary-meta-row">
           <span class="summary-model-chip">🤖 Model: ${escHtml(chapter.summary.model_used)}</span>
         </div>
       </section>`
    : `<div class="chapter-section text-muted text-sm">No summary available.</div>`;

  const titleText = escHtml(deriveChapterTitle(chapter));
  const canMoveUp = index > 0;
  const canMoveDown = index < totalChapters - 1;

  return `
    <div class="chapter-item" data-chapter-id="${chapter.id}">
      <div class="chapter-summary-row" role="button" tabindex="0"
           aria-expanded="false" aria-controls="ch-body-${chapter.id}">
        <div class="chapter-num-badge">${chapter.chapter_number}</div>
        <div class="chapter-title-text">
          <span class="chunk-badge">Chapter ${chapter.chapter_number}</span>
          ${titleText}
        </div>
        <button class="btn-ghost btn-sm chapter-edit-toggle" data-action="toggle-chapter-edit" data-chapter-id="${chapter.id}" aria-expanded="false">Edit</button>
        <div class="chapter-sort-controls" aria-label="Sort chapter">
          <button class="btn-ghost btn-sm" data-action="move-chapter" data-direction="up" data-chapter-id="${chapter.id}" ${canMoveUp ? "" : "disabled"}>↑</button>
          <button class="btn-ghost btn-sm" data-action="move-chapter" data-direction="down" data-chapter-id="${chapter.id}" ${canMoveDown ? "" : "disabled"}>↓</button>
        </div>
        <span class="chapter-chevron">▼</span>
      </div>
      <div class="chapter-body" id="ch-body-${chapter.id}">
        ${buildChapterEditFormHTML(chapter, transcription, summary)}
        ${summaryHTML}
        ${transcriptionHTML}
      </div>
    </div>
  `;
}

function buildChapterEditFormHTML(chapter, transcription, summary) {
  return `
    <section class="chapter-section chapter-edit-section" aria-label="Manual chapter editing">
      <div class="chapter-edit-header">
        <div>
          <h4>Manual Edit</h4>
          <p>Use this only when you need to correct the saved title, transcription, or summary.</p>
        </div>
      </div>
      <div class="chapter-edit-grid">
        <label class="chapter-edit-field chapter-edit-title-field">
          <span>Title</span>
          <input data-field="title" type="text" maxlength="512" value="${escHtml(chapter.title || "")}" placeholder="Chapter title">
        </label>
        <label class="chapter-edit-field">
          <span>Transcription</span>
          <textarea data-field="transcription" rows="8" placeholder="Raw transcription text">${escHtml(transcription)}</textarea>
        </label>
        <label class="chapter-edit-field">
          <span>Summary</span>
          <textarea data-field="summary" rows="10" placeholder="Markdown summary text">${escHtml(summary)}</textarea>
        </label>
      </div>
      <div class="chapter-edit-actions">
        <button class="btn-primary btn-sm" data-action="save-chapter" data-chapter-id="${chapter.id}">Save Chapter</button>
        <button class="btn-ghost btn-sm" data-action="reset-chapter-form" data-chapter-id="${chapter.id}">Cancel Changes</button>
      </div>
    </section>
  `;
}

function getReorderedChapterIds(chapters, chapterId, direction) {
  const ids = chapters.map((chapter) => chapter.id);
  const currentIndex = ids.indexOf(chapterId);
  if (currentIndex === -1) return null;

  const nextIndex = direction === "up" ? currentIndex - 1 : currentIndex + 1;
  if (nextIndex < 0 || nextIndex >= ids.length) return null;

  [ids[currentIndex], ids[nextIndex]] = [ids[nextIndex], ids[currentIndex]];
  return ids;
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
