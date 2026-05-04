/**
 * summaryRenderer.js — Markdown and Mermaid rendering helpers for summaries.
 */

let mermaidInitialized = false;
let mermaidRenderCounter = 0;
let mermaidModalEscapeHandler = null;
let mermaidModalResizeHandler = null;

export function renderSummaryMarkdown(markdownText) {
  const text = String(markdownText ?? "").trim();
  if (!text) return "";

  if (!window.marked || !window.DOMPurify) return renderBasicMarkdown(text);

  try {
    window.marked.setOptions({
      async: false,
      breaks: true,
      gfm: true,
    });

    const rawHtml = window.marked.parse(text);
    return window.DOMPurify.sanitize(rawHtml, {
      ADD_ATTR: ["target", "rel", "class"],
    });
  } catch (err) {
    console.error("Markdown render failed", err);
    return renderBasicMarkdown(text);
  }
}

export function renderSummaryWithTranscriptGraphs(summaryText, transcriptionText) {
  const summary = String(summaryText ?? "");
  const transcription = String(transcriptionText ?? "");
  const transcriptGraphs = extractMermaidBlocks(transcription);
  const summaryGraphs = extractMermaidBlocks(summary);
  const summaryGraphKeys = new Set(summaryGraphs.flatMap(splitMermaidDiagrams).map(normalizeMermaidKey));
  const uniqueTranscriptGraphs = transcriptGraphs.filter(
    (diagram) => splitMermaidDiagrams(diagram).some((part) => !summaryGraphKeys.has(normalizeMermaidKey(part)))
  );

  let markdown = summary.trim();
  if (uniqueTranscriptGraphs.length && !summaryGraphs.length) {
    markdown += [
      "",
      "### Диаграммы из транскриба",
      "",
      ...uniqueTranscriptGraphs.map((diagram) => `\`\`\`mermaid\n${diagram}\n\`\`\``),
    ].join("\n");
  }

  return renderSummaryMarkdown(markdown);
}

export async function renderMermaidDiagrams(root = document, retries = 20) {
  const codeBlocks = root.querySelectorAll(
    ".summary-markdown pre > code.language-mermaid"
  );

  if (!codeBlocks.length) return;

  if (!window.mermaid) {
    if (retries > 0) {
      window.setTimeout(() => renderMermaidDiagrams(root, retries - 1), 150);
      return;
    }

    for (const code of codeBlocks) {
      const pre = code.closest("pre");
      if (pre) pre.classList.add("mermaid-source-unrendered");
    }
    return;
  }

  if (!mermaidInitialized) {
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "default",
      flowchart: {
        htmlLabels: true,
        useMaxWidth: false,
      },
    });
    mermaidInitialized = true;
  }

  for (const code of codeBlocks) {
    const pre = code.closest("pre");
    if (!pre || pre.dataset.mermaidRendered === "true") continue;

    pre.dataset.mermaidRendered = "true";
    const sourceDiagram = code.textContent || "";
    const diagrams = splitMermaidDiagrams(sourceDiagram);
    const renderedFrames = [];

    for (const [index, diagram] of diagrams.entries()) {
      const renderDiagram = repairMermaidDiagram(diagram);
      const container = document.createElement("div");
      container.className = "mermaid-diagram";
      container.setAttribute("aria-label", "Mermaid diagram rendered from summary");

      try {
      const id = `summary-mermaid-${Date.now()}-${++mermaidRenderCounter}`;
      const result = await window.mermaid.render(id, renderDiagram);

      // GitHub-like path: trust Mermaid's own strict-mode renderer and insert
      // the generated SVG as-is. Re-sanitizing Mermaid output here breaks
      // flowchart labels because DOMPurify can strip/alter foreignObject HTML.
      container.innerHTML = result.svg;
      prepareMermaidSvg(container);
      result.bindFunctions?.(container);
        renderedFrames.push(
          createMermaidFrame(
            container,
            diagram,
            renderDiagram,
            getMermaidDiagramTitle(diagram) || (diagrams.length > 1 ? `Граф связей ${index + 1}` : "Граф связей")
          )
        );
      } catch (err) {
        console.error("Mermaid render failed", err);
        pre.dataset.mermaidRendered = "false";
        pre.classList.add("mermaid-render-error");
        const message = document.createElement("div");
        message.className = "mermaid-error-message";
        message.textContent = `Could not render Mermaid diagram${diagrams.length > 1 ? ` #${index + 1}` : ""}. Showing source.`;
        pre.before(message);
      }
    }

    if (renderedFrames.length) {
      pre.replaceWith(...renderedFrames);
    }
  }
}

