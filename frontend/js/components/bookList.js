/**
 * bookList.js — Book list view.
 *
 * Renders the list of books, a "+ New Book" button, and an inline modal
 * for adding a new book.
 *
 * Exported:
 *   renderBookList(container, { onSelectBook })
 */

import { api, ApiError } from "../api.js";
import { showToast } from "../app.js";

/**
 * Render the books list into `container`.
 *
 * @param {HTMLElement} container
 * @param {object} opts
 * @param {(book: object) => void} opts.onSelectBook  — called when user opens a book
 */
export async function renderBookList(container, { onSelectBook }) {
  // Show local loading state
  container.innerHTML = `
    <div class="books-header">
      <h2>My Books</h2>
      <button class="btn-primary" id="btn-new-book">+ New Book</button>
    </div>
    <div class="loading-spinner">
      <div class="spinner"></div>
      <p>Loading books…</p>
    </div>
  `;

  // Wire the "New Book" button before fetch so it works immediately
  container.querySelector("#btn-new-book").addEventListener("click", () => {
    openAddBookModal(container, { onSelectBook });
  });

  let books;
  try {
    books = await api.get("/api/books");
  } catch (err) {
    container.querySelector(".loading-spinner").outerHTML = `
      <p class="text-muted">Failed to load books: ${escHtml(err.detail || err.message)}</p>
    `;
    return;
  }

  renderBooksGrid(container, books, { onSelectBook });
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Render the grid of book cards (replaces the loading spinner).
 */
function renderBooksGrid(container, books, { onSelectBook }) {
  const spinner = container.querySelector(".loading-spinner");
  if (spinner) spinner.remove();

  // Remove any existing grid
  const existing = container.querySelector(".book-grid, .empty-state");
  if (existing) existing.remove();

  if (books.length === 0) {
    container.insertAdjacentHTML(
      "beforeend",
      `<div class="empty-state">
        <div class="empty-icon">📖</div>
        <h3>No books yet</h3>
        <p>Add your first book to start recording summaries.</p>
        <button class="btn-primary" id="btn-empty-new-book">+ Add First Book</button>
      </div>`
    );
    container.querySelector("#btn-empty-new-book").addEventListener("click", () => {
      openAddBookModal(container, { onSelectBook });
    });
    return;
  }

  const grid = document.createElement("div");
  grid.className = "book-grid";

  for (const book of books) {
    const card = createBookCard(book, { onSelectBook, onDeleted: () => {
      // Re-render after deletion
      renderBookList(container, { onSelectBook });
    }});
    grid.appendChild(card);
  }

  container.appendChild(grid);
}

/**
 * Build a single book card element.
 */
function createBookCard(book, { onSelectBook, onDeleted }) {
  const card = document.createElement("div");
  card.className = "book-card";
  card.innerHTML = `
    <div class="book-card-title">${escHtml(book.title)}</div>
    ${book.author
      ? `<div class="book-card-author">by ${escHtml(book.author)}</div>`
      : `<div class="book-card-author text-muted">No author</div>`}
    <div class="book-card-meta">Added ${formatDate(book.created_at)}</div>
    <div class="book-card-actions">
      <button class="btn-primary btn-record">Open Book</button>
      <button class="btn-ghost btn-sm btn-delete btn-icon" title="Delete book">🗑</button>
    </div>
  `;

  card.querySelector(".btn-record").addEventListener("click", () => {
    onSelectBook(book);
  });

  card.querySelector(".btn-delete").addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!confirm(`Delete "${book.title}"? This will remove all recordings.`)) return;
    try {
      await api.delete(`/api/books/${book.id}`);
      showToast(`"${book.title}" deleted.`, "success");
      onDeleted();
    } catch (err) {
      showToast(`Delete failed: ${err.detail || err.message}`, "error");
    }
  });

  return card;
}

/**
 * Open the "Add Book" modal, appended to document.body.
 */
function openAddBookModal(listContainer, { onSelectBook }) {
  const overlay = document.createElement("div");
  overlay.className = "add-book-overlay";
  overlay.innerHTML = `
    <div class="add-book-modal" role="dialog" aria-modal="true" aria-label="Add new book">
      <h3>Add New Book</h3>
      <div class="form-group">
        <label for="book-title-input">Title <span style="color:var(--color-danger)">*</span></label>
        <input id="book-title-input" type="text" placeholder="e.g. The Pragmatic Programmer"
               maxlength="512" autocomplete="off" />
      </div>
      <div class="form-group">
        <label for="book-author-input">Author</label>
        <input id="book-author-input" type="text" placeholder="e.g. David Thomas"
               maxlength="512" autocomplete="off" />
      </div>
      <div id="modal-error" style="color:var(--color-danger);font-size:.88rem;min-height:1.2em"></div>
      <div class="modal-actions">
        <button class="btn-ghost" id="btn-modal-cancel">Cancel</button>
        <button class="btn-primary" id="btn-modal-save">Add Book</button>
      </div>
    </div>
  `;

  document.body.appendChild(overlay);

  const titleInput  = overlay.querySelector("#book-title-input");
  const authorInput = overlay.querySelector("#book-author-input");
  const errorEl     = overlay.querySelector("#modal-error");
  const saveBtn     = overlay.querySelector("#btn-modal-save");
  const cancelBtn   = overlay.querySelector("#btn-modal-cancel");

  titleInput.focus();

  function closeModal() {
    document.body.removeChild(overlay);
  }

  // Close on overlay click (outside modal)
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeModal();
  });

  // ESC key
  function onKey(e) {
    if (e.key === "Escape") { closeModal(); document.removeEventListener("keydown", onKey); }
  }
  document.addEventListener("keydown", onKey);

  cancelBtn.addEventListener("click", () => {
    closeModal();
    document.removeEventListener("keydown", onKey);
  });

  saveBtn.addEventListener("click", async () => {
    const title = titleInput.value.trim();
    if (!title) {
      errorEl.textContent = "Title is required.";
      titleInput.focus();
      return;
    }

    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    errorEl.textContent = "";

    try {
      const book = await api.post("/api/books", {
        title,
        author: authorInput.value.trim() || null,
      });
      closeModal();
      document.removeEventListener("keydown", onKey);
      showToast(`"${book.title}" added!`, "success");
      // Re-render book list
      renderBookList(listContainer, { onSelectBook });
    } catch (err) {
      errorEl.textContent = err.detail || err.message;
      saveBtn.disabled = false;
      saveBtn.textContent = "Add Book";
    }
  });

  // Allow Enter to submit
  [titleInput, authorInput].forEach((el) => {
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") saveBtn.click();
    });
  });
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

function formatDate(isoStr) {
  if (!isoStr) return "—";
  try {
    return new Date(isoStr).toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch (_) { return isoStr; }
}
