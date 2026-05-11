/**
 * Shared renderer and polling helpers for persisted recording processing progress.
 */

import { getRecordingProgress } from "../api.js";

export function renderProcessingProgress(progress, { compact = false } = {}) {
  const normalized = normalizeProgress(progress);
  const statusClass = normalized.stage === "error" || normalized.status === "error"
    ? "is-error"
    : normalized.stage === "completed" || normalized.status === "completed"
      ? "is-success"
      : "is-active";
  const label = progressStageLabel(normalized.stage);
  const transcriptionLabel = normalized.transcription_chunks_total > 0
    ? `${normalized.transcription_percent}% · ${normalized.transcription_chunks_completed}/${normalized.transcription_chunks_total} chunks`
    : "waiting for chunks";
  const summaryLabel = normalized.summary_chapters_total > 0
    ? `${normalized.summary_chapters_completed}/${normalized.summary_chapters_total} chapters`
    : "waiting for chapter analysis";
  const sectionLabel = normalized.summary_sections_total > 0
    ? `${normalized.summary_section_percent}% · ${normalized.summary_sections_completed}/${normalized.summary_sections_total} sections`
    : "waiting for summary sections";
  const message = normalized.error_message || normalized.message || label;

  return `
    <div class="processing-progress ${compact ? "is-compact" : ""} ${statusClass}" data-stage="${escHtml(normalized.stage)}">
      <div class="processing-progress-topline">
        <span class="processing-progress-stage">${escHtml(label)}</span>
        <span class="processing-progress-percent">${normalized.percent}%</span>
      </div>
      <div class="processing-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${normalized.percent}" aria-label="Processing progress">
        <div class="processing-progress-fill" style="width:${normalized.percent}%"></div>
      </div>
      <div class="processing-progress-message">${escHtml(message)}</div>
      <div class="processing-progress-metrics">
        <span>📝 Transcription: ${escHtml(transcriptionLabel)}</span>
        <span>📚 Summaries: ${escHtml(summaryLabel)}</span>
        <span>🧩 Summary sections: ${escHtml(sectionLabel)}</span>
      </div>
    </div>
  `;
}

export function renderProgressPlaceholder(message = "Waiting for processing progress…", { compact = false } = {}) {
  return renderProcessingProgress({
    status: "processing",
    stage: "queued",
    message,
    percent: 0,
  }, { compact });
}

export function startProgressPolling(recordingId, onProgress, { intervalMs = 1200, stopWhenTerminal = true } = {}) {
  let stopped = false;
  let timer = null;

  async function tick() {
    if (stopped) return;
    try {
      const progress = await getRecordingProgress(recordingId);
      if (stopped) return;
      onProgress(progress);
      if (stopWhenTerminal && isTerminalProgress(progress)) {
        stopped = true;
        return;
      }
    } catch (_) {
      // Keep polling; the POST /process call is still the source of truth for errors.
    }
    if (!stopped) timer = setTimeout(tick, intervalMs);
  }

  tick();
  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
  };
}

export function isTerminalProgress(progress) {
  const stage = String(progress?.stage || "");
  const status = String(progress?.status || "");
  return stage === "completed" || stage === "error" || status === "completed" || status === "error";
}

function normalizeProgress(progress = {}) {
  const totalChunks = Number(progress.transcription_chunks_total || 0);
  const completedChunks = clampCompleted(Number(progress.transcription_chunks_completed || 0), totalChunks);
  const totalSummaries = Number(progress.summary_chapters_total || 0);
  const completedSummaries = clampCompleted(Number(progress.summary_chapters_completed || 0), totalSummaries);
  const totalSections = Number(progress.summary_sections_total || 0);
  const completedSections = clampCompleted(Number(progress.summary_sections_completed || 0), totalSections);
  return {
    status: String(progress.status || "processing"),
    stage: String(progress.stage || "queued"),
    message: String(progress.message || ""),
    percent: clampPercent(progress.percent),
    transcription_chunks_completed: Math.max(0, completedChunks),
    transcription_chunks_total: Math.max(0, totalChunks),
    transcription_percent: clampPercent(progress.transcription_percent ?? (totalChunks ? (completedChunks / totalChunks) * 100 : 0)),
    summary_chapters_completed: Math.max(0, completedSummaries),
    summary_chapters_total: Math.max(0, totalSummaries),
    summary_percent: clampPercent(progress.summary_percent ?? (totalSummaries ? (completedSummaries / totalSummaries) * 100 : 0)),
    summary_sections_completed: Math.max(0, completedSections),
    summary_sections_total: Math.max(0, totalSections),
    summary_section_percent: clampPercent(progress.summary_section_percent ?? (totalSections ? (completedSections / totalSections) * 100 : 0)),
    error_message: progress.error_message ? String(progress.error_message) : "",
  };
}

function progressStageLabel(stage) {
  switch (stage) {
    case "ready": return "Ready";
    case "queued": return "Queued";
    case "validating": return "Validating audio";
    case "transcribing": return "Transcribing";
    case "transcribed": return "Transcription complete";
    case "analyzing_chapters": return "Analyzing chapters";
    case "summarizing": return "Summarizing";
    case "persisting": return "Saving results";
    case "completed": return "Completed";
    case "error": return "Failed";
    default: return stage ? stage.replace(/_/g, " ") : "Processing";
  }
}

function clampPercent(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(100, Math.round(n)));
}

function clampCompleted(completed, total) {
  const normalizedCompleted = Math.max(0, Number.isFinite(completed) ? completed : 0);
  const normalizedTotal = Math.max(0, Number.isFinite(total) ? total : 0);
  if (normalizedTotal <= 0) return normalizedCompleted;
  return Math.min(normalizedCompleted, normalizedTotal);
}

function escHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
