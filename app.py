import io
import os
import re
import sqlite3
import time
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from flask import Flask, g, jsonify, render_template, request

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "greek.db"
DB_PATH.parent.mkdir(exist_ok=True)

PERSEIDS_MORPH = "https://services.perseids.org/bsp/morphologyservice/analysis/word"
PERSEUS_MORPH_FALLBACK = "https://www.perseus.tufts.edu/hopper/xmlmorph"
WIKTIONARY_DEF = "https://en.wiktionary.org/api/rest_v1/page/definition/{lemma}"
LOOKUP_TTL = 60 * 60 * 24  # 24h
LOOKUP_MAX_ENTRIES = 5000
USER_AGENT = "ItsAllGreekToMe/0.1 (greek translation workbench)"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


# ---------- database ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_filename TEXT,
    current_line INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    line_no INTEGER NOT NULL,
    original TEXT NOT NULL,
    translation TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    display_label TEXT NOT NULL DEFAULT '',
    is_header INTEGER NOT NULL DEFAULT 0,
    group_head INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_lines_project ON lines(project_id, line_no);
"""

# Add-column migrations for old DBs.
MIGRATIONS = [
    "ALTER TABLE lines ADD COLUMN display_label TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE lines ADD COLUMN is_header INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE lines ADD COLUMN group_head INTEGER NOT NULL DEFAULT 0",
]


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()


# ---------- text ingestion ----------

def extract_text_from_pdf(data: bytes, ocr: bool = False, ocr_lang: str = "grc") -> str:
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    pages = []
    for page in doc:
        text = page.get_text("text") or ""
        if ocr or not text.strip():
            try:
                import pytesseract
                from PIL import Image
                pix = page.get_pixmap(dpi=300)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                text = pytesseract.image_to_string(img, lang=ocr_lang)
            except Exception as e:
                text = text or f"[OCR unavailable: {e}]"
        pages.append(text)
    return "\n".join(pages)


# Recognize "[Speaker]" markers and "<digits-or-letters>\t<text>" line labels
# emitted by scripts/extract_tei.py.
HEADER_RE = re.compile(r"^\[(.+)\]\s*$")
LABELED_RE = re.compile(r"^([0-9]+[a-zA-Z]?)\t(.+)$")


def parse_lines(text: str) -> list[dict]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = HEADER_RE.match(line)
        if m:
            out.append({"original": m.group(1).strip(), "display_label": "", "is_header": 1})
            continue
        m = LABELED_RE.match(line)
        if m:
            out.append({"original": m.group(2).strip(), "display_label": m.group(1), "is_header": 0})
            continue
        out.append({"original": line, "display_label": "", "is_header": 0})
    return out


# ---------- routes: pages ----------

@app.route("/")
def index():
    return render_template("index.html")


# ---------- routes: projects ----------

@app.get("/api/projects")
def list_projects():
    rows = db().execute(
        "SELECT id, name, source_filename, current_line, updated_at FROM projects ORDER BY updated_at DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/projects")
def create_project():
    name = (request.form.get("name") or "").strip() or "Untitled"
    file = request.files.get("file")
    pasted = request.form.get("pasted_text") or ""
    ocr = request.form.get("ocr") == "1"

    if file and file.filename:
        data = file.read()
        fname = file.filename
        if fname.lower().endswith(".pdf"):
            text = extract_text_from_pdf(data, ocr=ocr)
        else:
            text = data.decode("utf-8", errors="replace")
    elif pasted.strip():
        text = pasted
        fname = None
    else:
        return jsonify({"error": "Provide a file or pasted text"}), 400

    parsed = parse_lines(text)
    if not parsed:
        return jsonify({"error": "No text extracted (try OCR for scanned PDFs)"}), 400

    conn = db()
    cur = conn.execute(
        "INSERT INTO projects(name, source_filename) VALUES (?, ?)", (name, fname)
    )
    pid = cur.lastrowid
    conn.executemany(
        "INSERT INTO lines(project_id, line_no, original, display_label, is_header) VALUES (?, ?, ?, ?, ?)",
        [(pid, i + 1, p["original"], p["display_label"], p["is_header"]) for i, p in enumerate(parsed)],
    )
    conn.commit()
    return jsonify({"id": pid, "lines": len(parsed)})


def _line_to_dict(row):
    d = dict(row)
    if not d.get("group_head"):
        d["group_head"] = d["line_no"]
    return d


@app.get("/api/projects/<int:pid>")
def get_project(pid):
    conn = db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        return jsonify({"error": "not found"}), 404
    rows = conn.execute(
        "SELECT line_no, original, translation, notes, display_label, is_header, group_head "
        "FROM lines WHERE project_id=? ORDER BY line_no",
        (pid,),
    ).fetchall()
    return jsonify({"project": dict(proj), "lines": [_line_to_dict(r) for r in rows]})


@app.delete("/api/projects/<int:pid>")
def delete_project(pid):
    conn = db()
    conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    conn.commit()
    return jsonify({"ok": True})


@app.put("/api/projects/<int:pid>/cursor")
def set_cursor(pid):
    line_no = int(request.json.get("line_no", 0))
    conn = db()
    conn.execute(
        "UPDATE projects SET current_line=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (line_no, pid),
    )
    conn.commit()
    return jsonify({"ok": True})


@app.put("/api/projects/<int:pid>/lines/<int:line_no>")
def update_line(pid, line_no):
    payload = request.json or {}
    fields, values = [], []
    for col in ("translation", "notes"):
        if col in payload:
            fields.append(f"{col}=?")
            values.append(payload[col])
    if not fields:
        return jsonify({"ok": True})
    values.extend([pid, line_no])
    conn = db()
    conn.execute(
        f"UPDATE lines SET {', '.join(fields)} WHERE project_id=? AND line_no=?", values
    )
    conn.execute("UPDATE projects SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (pid,))
    conn.commit()
    return jsonify({"ok": True})


def _prev_translatable_line(conn, pid, line_no):
    """Return the highest line_no < given that is not a header."""
    row = conn.execute(
        "SELECT line_no FROM lines WHERE project_id=? AND line_no<? AND is_header=0 "
        "ORDER BY line_no DESC LIMIT 1",
        (pid, line_no),
    ).fetchone()
    return row["line_no"] if row else None


@app.post("/api/projects/<int:pid>/lines/<int:line_no>/merge_up")
def merge_up(pid, line_no):
    conn = db()
    cur = conn.execute(
        "SELECT line_no, is_header, group_head, translation, notes FROM lines "
        "WHERE project_id=? AND line_no=?",
        (pid, line_no),
    ).fetchone()
    if not cur or cur["is_header"]:
        return jsonify({"error": "cannot merge"}), 400
    prev_no = _prev_translatable_line(conn, pid, line_no)
    if prev_no is None:
        return jsonify({"error": "no previous line"}), 400
    prev = conn.execute(
        "SELECT group_head FROM lines WHERE project_id=? AND line_no=?", (pid, prev_no)
    ).fetchone()
    head = prev["group_head"] or prev_no
    # Move any text from this line into the head's translation/notes.
    if cur["translation"] or cur["notes"]:
        head_row = conn.execute(
            "SELECT translation, notes FROM lines WHERE project_id=? AND line_no=?",
            (pid, head),
        ).fetchone()
        new_t = (head_row["translation"] + ("\n" if head_row["translation"] and cur["translation"] else "") + cur["translation"]).strip("\n")
        new_n = (head_row["notes"] + ("\n" if head_row["notes"] and cur["notes"] else "") + cur["notes"]).strip("\n")
        conn.execute(
            "UPDATE lines SET translation=?, notes=? WHERE project_id=? AND line_no=?",
            (new_t, new_n, pid, head),
        )
    conn.execute(
        "UPDATE lines SET group_head=?, translation='', notes='' WHERE project_id=? AND line_no=?",
        (head, pid, line_no),
    )
    conn.commit()
    return jsonify({"ok": True, "group_head": head})


@app.post("/api/projects/<int:pid>/lines/<int:line_no>/split")
def split_line(pid, line_no):
    conn = db()
    conn.execute(
        "UPDATE lines SET group_head=0 WHERE project_id=? AND line_no=?", (pid, line_no)
    )
    conn.commit()
    return jsonify({"ok": True})


@app.get("/api/projects/<int:pid>/export")
def export_project(pid):
    conn = db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        return jsonify({"error": "not found"}), 404
    rows = conn.execute(
        "SELECT line_no, original, translation, notes, display_label, is_header, group_head "
        "FROM lines WHERE project_id=? ORDER BY line_no",
        (pid,),
    ).fetchall()
    return jsonify({"project": dict(proj), "lines": [_line_to_dict(r) for r in rows]})


# ---------- dictionary ----------

_lookup_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _cache_get(word: str):
    with _cache_lock:
        entry = _lookup_cache.get(word)
        if not entry:
            return None
        ts, data = entry
        if time.monotonic() - ts > LOOKUP_TTL:
            _lookup_cache.pop(word, None)
            return None
        return data


def _cache_put(word: str, data: dict):
    with _cache_lock:
        if len(_lookup_cache) >= LOOKUP_MAX_ENTRIES:
            # Evict the oldest 10% to keep this O(N) only occasionally.
            victims = sorted(_lookup_cache.items(), key=lambda kv: kv[1][0])[: LOOKUP_MAX_ENTRIES // 10]
            for k, _ in victims:
                _lookup_cache.pop(k, None)
        _lookup_cache[word] = (time.monotonic(), data)


# Strip Greek elision marks (U+02BC modifier letter apostrophe, U+2019 right
# single quote, U+1FBD koronis) and surrounding punctuation. \w matches U+02BC
# (it's category Lm), so we list it explicitly.
_ELISION = "ʼ’᾽'"
_PUNCT_BOUNDARY = re.compile(rf"^[^\wͰ-Ͽἀ-῿]+|[{_ELISION}]+$|[^\wͰ-Ͽἀ-῿]+$")


def strip_punct(word: str) -> str:
    prev = None
    while word != prev:
        prev = word
        word = _PUNCT_BOUNDARY.sub("", word)
    return word


def _unwrap(node):
    """Perseids returns either {'$': value} or sometimes a plain string."""
    if isinstance(node, dict):
        return node.get("$", "")
    return node or ""


def fetch_perseids(word: str) -> list[dict]:
    """Query Perseids morphology service. Returns list of analysis dicts."""
    r = requests.get(
        PERSEIDS_MORPH,
        params={"lang": "grc", "engine": "morpheusgrc", "word": word},
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    body = data.get("RDF", {}).get("Annotation", {}).get("Body", [])
    if isinstance(body, dict):
        body = [body]
    out = []
    for b in body:
        entry = b.get("rest", {}).get("entry", {})
        d = entry.get("dict", {})
        lemma = _unwrap(d.get("hdwd"))
        pos = _unwrap(d.get("pofs"))
        infls = entry.get("infl", [])
        if isinstance(infls, dict):
            infls = [infls]
        if not infls:
            out.append({"lemma": lemma, "pos": pos, "form": word})
            continue
        for infl in infls:
            a = {"lemma": lemma, "pos": pos or _unwrap(infl.get("pofs")), "form": word}
            for k_in, k_out in (
                ("case", "case"), ("gend", "gender"), ("num", "number"),
                ("tense", "tense"), ("mood", "mood"), ("voice", "voice"),
                ("pers", "person"), ("decl", "decl"), ("conj", "conj"),
                ("dial", "dialect"),
            ):
                v = _unwrap(infl.get(k_in))
                if v:
                    a[k_out] = v
            out.append(a)
    return out


def fetch_perseus_xmlmorph(word: str) -> list[dict]:
    """Fallback: Perseus' classic xmlmorph endpoint."""
    r = requests.get(
        PERSEUS_MORPH_FALLBACK,
        params={"lang": "greek", "lookup": word},
        headers={"User-Agent": USER_AGENT},
        timeout=10,
    )
    r.raise_for_status()
    out = []
    try:
        root = ET.fromstring(r.text)
        for analysis in root.findall(".//analysis"):
            entry = {child.tag: (child.text or "").strip() for child in analysis}
            out.append(entry)
    except ET.ParseError:
        pass
    return out


