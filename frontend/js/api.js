/**
 * api.js — HTTP and WebSocket client helpers.
 *
 * All REST calls go through the `api` object.
 * WebSocket helpers are exported separately.
 */

const BASE = "";   // same origin — FastAPI serves both API and frontend

// ---------------------------------------------------------------------------
// Low-level fetch wrapper
// ---------------------------------------------------------------------------

async function request(method, path, body = undefined) {
  const init = {
    method,
    headers: {},
  };

  if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers["Content-Type"] = "application/json";
  }

  const res = await fetch(BASE + path, init);

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      detail = data.detail || JSON.stringify(data);
    } catch (_) {
      /* ignore parse errors */
    }
    throw new ApiError(res.status, detail);
  }

  // 204 No Content — return null
  if (res.status === 204) return null;

  return res.json();
}

async function uploadRequest(path, formData) {
  const res = await fetch(BASE + path, {
    method: "POST",
    body: formData,
  });

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      detail = data.detail || JSON.stringify(data);
    } catch (_) {
      /* ignore parse errors */
    }
    throw new ApiError(res.status, detail);
  }

  return res.json();
}

// ---------------------------------------------------------------------------
// Named error class
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  /**
   * @param {number} status  HTTP status code
   * @param {string} detail  Human-readable message from server
   */
  constructor(status, detail) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

// ---------------------------------------------------------------------------
// REST helpers
// ---------------------------------------------------------------------------

export const api = {
  /** @param {string} path */
  get: (path) => request("GET", path),

  /** @param {string} path  @param {object} body */
  post: (path, body) => request("POST", path, body),

  /** @param {string} path  @param {object} body */
  patch: (path, body) => request("PATCH", path, body),

  /** @param {string} path */
  delete: (path) => request("DELETE", path),
};

/**
 * Trigger the transcription + summarisation pipeline for a recording.
 *
 * @param {number} recordingId
 * @param {string} [language="ru"]  BCP-47 language code (e.g. "ru", "en", "auto")
 * @returns {Promise<object>}  RecordingDetailOut from the server
 */
export function processRecording(recordingId, language = "ru") {
  return request("POST", `/api/recordings/${recordingId}/process`, { language });
}

/**
 * Manually update one chapter's editable fields.
 *
 * @param {number} recordingId
 * @param {number} chapterId
 * @param {{title?: string|null, transcription?: string, summary?: string}} payload
 * @returns {Promise<object>} RecordingDetailOut from the server
 */
export function updateRecordingChapter(recordingId, chapterId, payload) {
  return request("PATCH", `/api/recordings/${recordingId}/chapters/${chapterId}`, payload);
}

/**
 * Persist the manual order of chapters for one recording.
 *
 * @param {number} recordingId
 * @param {number[]} chapterIds
 * @returns {Promise<object>} RecordingDetailOut from the server
 */
export function updateRecordingChapterOrder(recordingId, chapterIds) {
  return request("PATCH", `/api/recordings/${recordingId}/chapters/order`, { chapter_ids: chapterIds });
}

/** @param {number} bookId */
export function getBookTestAvailability(bookId) {
  return request("GET", `/api/books/${bookId}/tests/availability`);
}

/**
 * @param {number} bookId
 * @param {{chapter_id?: number|null, target_count?: number, replace_existing?: boolean}} payload
 */
export function generateBookTests(bookId, payload = {}) {
  return request("POST", `/api/books/${bookId}/tests/generate`, payload);
}

/**
 * @param {number} bookId
 * @param {{chapter_id?: number|null, sample_size?: number}} payload
 */
export function sampleBookTests(bookId, payload = {}) {
  return request("POST", `/api/books/${bookId}/tests/sample`, payload);
}

/**
 * @param {number} bookId
 * @param {Array<{question_id: number, option_id: number}>} answers
 */
export function submitBookTests(bookId, answers) {
  return request("POST", `/api/books/${bookId}/tests/submit`, { answers });
}

/**
 * Upload one complete browser-recorded audio session for offline decode + VAD.
 *
 * @param {number} recordingId
 * @param {Blob} audioBlob
 * @returns {Promise<object>} RecordingOut from the server
 */
export function uploadRecordingAudio(recordingId, audioBlob) {
  const form = new FormData();
  const ext = audioBlob.type.includes("ogg") ? "ogg" : "webm";
  form.append("file", audioBlob, `recording-${recordingId}.${ext}`);
  return uploadRequest(`/api/recordings/${recordingId}/audio`, form);
}

/**
 * Append one browser-recorded audio session to an existing recording.
 *
 * @param {number} recordingId
 * @param {Blob} audioBlob
 * @returns {Promise<object>} RecordingOut from the server
 */
export function appendRecordingAudio(recordingId, audioBlob) {
  const form = new FormData();
  const ext = audioBlob.type.includes("ogg") ? "ogg" : "webm";
  form.append("file", audioBlob, `recording-${recordingId}-append.${ext}`);
  return uploadRequest(`/api/recordings/${recordingId}/audio/append`, form);
}

// ---------------------------------------------------------------------------
// WebSocket helper
// ---------------------------------------------------------------------------

/**
 * Open a WebSocket to `/ws/audio/{recordingId}`.
 *
 * @param {number} recordingId
 * @param {object} callbacks
 * @param {(data: object) => void} callbacks.onMessage   — called for every JSON message
 * @param {() => void}             callbacks.onOpen      — called when socket opens
 * @param {(event: CloseEvent) => void} callbacks.onClose
 * @param {(event: Event) => void} callbacks.onError
 * @returns {WebSocket}
 */
export function openAudioWebSocket(recordingId, { onMessage, onOpen, onClose, onError } = {}) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${protocol}//${window.location.host}/ws/audio/${recordingId}`;

  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";

  if (onOpen)    ws.addEventListener("open",    onOpen);
  if (onClose)   ws.addEventListener("close",   onClose);
  if (onError)   ws.addEventListener("error",   onError);

  if (onMessage) {
    ws.addEventListener("message", (event) => {
      try {
        const data = JSON.parse(event.data);
        onMessage(data);
      } catch (_) {
        // binary frames are not expected from server → ignore
      }
    });
  }

  return ws;
}
