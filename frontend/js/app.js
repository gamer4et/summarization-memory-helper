/**
 * app.js — SPA router and global utilities.
 *
 * Views
 * -----
 *   #books               → Book list
 *   #book?book=<id>      → Selected book dashboard with existing results
 *   #record?book=<id>    → New recording panel for a book
 *   #record?book=<id>&append=<recording_id> → Continue an existing recording
 *   #chapters?rec=<id>   → Chapter view for a completed recording
 *   #tests?book=<id>     → Book-level generated quiz/test view
 *
 * Exported globals (used by components)
 * ---------------------------------------
 *   showToast(message, type?)
 */

import { renderBookList }      from "./components/bookList.js";
import { renderBookDetail }    from "./components/bookDetail.js";
import { renderRecordingPanel } from "./components/recordingPanel.js";
import { renderChapterView }   from "./components/chapterView.js";
import { renderTestView }      from "./components/testView.js";
import { api }                 from "./api.js";

// ---------------------------------------------------------------------------
// App state
// ---------------------------------------------------------------------------

const state = {
  /** @type {'books'|'book'|'record'|'chapters'|'tests'} */
  view: "books",
  /** @type {object|null} selected book for recording */
  book: null,
  /** @type {number|null} recording id for chapter view */
  recordingId: null,
};

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

function getHashParams() {
  const hash = window.location.hash.slice(1);   // remove leading '#'
  const [path, query] = hash.split("?");
  const params = {};
  if (query) {
    for (const part of query.split("&")) {
      const [k, v] = part.split("=");
      if (k) params[decodeURIComponent(k)] = decodeURIComponent(v ?? "");
    }
  }
  return { path: path || "books", params };
}

function setHash(path, params = {}) {
  const qs = Object.entries(params)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");
  window.location.hash = qs ? `${path}?${qs}` : path;
}

async function route() {
  const main       = document.getElementById("main");
  const breadcrumb = document.getElementById("breadcrumb");
  const { path, params } = getHashParams();

  main.innerHTML = "";                 // clear previous content

  // ── Books list ────────────────────────────────────────────────────────────
  if (path === "books") {
    setBreadcrumb(breadcrumb, [{ label: "Books" }]);
    await renderBookList(main, {
      onSelectBook(book) {
        state.book = book;
        setHash("book", { book: book.id });
      },
    });
    return;
  }

  // ── Selected book dashboard ───────────────────────────────────────────────
  if (path === "book") {
    const bookId = parseInt(params.book, 10);
    if (!bookId) { setHash("books"); return; }

    const bookLabel = state.book?.id === bookId ? state.book.title : "Book";
    setBreadcrumb(breadcrumb, [
      { label: "Books", onClick: () => setHash("books") },
      { label: bookLabel },
    ]);

    await renderBookDetail(main, {
      bookId,
      onBack() { setHash("books"); },
      onBookLoaded(book) {
        state.book = book;
        setBreadcrumb(breadcrumb, [
          { label: "Books", onClick: () => setHash("books") },
          { label: book.title },
        ]);
      },
      onNewRecording(book) {
        state.book = book;
        setHash("record", { book: book.id });
      },
      onContinueRecording(book, recordingId) {
        state.book = book;
        state.recordingId = recordingId;
        setHash("record", { book: book.id, append: recordingId });
      },
      onViewResults(recordingId) {
        state.recordingId = recordingId;
        setHash("chapters", { rec: recordingId });
      },
      onViewTests(book) {
        state.book = book;
        setHash("tests", { book: book.id });
      },
    });
    return;
  }

  // ── Book tests / quiz view ────────────────────────────────────────────────
  if (path === "tests") {
    const bookId = parseInt(params.book, 10);
    if (!bookId) { setHash("books"); return; }

    if (!state.book || state.book.id !== bookId) {
      try {
        state.book = await api.get(`/api/books/${bookId}`);
      } catch (_) {
        showToast("Book not found — returning to list.", "error");
        setHash("books");
        return;
      }
    }

    setBreadcrumb(breadcrumb, [
      { label: "Books", onClick: () => setHash("books") },
      { label: state.book.title, onClick: () => setHash("book", { book: state.book.id }) },
      { label: "Tests" },
    ]);

    await renderTestView(main, {
      book: state.book,
      onBack(bookId) {
        if (bookId) setHash("book", { book: bookId });
        else setHash("books");
      },
      onBookLoaded(book) {
        state.book = book;
        setBreadcrumb(breadcrumb, [
          { label: "Books", onClick: () => setHash("books") },
          { label: book.title, onClick: () => setHash("book", { book: book.id }) },
          { label: "Tests" },
        ]);
      },
    });
    return;
  }

  // ── Recording panel ───────────────────────────────────────────────────────
  if (path === "record") {
    const bookId = parseInt(params.book, 10);
    const appendToRecordingId = parseInt(params.append, 10) || null;
    if (!bookId) { setHash("books"); return; }

    // If we don't have the book in state (e.g. hard link / refresh), fetch it
    if (!state.book || state.book.id !== bookId) {
      try {
        state.book = await api.get(`/api/books/${bookId}`);
      } catch (_) {
        showToast("Book not found — returning to list.", "error");
        setHash("books");
        return;
      }
    }

    setBreadcrumb(breadcrumb, [
      { label: "Books", onClick: () => setHash("books") },
      { label: state.book.title, onClick: () => setHash("book", { book: state.book.id }) },
      { label: appendToRecordingId ? "Continue Recording" : "New Recording" },
    ]);

    await renderRecordingPanel(main, {
      book: state.book,
      appendToRecordingId,
      onBack() { setHash("book", { book: state.book.id }); },
      onViewChapters(recordingId) {
        state.recordingId = recordingId;
        setHash("chapters", { rec: recordingId });
      },
    });
    return;
  }

  // ── Chapter view ──────────────────────────────────────────────────────────
  if (path === "chapters") {
    const recId = parseInt(params.rec, 10);
    if (!recId) { setHash("books"); return; }

    state.recordingId = recId;

    setBreadcrumb(breadcrumb, [
      { label: "Books", onClick: () => setHash("books") },
      { label: state.book?.title || "Book", onClick: state.book
          ? () => setHash("book", { book: state.book.id })
          : null },
      { label: "Results" },
    ]);

    await renderChapterView(main, {
      recordingId: recId,
      onBack(bookId) {
        if (bookId) setHash("book", { book: bookId });
        else setHash("books");
      },
      onBookLoaded(book) {
        state.book = book;
        setBreadcrumb(breadcrumb, [
          { label: "Books", onClick: () => setHash("books") },
          { label: book.title, onClick: () => setHash("book", { book: book.id }) },
          { label: "Results" },
        ]);
      },
      onNewRecording(book) {
        state.book = book;
        setHash("record", { book: book.id });
      },
    });
    return;
  }

  // Fallback
  setHash("books");
}

