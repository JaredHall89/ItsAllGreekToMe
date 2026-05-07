const $ = (sel) => document.querySelector(sel);
const statusEl = $("#status");

let currentProject = null;       // {project, lines}
let currentLineNo = 0;
const saveTimers = new Map();

function setStatus(msg, ttl = 2000) {
  statusEl.textContent = msg;
  if (ttl) setTimeout(() => { if (statusEl.textContent === msg) statusEl.textContent = ""; }, ttl);
}

async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ error: r.statusText }));
    throw new Error(err.error || r.statusText);
  }
  return r.json();
}

// ---------- projects ----------

async function loadProjectList() {
  const projects = await api("/api/projects");
  const sel = $("#project-select");
  const cur = sel.value;
  sel.innerHTML = '<option value="">— select project —</option>';
  for (const p of projects) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = `${p.name} (line ${p.current_line || 0})`;
    sel.appendChild(opt);
  }
  if (cur) sel.value = cur;
}

async function loadProject(id) {
  if (!id) { currentProject = null; renderAll(); return; }
  currentProject = await api(`/api/projects/${id}`);
  currentLineNo = currentProject.project.current_line || 1;
  renderAll();
  const el = document.querySelector(`.line[data-line="${currentLineNo}"]`);
  if (el) el.scrollIntoView({ block: "center" });
  const tel = document.querySelector(`.tline[data-line="${currentLineNo}"]`);
  if (tel) tel.scrollIntoView({ block: "center" });
}

function renderAll() {
  renderOriginal();
  renderTranslation();
}

function renderOriginal() {
  const root = $("#original-lines");
  root.innerHTML = "";
  if (!currentProject) return;
  for (const ln of currentProject.lines) {
    const div = document.createElement("div");
    div.className = "line" + (ln.line_no === currentLineNo ? " current" : "");
    div.dataset.line = ln.line_no;
    const num = document.createElement("span");
    num.className = "lineno"; num.textContent = ln.line_no;
    const text = document.createElement("span");
    text.className = "text";
    // Split on whitespace, wrap each word so we can click it.
    for (const tok of ln.original.split(/(\s+)/)) {
      if (/^\s+$/.test(tok)) {
        text.appendChild(document.createTextNode(tok));
      } else {
        const w = document.createElement("span");
        w.className = "word";
        w.textContent = tok;
        w.addEventListener("click", (e) => {
          e.stopPropagation();
          setCurrentLine(ln.line_no);
          lookupWord(tok);
        });
        text.appendChild(w);
      }
    }
    div.addEventListener("click", () => setCurrentLine(ln.line_no));
    div.appendChild(num);
    div.appendChild(text);
    root.appendChild(div);
  }
}

function renderTranslation() {
  const root = $("#translation-lines");
  root.innerHTML = "";
  if (!currentProject) return;
  for (const ln of currentProject.lines) {
    const div = document.createElement("div");
    div.className = "tline" + (ln.line_no === currentLineNo ? " current" : "");
    div.dataset.line = ln.line_no;
    const num = document.createElement("span");
    num.className = "lineno"; num.textContent = ln.line_no;

    const editor = document.createElement("div");
    editor.className = "editor";
    const ta = document.createElement("textarea");
    ta.value = ln.translation || "";
    ta.placeholder = "translation…";
    ta.rows = 1;
    ta.addEventListener("input", () => scheduleSave(ln.line_no, "translation", ta.value));
    ta.addEventListener("focus", () => setCurrentLine(ln.line_no));

    const notes = document.createElement("textarea");
    notes.className = "notes";
    notes.value = ln.notes || "";
    notes.placeholder = "notes…";
    notes.rows = 1;
    notes.addEventListener("input", () => scheduleSave(ln.line_no, "notes", notes.value));
    notes.addEventListener("focus", () => setCurrentLine(ln.line_no));

    editor.appendChild(ta);
    editor.appendChild(notes);
    div.appendChild(num);
    div.appendChild(editor);
    root.appendChild(div);
  }
}

function setCurrentLine(line_no) {
  if (line_no === currentLineNo) return;
  currentLineNo = line_no;
  for (const el of document.querySelectorAll(".line.current, .tline.current")) {
    el.classList.remove("current");
  }
  for (const el of document.querySelectorAll(`[data-line="${line_no}"]`)) {
    el.classList.add("current");
  }
  if (!currentProject) return;
  // Persist cursor (debounced).
  clearTimeout(setCurrentLine._t);
  setCurrentLine._t = setTimeout(() => {
    api(`/api/projects/${currentProject.project.id}/cursor`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ line_no }),
    }).catch(() => {});
  }, 500);
}

