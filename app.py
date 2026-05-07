import io
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from flask import Flask, g, jsonify, render_template, request

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "greek.db"
DB_PATH.parent.mkdir(exist_ok=True)

PERSEUS_MORPH = "https://www.perseus.tufts.edu/hopper/xmlmorph"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload cap


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
    UNIQUE(project_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_lines_project ON lines(project_id, line_no);
"""


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
    conn.commit()
    conn.close()


# ---------- text ingestion ----------

def extract_text_from_pdf(data: bytes, ocr: bool = False, ocr_lang: str = "grc") -> str:
    import fitz  # PyMuPDF

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


def split_into_lines(text: str) -> list[str]:
    # Preserve non-empty lines, strip surrounding whitespace.
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


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

    lines = split_into_lines(text)
    if not lines:
        return jsonify({"error": "No text extracted (try OCR for scanned PDFs)"}), 400

    conn = db()
    cur = conn.execute(
        "INSERT INTO projects(name, source_filename) VALUES (?, ?)", (name, fname)
    )
    pid = cur.lastrowid
    conn.executemany(
        "INSERT INTO lines(project_id, line_no, original) VALUES (?, ?, ?)",
        [(pid, i + 1, ln) for i, ln in enumerate(lines)],
    )
    conn.commit()
    return jsonify({"id": pid, "lines": len(lines)})


@app.get("/api/projects/<int:pid>")
def get_project(pid):
    conn = db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        return jsonify({"error": "not found"}), 404
    rows = conn.execute(
        "SELECT line_no, original, translation, notes FROM lines WHERE project_id=? ORDER BY line_no",
        (pid,),
    ).fetchall()
    return jsonify({"project": dict(proj), "lines": [dict(r) for r in rows]})


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
    fields = []
    values = []
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
    conn.execute(
        "UPDATE projects SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (pid,)
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
        "SELECT line_no, original, translation, notes FROM lines WHERE project_id=? ORDER BY line_no",
        (pid,),
    ).fetchall()
    return jsonify({"project": dict(proj), "lines": [dict(r) for r in rows]})


# ---------- routes: dictionary ----------

# Strip combining diacritics for normalization fallbacks.
COMBINING = re.compile(r"[̀-ͯ҃-҉᷀-᷿]")


def strip_punct(word: str) -> str:
    # Remove leading/trailing punctuation but keep Greek letters and combining marks.
    return re.sub(r"^[^\wͰ-Ͽἀ-῿]+|[^\wͰ-Ͽἀ-῿]+$", "", word)


@app.get("/api/lookup")
def lookup():
    raw = (request.args.get("word") or "").strip()
    if not raw:
        return jsonify({"error": "empty"}), 400
    word = strip_punct(raw)
    try:
        r = requests.get(
            PERSEUS_MORPH,
            params={"lang": "greek", "lookup": word},
            timeout=10,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"word": word, "error": f"Perseus unreachable: {e}", "analyses": []})

    analyses = []
    try:
        root = ET.fromstring(r.text)
        for analysis in root.findall(".//analysis"):
            entry = {child.tag: (child.text or "").strip() for child in analysis}
            analyses.append(entry)
    except ET.ParseError:
        pass

    lemmas = sorted({a.get("lemma", "") for a in analyses if a.get("lemma")})
    return jsonify({
        "word": word,
        "analyses": analyses,
        "lemmas": lemmas,
        "links": {
            "logeion": f"https://logeion.uchicago.edu/{word}",
            "perseus": f"https://www.perseus.tufts.edu/hopper/morph?l={word}&la=greek",
        },
        "lemma_links": [
            {
                "lemma": l,
                "logeion": f"https://logeion.uchicago.edu/{l}",
                "perseus": f"https://www.perseus.tufts.edu/hopper/text?doc=Perseus%3Atext%3A1999.04.0057%3Aentry%3D{l}",
            }
            for l in lemmas
        ],
    })


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
