// ScrapeFlow – flow editor on top of Drawflow (https://github.com/jerosoler/Drawflow, MIT).
// The server keeps flows as {nodes: [{id, type, x, y, config}], edges: [{from, out, to}]};
// this file converts between that and Drawflow's own format.
(function () {
  "use strict";
  const app = document.getElementById("flow-app");
  const siteId = app.dataset.site;
  const types = Object.fromEntries(JSON.parse(document.getElementById("node-types").textContent).map(t => [t.type, t]));
  const initial = JSON.parse(document.getElementById("flow-data").textContent);

  const container = document.getElementById("drawflow");
  const editor = new Drawflow(container);
  editor.reroute = false;
  editor.zoom_min = 0.35;
  editor.start();

  const stateEl = document.getElementById("flow-state");
  const notesEl = document.getElementById("flow-notes");
  const inspector = document.getElementById("inspector");
  let dirty = false, loading = false, selected = null;

  // ------------------------------------------------------------------ helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (name) => `<svg class="icon sm" aria-hidden="true"><use href="#i-${name}"/></svg>`;

  function setDirty(value) {
    if (loading) return;
    dirty = value;
    stateEl.textContent = value ? "Unsaved changes" : "Saved";
    stateEl.className = "pill no-dot" + (value ? " warn" : "");
  }

  function summary(type, config) {
    const c = config || {};
    switch (type) {
      case "source": return "New and changed files";
      case "filter": return [c.extensions && `types: ${c.extensions}`, c.pattern && `name ~ /${c.pattern}/`].filter(Boolean).join(" · ") || "Lets everything through";
      case "ai_filter": return c.instructions ? `“${c.instructions}”` : "Describe which files to keep";
      case "markdown": return c.on_error === "pass" ? "Unconvertible files pass on" : "Unconvertible files are skipped";
      case "code": {
        const lines = (c.code || "").split("\n").filter(l => l.trim() && !l.trim().startsWith("#"));
        return `${lines.length} line(s) of Python · ${c.timeout || 60} s limit`;
      }
      case "storage": return `Folder: ${c.folder || "{site}"}`;
    }
    return "";
  }

  function nodeHtml(type, config) {
    const t = types[type];
    return `<div class="fn" style="--nc:${t.color}">
      <div class="fn-head"><span class="fn-icon">${icon(t.icon)}</span><span class="fn-title">${esc(t.label)}</span></div>
      <div class="fn-sum">${esc(summary(type, config))}</div>
    </div>`;
  }

  function labelOutputs(dfId, type) {
    const el = document.getElementById(`node-${dfId}`);
    if (!el) return;
    types[type].outputs.forEach((name, i) => {
      const out = el.querySelector(`.output.output_${i + 1}`);
      if (out && types[type].outputs.length > 1 && !out.querySelector(".out-label")) {
        const label = document.createElement("span");
        label.className = "out-label " + name;
        label.textContent = name;
        out.appendChild(label);
      }
    });
    el.classList.add(`type-${type}`);
  }

  function addNode(type, x, y, config, nid) {
    const t = types[type];
    const data = { nid: nid || `${type}_${Math.random().toString(36).slice(2, 7)}`, type, config: config || defaults(type) };
    const id = editor.addNode(type, t.inputs, t.outputs.length, x, y, `t-${type}`, data, nodeHtml(type, data.config));
    labelOutputs(id, type);
    return id;
  }

  function defaults(type) {
    const config = {};
    for (const f of types[type].fields) if (f.default !== null && f.default !== undefined) config[f.name] = f.default;
    return config;
  }

  // ------------------------------------------------------------------ load / export
  function load(flow) {
    loading = true;
    editor.clear();
    const ids = {};
    flow.nodes.forEach((n, i) => {
      if (!types[n.type]) return;
      ids[n.id] = addNode(n.type, n.x ?? 60 + i * 300, n.y ?? 160, n.config || {}, n.id);
    });
    for (const e of flow.edges) {
      const from = flow.nodes.find(n => n.id === e.from);
      if (!from || !(e.from in ids) || !(e.to in ids)) continue;
      const k = types[from.type].outputs.indexOf(e.out) + 1;
      if (k > 0) editor.addConnection(ids[e.from], ids[e.to], `output_${k}`, "input_1");
    }
    loading = false;
    setDirty(false);
    showInspector(null);
    requestAnimationFrame(fit);
  }

  // Zoom and centre so the whole flow is visible when it opens.
  function fit() {
    const nodes = Object.values(editor.export().drawflow.Home.data);
    if (!nodes.length) return;
    const W = container.clientWidth, H = container.clientHeight;
    const x0 = Math.min(...nodes.map(n => n.pos_x)), y0 = Math.min(...nodes.map(n => n.pos_y));
    const x1 = Math.max(...nodes.map(n => n.pos_x)) + 240, y1 = Math.max(...nodes.map(n => n.pos_y)) + 90;
    const zoom = Math.max(editor.zoom_min, Math.min(1, (W - 80) / (x1 - x0), (H - 80) / (y1 - y0)));
    // The canvas scales around its centre, so move the flow's centre onto the canvas centre.
    editor.zoom = editor.zoom_last_value = zoom;
    editor.canvas_x = (W / 2 - (x0 + x1) / 2) * zoom;
    editor.canvas_y = (H / 2 - (y0 + y1) / 2) * zoom;
    editor.precanvas.style.transform = `translate(${editor.canvas_x}px, ${editor.canvas_y}px) scale(${zoom})`;
  }

  function exportFlow() {
    const data = editor.export().drawflow.Home.data;
    const nodes = [], edges = [];
    for (const n of Object.values(data)) {
      const type = n.data.type;
      nodes.push({ id: n.data.nid, type, x: Math.round(n.pos_x), y: Math.round(n.pos_y), config: n.data.config || {} });
      Object.entries(n.outputs).forEach(([key, out]) => {
        const name = types[type].outputs[Number(key.split("_")[1]) - 1];
        for (const c of out.connections) edges.push({ from: n.data.nid, out: name, to: data[c.node].data.nid });
      });
    }
    return { nodes, edges };
  }

  // ------------------------------------------------------------------ palette
  const palette = document.getElementById("palette");
  for (const t of Object.values(types)) {
    if (t.singleton) continue;
    const item = document.createElement("div");
    item.className = "pal-item";
    item.draggable = true;
    item.dataset.type = t.type;
    item.style.setProperty("--nc", t.color);
    item.innerHTML = `<span class="fn-icon">${icon(t.icon)}</span><span><b>${esc(t.label)}</b><small>${esc(t.description)}</small></span>`;
    item.addEventListener("dragstart", (e) => e.dataTransfer.setData("node", t.type));
    item.addEventListener("click", () => {  // click = add right of the last node, so nodes don't pile up
      const nodes = Object.values(editor.export().drawflow.Home.data);
      const last = nodes.reduce((a, n) => (!a || n.id > a.id ? n : a), null);
      const rect = container.getBoundingClientRect();
      let [x, y] = canvasPoint(rect.x + rect.width / 2 - 120, rect.y + rect.height / 2 - 40);
      if (last) { x = last.pos_x + 300; y = last.pos_y + (nodes.length % 2 ? 40 : -40); }
      addNode(t.type, x, y);
      setDirty(true);
    });
    palette.appendChild(item);
  }

  function canvasPoint(clientX, clientY) {
    const pc = editor.precanvas;
    const scale = pc.clientWidth / (pc.clientWidth * editor.zoom);
    const rect = pc.getBoundingClientRect();
    return [clientX * scale - rect.x * scale, clientY * scale - rect.y * scale];
  }

  container.addEventListener("dragover", (e) => e.preventDefault());
  container.addEventListener("drop", (e) => {
    e.preventDefault();
    const type = e.dataTransfer.getData("node");
    if (!types[type]) return;
    addNode(type, ...canvasPoint(e.clientX - 20, e.clientY - 30));
    setDirty(true);
  });

  // The Website node can't be removed.
  const isSourceSelected = () => selected && editor.getNodeFromId(selected)?.data.type === "source";
  window.addEventListener("keydown", (e) => {
    if ((e.key === "Delete" || e.key === "Backspace") && isSourceSelected() && !e.target.closest("input, textarea, select")) {
      e.stopImmediatePropagation();
    }
  }, true);
  container.addEventListener("contextmenu", (e) => {
    const node = e.target.closest(".drawflow-node");
    if (node && node.classList.contains("type-source")) { e.stopImmediatePropagation(); e.preventDefault(); }
  }, true);

  // ------------------------------------------------------------------ inspector
  function showInspector(dfId) {
    selected = dfId;
    if (!dfId) {
      inspector.innerHTML = `<div class="inspector-empty"><span class="icon-wrap">${icon("settings")}</span><b>Select a node</b><p class="muted small">Its settings appear here.</p></div>`;
      return;
    }
    const node = editor.getNodeFromId(dfId);
    const t = types[node.data.type];
    const config = node.data.config || {};
    let html = `<div class="insp-head" style="--nc:${t.color}"><span class="fn-icon">${icon(t.icon)}</span><div><b>${esc(t.label)}</b><p class="muted small">${esc(t.description)}</p></div></div>`;
    if (t.type === "source") {
      html += `<div class="fields"><p class="small">Crawl settings (address, link depth, file types, schedule) are on the website's <a href="/sites/${siteId}?tab=settings">Settings</a> tab.</p>
        <p class="small muted">Connect its output to filters, converters, code and storage. Filters run on names and links before a file is downloaded.</p></div>`;
    } else {
      html += `<div class="fields">`;
      for (const f of t.fields) {
        const v = config[f.name] ?? f.default ?? "";
        const id = `f-${f.name}`;
        html += `<div class="field"><label for="${id}">${esc(f.label)}</label>`;
        if (f.kind === "textarea") html += `<textarea class="input" id="${id}" data-field="${f.name}" rows="3" placeholder="${esc(f.placeholder)}">${esc(v)}</textarea>`;
        else if (f.kind === "code") html += `<textarea class="input code-input" id="${id}" data-field="${f.name}" spellcheck="false" rows="16">${esc(v)}</textarea><button type="button" class="btn sm" id="code-expand">${icon("external")}Open large editor</button>`;
        else if (f.kind === "select") html += `<select class="input" id="${id}" data-field="${f.name}">${f.options.map(([val, label]) => `<option value="${esc(val)}" ${val === v ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>`;
        else html += `<input class="input" id="${id}" data-field="${f.name}" type="${f.kind === "number" ? "number" : "text"}" value="${esc(v)}" placeholder="${esc(f.placeholder)}">`;
        if (f.help) html += `<span class="hint">${esc(f.help)}</span>`;
        html += `</div>`;
      }
      if (t.type === "ai_filter" && !app.dataset.model) {
        html += `<div class="banner warn small">No model is connected – until you add one on <a href="/settings/models">Model providers</a> this node lets every file through.</div>`;
      }
      html += `</div><div class="insp-foot"><span class="muted small mono">${esc(node.data.nid)}</span><button type="button" class="btn sm danger" id="node-delete">${icon("trash")}Remove</button></div>`;
    }
    inspector.innerHTML = html;
    inspector.querySelectorAll("[data-field]").forEach(input => {
      input.addEventListener("input", () => updateConfig(dfId, input.dataset.field,
        input.type === "number" ? Number(input.value) : input.value));
      if (input.classList.contains("code-input")) enableTab(input);
    });
    inspector.querySelector("#node-delete")?.addEventListener("click", () => { editor.removeNodeId(`node-${dfId}`); });
    inspector.querySelector("#code-expand")?.addEventListener("click", () => openCodeDialog(dfId));
  }

  function updateConfig(dfId, field, value) {
    const node = editor.getNodeFromId(dfId);
    const config = { ...(node.data.config || {}), [field]: value };
    editor.updateNodeDataFromId(dfId, { ...node.data, config });
    const sum = document.querySelector(`#node-${dfId} .fn-sum`);
    if (sum) sum.textContent = summary(node.data.type, config);
    setDirty(true);
  }

  function enableTab(textarea) {
    textarea.addEventListener("keydown", (e) => {
      if (e.key !== "Tab") return;
      e.preventDefault();
      const { selectionStart: s, selectionEnd: end, value } = textarea;
      textarea.value = value.slice(0, s) + "    " + value.slice(end);
      textarea.selectionStart = textarea.selectionEnd = s + 4;
      textarea.dispatchEvent(new Event("input"));
    });
  }

  const codeDialog = document.getElementById("code-dialog");
  const codeBig = document.getElementById("code-big");
  enableTab(codeBig);
  function openCodeDialog(dfId) {
    codeBig.value = editor.getNodeFromId(dfId).data.config.code || "";
    codeBig.oninput = () => {
      updateConfig(dfId, "code", codeBig.value);
      const small = inspector.querySelector('[data-field="code"]');
      if (small) small.value = codeBig.value;
    };
    codeDialog.showModal();
    codeBig.focus();
  }

  // ------------------------------------------------------------------ events
  editor.on("nodeSelected", (id) => showInspector(id));
  editor.on("nodeUnselected", () => showInspector(null));
  editor.on("nodeRemoved", (id) => { if (String(selected) === String(id)) showInspector(null); setDirty(true); });
  editor.on("nodeMoved", () => setDirty(true));
  editor.on("connectionCreated", ({ output_id, input_id, output_class }) => {
    // Only one connection per (output → node) pair, and nothing into the Website node.
    const target = editor.getNodeFromId(input_id);
    if (target.data.type === "source" || output_id === input_id) {
      editor.removeSingleConnection(output_id, input_id, output_class, "input_1");
      return;
    }
    setDirty(true);
  });
  editor.on("connectionRemoved", () => setDirty(true));

  document.getElementById("zoom-in").onclick = () => editor.zoom_in();
  document.getElementById("zoom-out").onclick = () => editor.zoom_out();
  document.getElementById("zoom-reset").onclick = fit;
  document.getElementById("zoom-reset").title = "Fit the flow";

  window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

  // ------------------------------------------------------------------ save / reset
  function notes(messages, level) {
    notesEl.hidden = !messages.length;
    notesEl.className = "flow-notes banner " + (level || "warn");
    notesEl.textContent = messages.join(" · ");
  }

  document.getElementById("btn-save").addEventListener("click", async () => {
    const btn = document.getElementById("btn-save");
    btn.disabled = true;
    try {
      const r = await fetch(`/api/sites/${siteId}/flow`, {
        method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ flow: exportFlow() }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { notes([j.detail || `Saving failed (HTTP ${r.status})`], "bad"); return; }
      setDirty(false);
      notes(j.warnings || []);
      stateEl.textContent = "Saved";
    } finally { btn.disabled = false; }
  });

  document.getElementById("btn-reset").addEventListener("click", () => {
    if (!confirm("Replace the canvas with the default flow (Website → Save to storage)? Save to keep it.")) return;
    load({ nodes: [{ id: "source", type: "source", x: 60, y: 160, config: {} }, { id: "storage", type: "storage", x: 420, y: 160, config: {} }],
           edges: [{ from: "source", out: "files", to: "storage" }] });
    setDirty(true);
  });

  // ------------------------------------------------------------------ test run
  const testForm = document.getElementById("test-form");
  const testLog = document.getElementById("test-log");
  const testOut = document.getElementById("test-outputs");
  testForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("test-run");
    if (!btn) return;
    btn.disabled = true;
    testLog.innerHTML = '<div class="console-empty"><span class="spinner"></span> Running…</div>';
    testOut.innerHTML = "";
    try {
      const r = await fetch(`/api/sites/${siteId}/flow/test`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ file_id: Number(document.getElementById("test-file").value), flow: exportFlow() }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { testLog.innerHTML = `<div class="log-line error"><span>${esc(j.detail || "HTTP " + r.status)}</span></div>`; return; }
      testLog.innerHTML = j.log.map(([t, level, msg]) => `<div class="log-line ${level}"><span class="ts">${new Date(t * 1000).toLocaleTimeString([], { hour12: false })}</span><span>${esc(msg)}</span></div>`).join("")
        || '<div class="console-empty">No log.</div>';
      testOut.innerHTML = j.outputs.map(o => `<details class="test-output" open>
          <summary><b>${esc(o.filename)}</b> <span class="muted small">${o.size.toLocaleString()} bytes · would be saved in “${esc(o.folder)}”</span></summary>
          ${o.preview !== null ? `<pre>${esc(o.preview)}${o.truncated ? "\n…" : ""}</pre>` : '<p class="muted small">Binary file – no preview.</p>'}
        </details>`).join("");
    } finally { btn.disabled = false; }
  });

  load(initial);
})();
