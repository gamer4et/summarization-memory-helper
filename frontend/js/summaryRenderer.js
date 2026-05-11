/**
 * summaryRenderer.js — Markdown and Mermaid rendering helpers for summaries.
 */

let mermaidInitialized = false;
let mermaidRenderCounter = 0;
let mermaidModalEscapeHandler = null;
let mermaidModalResizeHandler = null;
let mermaidGlobalWheelHandlerAttached = false;
const MERMAID_LABEL_WRAP_AT = 24;
const MERMAID_ZOOM_MIN = 0.15;
const MERMAID_ZOOM_MAX = 4;
const MERMAID_ZOOM_DRAG_SENSITIVITY = 0.006;
const MERMAID_ZOOM_WHEEL_SENSITIVITY = 0.002;

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
        htmlLabels: false,
        markdownAutoWrap: true,
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
  hint.textContent = "Большой холст: тяните мышью для панорамы, Ctrl + колесо или drag вверх/вниз для масштаба";

  const chips = document.createElement("div");
  chips.className = "mermaid-toolbar-chips";
  for (const labelText of ["↔️ drag / scroll pan", "Ctrl + wheel / drag zoom", "⛶ focus mode"]) {
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
      const targetWidth = Math.max(width, 920);
      svg.style.width = `${targetWidth}px`;
      svg.style.minWidth = `${targetWidth}px`;
      svg.style.maxWidth = "none";
    }
    if (Number.isFinite(height)) svg.dataset.viewBoxHeight = String(height);
  }

  attachMermaidZoom(container);
}

function clampMermaidZoom(zoom) {
  return Math.min(MERMAID_ZOOM_MAX, Math.max(MERMAID_ZOOM_MIN, zoom));
}

function getMermaidSvgBaseWidth(svg) {
  const storedBaseWidth = Number(svg.dataset.baseWidth);
  if (Number.isFinite(storedBaseWidth) && storedBaseWidth > 0) return storedBaseWidth;

  const inlineWidth = Number.parseFloat(svg.style.width);
  const measuredWidth = svg.getBoundingClientRect().width;
  const viewBoxWidth = Number(svg.dataset.viewBoxWidth);
  const baseWidth = [inlineWidth, measuredWidth, viewBoxWidth, 920].find(
    (value) => Number.isFinite(value) && value > 0
  );

  svg.dataset.baseWidth = String(baseWidth);
  return baseWidth;
}

function setMermaidSvgBaseWidth(svg, baseWidth) {
  if (!Number.isFinite(baseWidth) || baseWidth <= 0) return;

  svg.dataset.baseWidth = String(baseWidth);
  applyMermaidSvgZoom(svg, Number(svg.dataset.zoom) || 1);
}

function applyMermaidSvgZoom(svg, zoom) {
  const baseWidth = getMermaidSvgBaseWidth(svg);
  const nextZoom = clampMermaidZoom(zoom);
  const targetWidth = Math.round(baseWidth * nextZoom);

  svg.dataset.zoom = String(nextZoom);
  svg.style.width = `${targetWidth}px`;
  svg.style.minWidth = `${targetWidth}px`;
  svg.style.maxWidth = "none";
}

function setMermaidSvgZoom(container, svg, zoom, anchorEvent) {
  const rect = container.getBoundingClientRect();
  const anchorX = anchorEvent ? anchorEvent.clientX - rect.left : rect.width / 2;
  const anchorY = anchorEvent ? anchorEvent.clientY - rect.top : rect.height / 2;
  const scrollRatioX = (container.scrollLeft + anchorX) / Math.max(container.scrollWidth, 1);
  const scrollRatioY = (container.scrollTop + anchorY) / Math.max(container.scrollHeight, 1);

  applyMermaidSvgZoom(svg, zoom);

  container.scrollLeft = Math.max(0, scrollRatioX * container.scrollWidth - anchorX);
  container.scrollTop = Math.max(0, scrollRatioY * container.scrollHeight - anchorY);
}