function createMermaidFrame(container, sourceDiagram, renderDiagram, titleText = "Граф связей") {
  const frame = document.createElement("figure");
  frame.className = "mermaid-frame";

  const toolbar = document.createElement("figcaption");
  toolbar.className = "mermaid-toolbar";

  const icon = document.createElement("span");
  icon.className = "mermaid-toolbar-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = "🕸️";

  const title = document.createElement("span");
  title.className = "mermaid-toolbar-title";
  title.textContent = titleText;

  const hint = document.createElement("span");
  hint.className = "mermaid-toolbar-hint";
  hint.textContent = "Большой холст: скроллите по горизонтали или откройте fullscreen";

  const chips = document.createElement("div");
  chips.className = "mermaid-toolbar-chips";
  for (const labelText of ["↔️ drag / scroll", "⛶ focus mode"]) {
    const chip = document.createElement("span");
    chip.textContent = labelText;
    chips.append(chip);
  }

  const button = document.createElement("button");
  button.type = "button";
  button.className = "mermaid-fullscreen-btn";
  button.textContent = "⛶ Развернуть граф";
  button.addEventListener("click", () => openMermaidModal(container, sourceDiagram, renderDiagram, titleText));

  const label = document.createElement("div");
  label.className = "mermaid-toolbar-label";
  label.append(title, hint, chips);

  const titleWrap = document.createElement("div");
  titleWrap.className = "mermaid-toolbar-title-wrap";
  titleWrap.append(icon, label);

  toolbar.append(titleWrap, button);
  frame.append(toolbar, container);
  return frame;
}

function prepareMermaidSvg(container) {
  const svg = container.querySelector("svg");
  if (!svg) return;

  svg.removeAttribute("width");
  svg.removeAttribute("height");
  svg.setAttribute("role", "img");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  const viewBox = svg.getAttribute("viewBox");
  if (viewBox) {
    const [, , width, height] = viewBox.split(/\s+/).map(Number);
    if (Number.isFinite(width)) {
      svg.dataset.viewBoxWidth = String(width);
      svg.style.minWidth = `${Math.max(width, 920)}px`;
    }
    if (Number.isFinite(height)) svg.dataset.viewBoxHeight = String(height);
  }
}

function autoscaleMermaidModalSvg(modalBody) {
  const svg = modalBody?.querySelector(".mermaid-diagram-modal svg");
  if (!svg) return;

  const viewBoxWidth = Number(svg.dataset.viewBoxWidth);
  if (!Number.isFinite(viewBoxWidth) || viewBoxWidth <= 0) return;

  const bodyRect = modalBody.getBoundingClientRect();
  const availableWidth = Math.max(bodyRect.width - 64, window.innerWidth * 0.65);
  const zoom = window.innerWidth >= 2200 ? 2.05 : window.innerWidth >= 1500 ? 1.85 : 1.65;
  const targetWidth = Math.round(Math.max(viewBoxWidth * zoom, availableWidth * 1.1));

  svg.style.width = `${targetWidth}px`;
  svg.style.minWidth = `${targetWidth}px`;
  svg.style.maxWidth = "none";
}

function openMermaidModal(sourceContainer, sourceDiagram, renderDiagram, titleText = "Граф связей") {
  closeMermaidModal();

  const modal = document.createElement("div");
  modal.className = "mermaid-modal";
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.setAttribute("aria-label", "Граф связей на весь экран");

  const panel = document.createElement("div");
  panel.className = "mermaid-modal-panel";

  const header = document.createElement("div");
  header.className = "mermaid-modal-header";

  const title = document.createElement("h2");
  title.textContent = titleText || "Граф связей";

  const subtitle = document.createElement("p");
  subtitle.textContent = "Fullscreen canvas for wide diagrams — use wheel, trackpad, or scrollbars to pan.";

  const titleBlock = document.createElement("div");
  titleBlock.className = "mermaid-modal-title-block";
  titleBlock.append(title, subtitle);

  const actions = document.createElement("div");
  actions.className = "mermaid-modal-actions";

  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.className = "mermaid-modal-close";
  closeButton.textContent = "Esc / Закрыть";
  closeButton.addEventListener("click", closeMermaidModal);

  actions.append(closeButton);
  header.append(titleBlock, actions);

  const body = document.createElement("div");
  body.className = "mermaid-modal-body";

  const diagramClone = sourceContainer.cloneNode(true);
  diagramClone.classList.add("mermaid-diagram-modal");
  prepareMermaidSvg(diagramClone);
  body.append(diagramClone);

  const source = document.createElement("details");
  source.className = "mermaid-modal-source";
  source.innerHTML = `<summary>Показать исходный Mermaid-код</summary><pre><code>${escHtml(sourceDiagram)}</code></pre>`;

  if (sourceDiagram !== renderDiagram) {
    const repaired = document.createElement("details");
    repaired.className = "mermaid-modal-source";
    repaired.innerHTML = `<summary>Показать Mermaid после автопочинки</summary><pre><code>${escHtml(renderDiagram)}</code></pre>`;
    panel.append(header, body, source, repaired);
    modal.append(panel);
  } else {
    panel.append(header, body, source);
    modal.append(panel);
  }

  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeMermaidModal();
  });

  mermaidModalEscapeHandler = (event) => {
    if (event.key === "Escape") closeMermaidModal();
  };

  document.addEventListener("keydown", mermaidModalEscapeHandler);
  document.body.append(modal);
  document.body.classList.add("has-mermaid-modal");
  autoscaleMermaidModalSvg(body);
  mermaidModalResizeHandler = () => autoscaleMermaidModalSvg(body);
  window.addEventListener("resize", mermaidModalResizeHandler);
  closeButton.focus();
}