def fetch_definitions(lemma: str) -> list[str]:
    """Wiktionary REST: short English glosses for an Ancient Greek lemma."""
    if not lemma:
        return []
    try:
        r = requests.get(
            WIKTIONARY_DEF.format(lemma=lemma),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=10,
        )
        if not r.ok:
            return []
        data = r.json()
    except (requests.RequestException, ValueError):
        return []

    # Wiktionary keys language sections by language code or full name.
    sections = data.get("grc") or data.get("Ancient Greek") or []
    out = []
    for sense in sections:
        for d in sense.get("definitions", []):
            txt = re.sub(r"<[^>]+>", "", d.get("definition", "")).strip()
            if txt:
                out.append(txt)
            if len(out) >= 4:
                return out
    return out


def fetch_lookup(word: str) -> dict:
    errors = []
    analyses = []
    try:
        analyses = fetch_perseids(word)
    except (requests.RequestException, ValueError) as e:
        errors.append(f"Perseids: {e}")

    if not analyses:
        try:
            analyses = fetch_perseus_xmlmorph(word)
        except requests.RequestException as e:
            errors.append(f"Perseus fallback: {e}")

    lemmas = sorted({a.get("lemma", "") for a in analyses if a.get("lemma")})
    definitions = {l: fetch_definitions(l) for l in lemmas}

    return {
        "word": word,
        "analyses": analyses,
        "lemmas": lemmas,
        "definitions": definitions,
        "errors": errors,
        "links": _links(word),
        "lemma_links": [
            {
                "lemma": l,
                "logeion": f"https://logeion.uchicago.edu/{l}",
                "perseus": f"https://www.perseus.tufts.edu/hopper/text?doc=Perseus%3Atext%3A1999.04.0057%3Aentry%3D{l}",
                "wiktionary": f"https://en.wiktionary.org/wiki/{l}#Ancient_Greek",
            }
            for l in lemmas
        ],
    }


def _links(word: str) -> dict:
    return {
        "logeion": f"https://logeion.uchicago.edu/{word}",
        "perseus": f"https://www.perseus.tufts.edu/hopper/morph?l={word}&la=greek",
        "wiktionary": f"https://en.wiktionary.org/wiki/{word}#Ancient_Greek",
    }


@app.get("/api/lookup")
def lookup():
    raw = (request.args.get("word") or "").strip()
    if not raw:
        return jsonify({"error": "empty"}), 400
    word = strip_punct(raw)
    if not word:
        return jsonify({"error": "empty after stripping punctuation"}), 400
    cached = _cache_get(word)
    if cached is not None:
        return jsonify({**cached, "cached": True})
    data = fetch_lookup(word)
    if data["analyses"]:
        _cache_put(word, data)
    return jsonify({**data, "cached": False})


@app.post("/api/lookup/cache/clear")
def clear_cache():
    with _cache_lock:
        n = len(_lookup_cache)
        _lookup_cache.clear()
    return jsonify({"cleared": n})


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