function attachMermaidZoom(container) {
  const svg = container.querySelector("svg");
  if (!svg || container.dataset.mermaidZoomAttached === "true") return;

  ensureMermaidGlobalWheelHandler();
  container.dataset.mermaidZoomAttached = "true";
  container.title = "Тяните мышью — двигать граф; Ctrl + drag вверх/вниз — изменить масштаб";

  let interactionState = null;

  container.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;

    event.preventDefault();
    interactionState = {
      mode: event.ctrlKey ? "zoom" : "pan",
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startScrollLeft: container.scrollLeft,
      startScrollTop: container.scrollTop,
      startZoom: Number(svg.dataset.zoom) || 1,
    };
    container.classList.toggle("mermaid-zooming", interactionState.mode === "zoom");
    container.classList.toggle("mermaid-panning", interactionState.mode === "pan");
    container.setPointerCapture?.(event.pointerId);
  });

  container.addEventListener("pointermove", (event) => {
    if (!interactionState || event.pointerId !== interactionState.pointerId) return;

    event.preventDefault();
    if (interactionState.mode === "zoom") {
      const dragDelta = interactionState.startY - event.clientY;
      const nextZoom = interactionState.startZoom * Math.exp(dragDelta * MERMAID_ZOOM_DRAG_SENSITIVITY);
      setMermaidSvgZoom(container, svg, nextZoom, event);
      return;
    }

    container.scrollLeft = interactionState.startScrollLeft - (event.clientX - interactionState.startX);
    container.scrollTop = interactionState.startScrollTop - (event.clientY - interactionState.startY);
  });

  const stopPointerInteraction = (event) => {
    if (!interactionState || event.pointerId !== interactionState.pointerId) return;

    container.classList.remove("mermaid-zooming");
    container.classList.remove("mermaid-panning");
    container.releasePointerCapture?.(event.pointerId);
    interactionState = null;
  };

  container.addEventListener("pointerup", stopPointerInteraction);
  container.addEventListener("pointercancel", stopPointerInteraction);
  container.addEventListener("lostpointercapture", () => {
    container.classList.remove("mermaid-zooming");
    container.classList.remove("mermaid-panning");
    interactionState = null;
  });

  container.addEventListener(
    "wheel",
    (event) => {
      if (!event.ctrlKey) return;

      zoomMermaidFromWheelEvent(container, svg, event);
    },
    { passive: false }
  );
}

function ensureMermaidGlobalWheelHandler() {
  if (mermaidGlobalWheelHandlerAttached) return;

  mermaidGlobalWheelHandlerAttached = true;
  document.addEventListener(
    "wheel",
    (event) => {
      if (!event.ctrlKey) return;

      const container = findMermaidZoomContainer(event.target);
      const svg = container?.querySelector("svg");
      if (!container || !svg) return;

      zoomMermaidFromWheelEvent(container, svg, event);
    },
    { capture: true, passive: false }
  );
}

function findMermaidZoomContainer(target) {
  if (!(target instanceof Element)) return null;

  const directContainer = target.closest(".mermaid-diagram");
  if (directContainer) return directContainer;

  const frame = target.closest(".mermaid-frame");
  if (frame) return frame.querySelector(".mermaid-diagram");

  const modalPanel = target.closest(".mermaid-modal-panel");
  if (modalPanel) return modalPanel.querySelector(".mermaid-diagram-modal");

  return null;
}

function zoomMermaidFromWheelEvent(container, svg, event) {
  event.preventDefault();
  event.stopPropagation();
  event.stopImmediatePropagation?.();

  const currentZoom = Number(svg.dataset.zoom) || 1;
  const nextZoom = currentZoom * Math.exp(-event.deltaY * MERMAID_ZOOM_WHEEL_SENSITIVITY);
  setMermaidSvgZoom(container, svg, nextZoom, event);
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

  setMermaidSvgBaseWidth(svg, targetWidth);
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
  subtitle.textContent = "Fullscreen canvas: drag with left mouse button to pan; hold Ctrl and use wheel or drag up/down to zoom.";

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
  delete diagramClone.dataset.mermaidZoomAttached;
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

  const header = normalizeFlowchartHeader(lines[headerIndex].trim());
  const prefix = [...lines.slice(0, headerIndex), header];
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

  // Top-level subgraphs are rendered by Mermaid side-by-side in a single wide
  // SVG. Split them into separate frames so each graph is horizontal by itself
  // and the frames stack vertically in the summary.
  if (subgraphs.length <= 1) return [normalizeFlowchartDirection(diagram)];

  return subgraphs.map((subgraph) => [...prefix, subgraph].join("\n").trim());
}

function normalizeFlowchartDirection(diagram) {
  const lines = String(diagram ?? "").split(/\r?\n/);
  const headerIndex = lines.findIndex((line) => /^(flowchart|graph)\b/i.test(line.trim()));
  if (headerIndex < 0) return diagram;

  lines[headerIndex] = normalizeFlowchartHeader(lines[headerIndex].trim());
  return lines.join("\n").trim();
}