// ---------------------------------------------------------------------------
// Breadcrumb helper
// ---------------------------------------------------------------------------

/**
 * @param {HTMLElement} el
 * @param {Array<{label: string, onClick?: () => void}>} crumbs
 */
function setBreadcrumb(el, crumbs) {
  el.innerHTML = "";
  crumbs.forEach((crumb, idx) => {
    if (idx > 0) {
      const sep = document.createElement("span");
      sep.className = "breadcrumb-sep";
      sep.textContent = "/";
      el.appendChild(sep);
    }

    if (crumb.onClick) {
      const btn = document.createElement("button");
      btn.textContent = crumb.label;
      btn.addEventListener("click", crumb.onClick);
      el.appendChild(btn);
    } else {
      const span = document.createElement("span");
      span.textContent = crumb.label;
      if (idx === crumbs.length - 1) span.style.fontWeight = "600";
      el.appendChild(span);
    }
  });
}

// ---------------------------------------------------------------------------
// Toast notification system
// ---------------------------------------------------------------------------

/**
 * Show a toast notification.
 *
 * @param {string} message
 * @param {'info'|'success'|'error'} type
 * @param {number} duration   — ms to display (default 3500)
 */
export function showToast(message, type = "info", duration = 3500) {
  const container = document.getElementById("toast-container");
  if (!container) return;

  const toast = document.createElement("div");
  toast.className = `toast${type === "error" ? " toast-error" : type === "success" ? " toast-success" : ""}`;
  toast.textContent = message;

  container.appendChild(toast);

  setTimeout(() => {
    toast.classList.add("removing");
    toast.addEventListener("animationend", () => toast.remove(), { once: true });
  }, duration);
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

window.addEventListener("hashchange", () => route().catch(console.error));

// Remove initial loader and start routing
document.addEventListener("DOMContentLoaded", () => {
  const loader = document.getElementById("initial-loader");
  if (loader) loader.remove();
  route().catch(console.error);
});
