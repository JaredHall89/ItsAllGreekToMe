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
  currentLineNo = currentProject.project.current_line || firstTranslatableLine() || 1;
  renderAll();
  scrollToLine(currentLineNo);
}

function firstTranslatableLine() {
  return currentProject?.lines.find(l => !l.is_header)?.line_no;
}

function lineByNo(n) { return currentProject?.lines.find(l => l.line_no === n); }

function scrollToLine(n) {
  document.querySelector(`.line[data-line="${n}"]`)?.scrollIntoView({ block: "center" });
  document.querySelector(`.tline[data-line="${n}"]`)?.scrollIntoView({ block: "center" });
}

function renderAll() {
  renderOriginal();
  renderTranslation();
}

function isGroupHead(ln) {
  if (ln.is_header) return false;
  return (ln.group_head || ln.line_no) === ln.line_no;
}

function groupMembers(headNo) {
  // Lines in group: head itself + any subsequent line (until next non-header head)
  // whose group_head === headNo.
  const lines = currentProject.lines;
  const headIdx = lines.findIndex(l => l.line_no === headNo);
  if (headIdx < 0) return [];
  const out = [lines[headIdx]];
  for (let i = headIdx + 1; i < lines.length; i++) {
    const l = lines[i];
    if (l.is_header) continue;
    if ((l.group_head || l.line_no) === headNo) out.push(l);
    else break;
  }
  return out;
}

function renderOriginal() {
  const root = $("#original-lines");
  root.innerHTML = "";
  if (!currentProject) return;
  for (const ln of currentProject.lines) {
    const div = document.createElement("div");
    div.dataset.line = ln.line_no;
    if (ln.is_header) {
      div.className = "line header";
      const num = document.createElement("span");
      num.className = "lineno";
      const text = document.createElement("span");
      text.className = "text";
      text.textContent = ln.original;
      div.appendChild(num);
      div.appendChild(text);
      root.appendChild(div);
      continue;
    }
    const grouped = !isGroupHead(ln);
    div.className = "line" + (ln.line_no === currentLineNo ? " current" : "") + (grouped ? " grouped" : "");
    const num = document.createElement("span");
    num.className = "lineno";
    num.textContent = ln.display_label || ln.line_no;
    const text = document.createElement("span");
    text.className = "text";
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
    if (ln.is_header) {
      const h = document.createElement("div");
      h.className = "tline spacer";
      const div = document.createElement("div");
      div.className = "header-divider";
      div.textContent = ln.original;
      h.appendChild(div);
      root.appendChild(h);
      continue;
    }
    if (!isGroupHead(ln)) continue;  // non-head members are folded into the head

    const members = groupMembers(ln.line_no);
    const div = document.createElement("div");
    div.className = "tline" + (members.some(m => m.line_no === currentLineNo) ? " current" : "");
    div.dataset.line = ln.line_no;

    const num = document.createElement("span");
    num.className = "lineno";
    if (members.length === 1) {
      num.textContent = ln.display_label || ln.line_no;
    } else {
      const first = members[0].display_label || members[0].line_no;
      const last = members[members.length - 1].display_label || members[members.length - 1].line_no;
      num.textContent = `${first}–${last}`;
    }

    const editor = document.createElement("div");
    editor.className = "editor";

    if (members.length > 1) {
      const src = document.createElement("div");
      src.className = "grouped-source";
      src.textContent = members.map(m => m.original).join("  /  ");
      editor.appendChild(src);
    }

    const ta = document.createElement("textarea");
    ta.value = ln.translation || "";
    ta.placeholder = "translation…";
    ta.rows = Math.max(1, members.length);
    ta.addEventListener("input", () => scheduleSave(ln.line_no, "translation", ta.value));
    ta.addEventListener("focus", () => setCurrentLine(ln.line_no));

    const notes = document.createElement("textarea");
    notes.className = "notes";
    notes.value = ln.notes || "";
    notes.placeholder = "notes…";
    notes.rows = 1;
    notes.addEventListener("input", () => scheduleSave(ln.line_no, "notes", notes.value));
    notes.addEventListener("focus", () => setCurrentLine(ln.line_no));

    const actions = document.createElement("div");
    actions.className = "row-actions";
    const mergeBtn = document.createElement("button");
    mergeBtn.type = "button";
    mergeBtn.textContent = "↑ merge with previous";
    mergeBtn.title = "Merge this group into the previous translation group";
    mergeBtn.addEventListener("click", () => mergeUp(ln.line_no));
    actions.appendChild(mergeBtn);

    if (members.length > 1) {
      const splitBtn = document.createElement("button");
      splitBtn.type = "button";
      splitBtn.textContent = "split group";
      splitBtn.title = "Restore each member as its own line";
      splitBtn.addEventListener("click", () => splitGroup(ln.line_no));
      actions.appendChild(splitBtn);
    }

    editor.appendChild(ta);
    editor.appendChild(notes);
    editor.appendChild(actions);
    div.appendChild(num);
    div.appendChild(editor);
    root.appendChild(div);
  }
}