function closeMermaidModal() {
  const modal = document.querySelector(".mermaid-modal");
  if (modal) modal.remove();
  document.body.classList.remove("has-mermaid-modal");

  if (mermaidModalEscapeHandler) {
    document.removeEventListener("keydown", mermaidModalEscapeHandler);
    mermaidModalEscapeHandler = null;
  }

  if (mermaidModalResizeHandler) {
    window.removeEventListener("resize", mermaidModalResizeHandler);
    mermaidModalResizeHandler = null;
  }
}

function extractMermaidBlocks(text) {
  const blocks = [];
  const fenced = /```mermaid\s*\n([\s\S]*?)```/gi;
  let match;
  while ((match = fenced.exec(text)) !== null) {
    const diagram = match[1].trim();
    if (diagram) blocks.push(diagram);
  }

  return blocks;
}

function splitMermaidDiagrams(diagram) {
  const lines = String(diagram ?? "").split(/\r?\n/);
  const parts = [];
  let current = [];
  let seenDiagramStart = false;
  let openSubgraphs = 0;

  for (const line of lines) {
    const trimmed = line.trim();
    const startsDiagram = isMermaidDiagramStart(trimmed);

    if (startsDiagram && seenDiagramStart && openSubgraphs === 0 && current.some((item) => item.trim())) {
      parts.push(current.join("\n").trim());
      current = [];
    }

    if (startsDiagram) seenDiagramStart = true;
    if (/^subgraph\b/i.test(trimmed)) openSubgraphs += 1;
    if (/^end\b/i.test(trimmed) && openSubgraphs > 0) openSubgraphs -= 1;
    current.push(line);
  }

  const last = current.join("\n").trim();
  if (last) parts.push(last);

  return (parts.length ? parts : [String(diagram ?? "").trim()].filter(Boolean))
    .flatMap(splitFlowchartSubgraphs);
}

function splitFlowchartSubgraphs(diagram) {
  const lines = String(diagram ?? "").split(/\r?\n/);
  const headerIndex = lines.findIndex((line) => /^(flowchart|graph)\b/i.test(line.trim()));
  if (headerIndex < 0) return [diagram];

  const header = lines[headerIndex].trim();
  const prefix = lines.slice(0, headerIndex + 1);
  const subgraphs = [];
  let current = null;
  let depth = 0;
  let outsideContent = [];

  for (const line of lines.slice(headerIndex + 1)) {
    const trimmed = line.trim();

    if (/^subgraph\b/i.test(trimmed) && depth === 0) {
      current = [line];
      depth = 1;
      continue;
    }

    if (current) {
      current.push(line);
      if (/^subgraph\b/i.test(trimmed)) depth += 1;
      if (/^end\b/i.test(trimmed)) depth -= 1;

      if (depth === 0) {
        subgraphs.push(current.join("\n").trim());
        current = null;
      }
      continue;
    }

    if (trimmed && !trimmed.startsWith("%%")) outsideContent.push(line);
  }

  if (current?.length) subgraphs.push(current.join("\n").trim());

  // Independent top-level subgraphs are rendered by Mermaid side-by-side in a
  // single wide SVG. Split them into separate frames so they stack vertically
  // and each graph can use the full summary width.
  if (subgraphs.length <= 1 || outsideContent.length) return [diagram];

  return subgraphs.map((subgraph) => [...prefix, subgraph].join("\n").trim());
}

function isMermaidDiagramStart(line) {
  return /^(flowchart|graph|sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|journey|gantt|pie|gitGraph|mindmap|timeline|quadrantChart|xychart-beta|block-beta|packet-beta|architecture-beta)\b/i.test(line);
}