function normalizeFlowchartHeader(header) {
  if (/^(flowchart|graph)\s+(LR|RL)\b/i.test(header)) return header;
  if (/^graph\b/i.test(header)) return header.replace(/^graph(?:\s+\w+)?/i, "graph LR");
  return header.replace(/^flowchart(?:\s+\w+)?/i, "flowchart LR");
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
  const normalizedDiagram = normalizeFlowchartDirection(diagram);
  const lines = String(normalizedDiagram ?? "")
    .replace(/\s+$/g, "")
    .split(/\r?\n/)
    .map((line) => repairMermaidNodeLabels(repairMermaidEdgeLabels(line)));
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

function repairMermaidNodeLabels(line) {
  const source = String(line ?? "");
  const trimmed = source.trim();
  if (!trimmed || trimmed.startsWith("%%") || !isFlowchartNodeLine(trimmed)) return source;

  let output = "";
  let index = 0;

  while (index < source.length) {
    const match = source.slice(index).match(/^([A-Za-z_][\w-]*)(\s*)(\[\[|\[\(|\[|\(\[|\(\(|\(|\{\{|\{)/);
    if (!match) {
      output += source[index];
      index += 1;
      continue;
    }

    const matchStart = index;
    const nodeId = match[1];
    const whitespace = match[2];
    const opener = match[3];
    const labelStart = matchStart + nodeId.length + whitespace.length + opener.length;
    const closer = getMermaidShapeCloser(opener);
    const labelEnd = findMermaidLabelEnd(source, labelStart, closer);

    if (labelEnd < 0) {
      output += source[index];
      index += 1;
      continue;
    }

    const label = source.slice(labelStart, labelEnd);
    output += `${nodeId}${whitespace}${opener}${wrapMermaidNodeLabel(label)}${closer}`;
    index = labelEnd + closer.length;
  }

  return output;
}

function isFlowchartNodeLine(trimmed) {
  return !/^(flowchart|graph|subgraph|end\b|classDef\b|class\b|style\b|linkStyle\b|click\b|accTitle\b|accDescr\b|title\b)/i.test(trimmed);
}

function getMermaidShapeCloser(opener) {
  return {
    "[[": "]]",
    "[(": ")]",
    "[": "]",
    "([": "])",
    "((": "))",
    "(": ")",
    "{{": "}}",
    "{": "}",
  }[opener] || opener;
}

function findMermaidLabelEnd(source, start, closer) {
  let quote = "";
  let backtick = false;

  for (let index = start; index < source.length; index += 1) {
    const char = source[index];
    const prev = source[index - 1];

    if (char === "`" && prev !== "\\") backtick = !backtick;
    if (!backtick && (char === '"' || char === "'") && prev !== "\\") {
      quote = quote === char ? "" : quote || char;
    }

    if (!quote && !backtick && source.startsWith(closer, index)) return index;
  }

  return -1;
}

function wrapMermaidNodeLabel(label) {
  const parsed = parseMermaidLabel(label);
  if (!parsed || shouldKeepMermaidLabel(parsed.text)) return label;

  const wrapped = wrapTextForMermaidLabel(parsed.text);
  if (wrapped === parsed.text) return label;

  return `"\`${escapeMermaidMarkdownLabel(wrapped)}\`"`;
}

function parseMermaidLabel(label) {
  const raw = String(label ?? "");
  const trimmed = raw.trim();
  if (!trimmed) return null;

  const quoted = trimmed.match(/^(["'])([\s\S]*)\1$/);
  const text = quoted ? quoted[2] : trimmed;
  const markdown = text.match(/^`([\s\S]*)`$/);
  return { text: markdown ? markdown[1] : text };
}

function shouldKeepMermaidLabel(text) {
  const value = String(text ?? "").trim();
  return (
    !value ||
    value.length <= MERMAID_LABEL_WRAP_AT ||
    /\r?\n|<br\s*\/?\s*>/i.test(value) ||
    /`/.test(value)
  );
}

function wrapTextForMermaidLabel(text) {
  const normalized = String(text ?? "").replace(/\s+/g, " ").trim();
  if (normalized.length <= MERMAID_LABEL_WRAP_AT) return normalized;

  const lines = [];
  let current = "";

  for (const word of normalized.split(" ")) {
    const candidate = current ? `${current} ${word}` : word;
    if (candidate.length <= MERMAID_LABEL_WRAP_AT || !current) {
      current = candidate;
      continue;
    }

    lines.push(current);
    current = word;
  }

  if (current) lines.push(current);
  return lines.join("\n");
}

function escapeMermaidMarkdownLabel(text) {
  return String(text ?? "")
    .replace(/&quot;/g, '"')
    .replace(/"/g, "#quot;");
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