async function mergeUp(line_no) {
  try {
    await api(`/api/projects/${currentProject.project.id}/lines/${line_no}/merge_up`, { method: "POST" });
    await reloadProject();
    setStatus("Merged");
  } catch (e) { setStatus("Merge failed: " + e.message, 4000); }
}

async function splitGroup(headNo) {
  // Split: reset every member except the head back to self.
  const members = groupMembers(headNo);
  for (const m of members.slice(1)) {
    await api(`/api/projects/${currentProject.project.id}/lines/${m.line_no}/split`, { method: "POST" });
  }
  await reloadProject();
  setStatus("Split");
}

async function reloadProject() {
  const id = currentProject.project.id;
  currentProject = await api(`/api/projects/${id}`);
  renderAll();
}

function setCurrentLine(line_no) {
  if (line_no === currentLineNo) return;
  currentLineNo = line_no;
  for (const el of document.querySelectorAll(".line.current, .tline.current")) {
    el.classList.remove("current");
  }
  const ln = lineByNo(line_no);
  document.querySelector(`.line[data-line="${line_no}"]`)?.classList.add("current");
  if (ln) {
    const head = ln.is_header ? line_no : (ln.group_head || ln.line_no);
    document.querySelector(`.tline[data-line="${head}"]`)?.classList.add("current");
  }
  if (!currentProject) return;
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
      const ln = lineByNo(line_no);
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
    <b>${escapeHtml(data.word)}</b>${data.cached ? '<span class="badge">cached</span>' : ''} &mdash;
    <a href="${data.links.logeion}" target="_blank">Logeion</a>
    <a href="${data.links.wiktionary}" target="_blank">Wiktionary</a>
    <a href="${data.links.perseus}" target="_blank">Perseus morph</a>
  </div>`;
  if (data.errors?.length) {
    html += `<div class="error">${data.errors.map(escapeHtml).join("<br>")}</div>`;
  }
  if (!data.lsj_available) {
    html += `<div class="lsj-hint">Tip: run <code>python scripts/build_lsj.py</code> once to bundle the full Liddell-Scott-Jones lexicon for offline definitions.</div>`;
  }
  if (!data.analyses?.length) {
    html += `<div class="error">No morphological analyses returned. Try Logeion or Wiktionary (links above) for the headword. If many lookups are failing, the upstream service may be down.</div>`;
  } else {
    const byLemma = {};
    for (const a of data.analyses) {
      const k = a.lemma || "?";
      (byLemma[k] = byLemma[k] || []).push(a);
    }
    for (const [lemma, list] of Object.entries(byLemma)) {
      const lemmaLink = data.lemma_links.find(l => l.lemma === lemma);
      const defs = data.definitions?.[lemma] || [];
      const lsj = data.lsj?.[lemma] || "";
      html += `<div class="analysis">
        <div class="lemma">${escapeHtml(lemma)}
          ${lemmaLink ? `<a href="${lemmaLink.wiktionary}" target="_blank" class="lemma-link">Wikt ↗</a>
          <a href="${lemmaLink.logeion}" target="_blank" class="lemma-link">Logeion ↗</a>
          <a href="${lemmaLink.perseus}" target="_blank" class="lemma-link">LSJ ↗</a>` : ""}
        </div>`;
      if (lsj) {
        html += `<div class="lsj-def"><span class="src-tag">LSJ</span> ${escapeHtml(lsj)}</div>`;
      }
      if (defs.length) {
        html += `<div class="wikt-defs"><span class="src-tag">Wiktionary</span><ol class="defs">${defs.map(d => `<li>${escapeHtml(d)}</li>`).join("")}</ol></div>`;
      }
      if (!lsj && !defs.length) {
        html += `<div class="no-def">No short definition available — see external links above.</div>`;
      }
      for (const a of list) {
        const parts = ["pos","person","number","tense","mood","voice","gender","case","degree","dialect","feature","decl","conj"]
          .map(k => a[k]).filter(Boolean).join(" · ");
        if (parts) html += `<div class="grammar">${escapeHtml(a.form || "")} — ${escapeHtml(parts)}</div>`;
      }
      html += `</div>`;
    }
  }
  out.innerHTML = html;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// ---------- polytonic keyboard ----------

const KB_LETTERS = [
  "α β γ δ ε ζ η θ ι κ λ μ ν ξ ο π ρ σ ς τ υ φ χ ψ ω".split(" "),
  // common precomposed vowels with breathings/accents — covers ~90% of Attic forms
  "ἀ ἁ ά ὰ ᾶ ἂ ἃ ἄ ἅ ἆ ἇ ᾳ".split(" "),
  "ἐ ἑ έ ὲ ἒ ἓ ἔ ἕ".split(" "),
  "ἠ ἡ ή ὴ ῆ ἢ ἣ ἤ ἥ ἦ ἧ ῃ".split(" "),
  "ἰ ἱ ί ὶ ῖ ἲ ἳ ἴ ἵ ἶ ἷ ϊ".split(" "),
  "ὀ ὁ ό ὸ ὂ ὃ ὄ ὅ".split(" "),
  "ὐ ὑ ύ ὺ ῦ ὒ ὓ ὔ ὕ ὖ ὗ ϋ".split(" "),
  "ὠ ὡ ώ ὼ ῶ ὢ ὣ ὤ ὥ ὦ ὧ ῳ".split(" "),
];

function buildKeyboard() {
  const kb = $("#polytonic-kb");
  const input = $("#lookup-input");

  function insert(text) {
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? input.value.length;
    input.value = input.value.slice(0, start) + text + input.value.slice(end);
    const pos = start + text.length;
    input.setSelectionRange(pos, pos);
    input.focus();
  }

  for (const row of KB_LETTERS) {
    const r = document.createElement("div");
    r.className = "kb-row";
    for (const ch of row) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = ch;
      b.addEventListener("click", () => insert(ch));
      r.appendChild(b);
    }
    kb.appendChild(r);
  }

  const r = document.createElement("div");
  r.className = "kb-row";
  for (const [label, action] of [
    ["space", () => insert(" ")],
    ["⌫", () => {
      const s = input.selectionStart ?? input.value.length;
      const e = input.selectionEnd ?? input.value.length;
      if (s !== e) { insert(""); return; }
      if (s === 0) return;
      input.value = input.value.slice(0, s - 1) + input.value.slice(e);
      input.setSelectionRange(s - 1, s - 1);
      input.focus();
    }],
    ["clear", () => { input.value = ""; input.focus(); }],
    ["look up", () => $("#lookup-form").requestSubmit()],
  ]) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "kb-action"; b.textContent = label;
    b.addEventListener("click", action);
    r.appendChild(b);
  }
  kb.appendChild(r);
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

document.addEventListener("keydown", (e) => {
  if (!currentProject) return;
  if (["TEXTAREA", "INPUT"].includes(document.activeElement.tagName)) return;
  if (e.key === "ArrowDown" || e.key === "j") {
    const lines = currentProject.lines;
    const idx = lines.findIndex(l => l.line_no === currentLineNo);
    for (let i = idx + 1; i < lines.length; i++) {
      if (!lines[i].is_header) {
        setCurrentLine(lines[i].line_no);
        scrollToLine(lines[i].line_no);
        break;
      }
    }
  } else if (e.key === "ArrowUp" || e.key === "k") {
    const lines = currentProject.lines;
    const idx = lines.findIndex(l => l.line_no === currentLineNo);
    for (let i = idx - 1; i >= 0; i--) {
      if (!lines[i].is_header) {
        setCurrentLine(lines[i].line_no);
        scrollToLine(lines[i].line_no);
        break;
      }
    }
  }
});

buildKeyboard();
loadProjectList();