function getMermaidDiagramTitle(diagram) {
  const lines = String(diagram ?? "").split(/\r?\n/);

  for (const line of lines) {
    const trimmed = line.trim();
    const titleMatch = trimmed.match(/^title\s+(.+)$/i);
    if (titleMatch?.[1]) return cleanMermaidTitle(titleMatch[1]);
  }

  for (const line of lines) {
    const trimmed = line.trim();
    const subgraphMatch = trimmed.match(/^subgraph\s+(.+)$/i);
    if (subgraphMatch?.[1]) return cleanMermaidTitle(subgraphMatch[1]);
  }

  return "";
}

function cleanMermaidTitle(title) {
  return String(title ?? "")
    .trim()
    .replace(/^ID\s*\[\s*(.+?)\s*\]$/i, "$1")
    .replace(/^\[\s*(.+?)\s*\]$/, "$1")
    .replace(/^\(\s*(.+?)\s*\)$/, "$1")
    .replace(/^\{\s*(.+?)\s*\}$/, "$1")
    .replace(/^['"]|['"]$/g, "")
    .trim();
}

function normalizeMermaidKey(diagram) {
  return repairMermaidDiagram(diagram)
    .replace(/%%.*$/gm, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

export function repairMermaidDiagram(diagram) {
  const lines = String(diagram ?? "")
    .replace(/\s+$/g, "")
    .split(/\r?\n/)
    .map(repairMermaidEdgeLabels);
  let openSubgraphs = 0;

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("%%")) continue;
    if (/^subgraph\b/i.test(trimmed)) openSubgraphs += 1;
    if (/^end\b/i.test(trimmed) && openSubgraphs > 0) openSubgraphs -= 1;
  }

  while (openSubgraphs > 0) {
    lines.push("end");
    openSubgraphs -= 1;
  }

  return lines.join("\n").trim();
}

function repairMermaidEdgeLabels(line) {
  const source = String(line ?? "");
  if (!source.includes("|")) return source;

  return source.replace(/\|([^|\n]+)\|/g, (match, label) => {
    const normalizedLabel = String(label).trim();
    if (!normalizedLabel || /^(["']).*\1$/.test(normalizedLabel)) return match;

    const escapedLabel = normalizedLabel
      .replace(/&quot;/g, '"')
      .replace(/"/g, "#quot;");
    return `|"${escapedLabel}"|`;
  });
}

function renderBasicMarkdown(markdown) {
  const lines = String(markdown ?? "").split(/\r?\n/);
  const html = [];
  let paragraph = [];
  let listType = null;
  let inFence = false;
  let fenceLang = "";
  let fenceLines = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    html.push(`<p>${renderInlineMarkdown(paragraph.join(" "))}</p>`);
    paragraph = [];
  };

  const closeList = () => {
    if (!listType) return;
    html.push(`</${listType}>`);
    listType = null;
  };

  const openList = (type) => {
    if (listType === type) return;
    closeList();
    html.push(`<${type}>`);
    listType = type;
  };

  for (const line of lines) {
    const fence = line.match(/^```(\w+)?\s*$/);
    if (fence) {
      if (inFence) {
        html.push(`<pre><code class="language-${escAttr(fenceLang)}">${escHtml(fenceLines.join("\n"))}</code></pre>`);
        inFence = false;
        fenceLang = "";
        fenceLines = [];
      } else {
        flushParagraph();
        closeList();
        inFence = true;
        fenceLang = fence[1] || "";
      }
      continue;
    }

    if (inFence) {
      fenceLines.push(line);
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      closeList();
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      closeList();
      const level = heading[1].length;
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    if (unordered) {
      flushParagraph();
      openList("ul");
      html.push(`<li>${renderInlineMarkdown(unordered[1])}</li>`);
      continue;
    }

    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ordered) {
      flushParagraph();
      openList("ol");
      html.push(`<li>${renderInlineMarkdown(ordered[1])}</li>`);
      continue;
    }

    const quote = line.match(/^>\s?(.+)$/);
    if (quote) {
      flushParagraph();
      closeList();
      html.push(`<blockquote>${renderInlineMarkdown(quote[1])}</blockquote>`);
      continue;
    }

    paragraph.push(line.trim());
  }

  if (inFence) html.push(`<pre><code class="language-${escAttr(fenceLang)}">${escHtml(fenceLines.join("\n"))}</code></pre>`);
  flushParagraph();
  closeList();
  return html.join("\n");
}

function renderInlineMarkdown(text) {
  return escHtml(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/_([^_]+)_/g, "<em>$1</em>");
}

function escAttr(str) {
  return String(str ?? "").replace(/[^a-z0-9_-]/gi, "");
}

function escHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