function scheduleSave(line_no, field, value) {
  const key = `${line_no}:${field}`;
  clearTimeout(saveTimers.get(key));
  saveTimers.set(key, setTimeout(async () => {
    try {
      await api(`/api/projects/${currentProject.project.id}/lines/${line_no}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: value }),
      });
      // update in-memory
      const ln = currentProject.lines.find(l => l.line_no === line_no);
      if (ln) ln[field] = value;
      setStatus("Saved");
    } catch (e) { setStatus("Save failed: " + e.message, 4000); }
  }, 600));
}

// ---------- dictionary ----------

async function lookupWord(word) {
  $("#lookup-input").value = word;
  const out = $("#lookup-results");
  out.innerHTML = `<div class="lookup-header">Looking up <b>${escapeHtml(word)}</b>…</div>`;
  try {
    const data = await api(`/api/lookup?word=${encodeURIComponent(word)}`);
    renderLookup(data);
  } catch (e) {
    out.innerHTML = `<div class="error">Lookup failed: ${escapeHtml(e.message)}</div>`;
  }
}

function renderLookup(data) {
  const out = $("#lookup-results");
  let html = `<div class="lookup-header">
    <b>${escapeHtml(data.word)}</b> &mdash;
    <a href="${data.links.logeion}" target="_blank">Logeion</a>
    <a href="${data.links.perseus}" target="_blank">Perseus morph</a>
  </div>`;
  if (data.error) html += `<div class="error">${escapeHtml(data.error)}</div>`;
  if (!data.analyses?.length) {
    html += `<div class="error">No morphological analyses returned. Try Logeion (link above) for the headword.</div>`;
  } else {
    // group by lemma
    const byLemma = {};
    for (const a of data.analyses) {
      const k = a.lemma || "?";
      (byLemma[k] = byLemma[k] || []).push(a);
    }
    for (const [lemma, list] of Object.entries(byLemma)) {
      const lemmaLink = data.lemma_links.find(l => l.lemma === lemma);
      html += `<div class="analysis">
        <div class="lemma">${escapeHtml(lemma)}
          ${lemmaLink ? `<a href="${lemmaLink.logeion}" target="_blank" style="font-size:13px;font-weight:normal;">Logeion ↗</a>
          <a href="${lemmaLink.perseus}" target="_blank" style="font-size:13px;font-weight:normal;">LSJ ↗</a>` : ""}
        </div>`;
      for (const a of list) {
        const parts = ["pos","person","number","tense","mood","voice","gender","case","degree","dialect","feature"]
          .map(k => a[k]).filter(Boolean).join(" · ");
        html += `<div class="grammar">${escapeHtml(a.form || "")} — ${escapeHtml(parts)}</div>`;
      }
      html += `</div>`;
    }
  }
  out.innerHTML = html;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// ---------- new project dialog ----------

$("#new-project-btn").addEventListener("click", () => $("#new-project-dialog").showModal());
$("#new-project-form").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const fd = new FormData(e.target);
  setStatus("Importing…", 0);
  try {
    const r = await fetch("/api/projects", { method: "POST", body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    setStatus(`Created project (${data.lines} lines)`);
    $("#new-project-dialog").close();
    e.target.reset();
    await loadProjectList();
    $("#project-select").value = data.id;
    await loadProject(data.id);
  } catch (err) {
    setStatus("Failed: " + err.message, 6000);
  }
});

$("#project-select").addEventListener("change", (e) => loadProject(e.target.value));

$("#delete-project-btn").addEventListener("click", async () => {
  if (!currentProject) return;
  if (!confirm(`Delete project "${currentProject.project.name}"? This cannot be undone.`)) return;
  await api(`/api/projects/${currentProject.project.id}`, { method: "DELETE" });
  currentProject = null;
  await loadProjectList();
  renderAll();
});

$("#export-btn").addEventListener("click", async () => {
  if (!currentProject) return;
  const data = await api(`/api/projects/${currentProject.project.id}/export`);
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${currentProject.project.name.replace(/\W+/g, "_")}.json`;
  a.click();
});

$("#lookup-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const w = $("#lookup-input").value.trim();
  if (w) lookupWord(w);
});

// keyboard: arrow keys move current line when focus is outside textarea
document.addEventListener("keydown", (e) => {
  if (!currentProject) return;
  if (["TEXTAREA", "INPUT"].includes(document.activeElement.tagName)) return;
  if (e.key === "ArrowDown" || e.key === "j") {
    const next = currentLineNo + 1;
    if (currentProject.lines.find(l => l.line_no === next)) {
      setCurrentLine(next);
      document.querySelector(`.line[data-line="${next}"]`)?.scrollIntoView({ block: "nearest" });
    }
  } else if (e.key === "ArrowUp" || e.key === "k") {
    const prev = currentLineNo - 1;
    if (prev >= 1) {
      setCurrentLine(prev);
      document.querySelector(`.line[data-line="${prev}"]`)?.scrollIntoView({ block: "nearest" });
    }
  }
});

loadProjectList();
