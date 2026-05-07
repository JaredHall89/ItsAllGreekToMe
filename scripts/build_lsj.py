"""Download the Perseus LSJ TEI XML and build a local SQLite index of
lemma -> short definition. Run once; the index lives at data/lsj.sqlite
and the app reads from it when present.

Usage:
    python scripts/build_lsj.py

This downloads ~150 MB of XML in 27 files (one per Greek letter) from
PerseusDL/lexica on GitHub, streams each through an iterparse extractor,
and discards the raw XML when finished. Resulting index is ~5-10 MB.
"""
import re
import sqlite3
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "lsj.sqlite"
TMP_DIR = DATA_DIR / "_lsj_tmp"
TMP_DIR.mkdir(exist_ok=True)

BASE_URL = (
    "https://raw.githubusercontent.com/PerseusDL/lexica/master/"
    "CTS_XML_TEI/perseus/pdllex/grc/lsj/grc.lsj.perseus-eng{n}.xml"
)
NUM_FILES = 27


# ---------------------- beta code -> Unicode ----------------------
# Perseus encodes Greek headwords in beta code: lower-case ASCII letters
# map to Greek letters, '*' marks capitals, and )( /\ = + | encode the
# breathings, accents, diaeresis, and iota subscript.

BETA_LETTERS = {
    "a": "α", "b": "β", "g": "γ", "d": "δ", "e": "ε", "z": "ζ", "h": "η",
    "q": "θ", "i": "ι", "k": "κ", "l": "λ", "m": "μ", "n": "ν", "c": "ξ",
    "o": "ο", "p": "π", "r": "ρ", "s": "σ", "t": "τ", "u": "υ", "f": "φ",
    "x": "χ", "y": "ψ", "w": "ω",
}
DIACRITICS = {
    ")": "̓",  # smooth breathing (comma above)
    "(": "̔",  # rough breathing
    "/": "́",  # acute
    "\\": "̀", # grave
    "=": "͂",  # circumflex / perispomeni
    "+": "̈",  # diaeresis
    "|": "ͅ",  # iota subscript
}


def beta_to_unicode(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "*":
            i += 1
            if i >= len(s):
                break
            base_letter = s[i].lower()
            base = BETA_LETTERS.get(base_letter)
            if base is None:
                out.append(s[i])
                i += 1
                continue
            i += 1
            # Diacritics may precede or follow the letter for capitals.
            mods = ""
            while i < len(s) and s[i] in DIACRITICS:
                mods += s[i]
                i += 1
            out.append(base.upper() + "".join(DIACRITICS[m] for m in mods))
            continue
        base = BETA_LETTERS.get(c.lower())
        if base is None:
            out.append(c)
            i += 1
            continue
        i += 1
        mods = ""
        while i < len(s) and s[i] in DIACRITICS:
            mods += s[i]
            i += 1
        out.append(base + "".join(DIACRITICS[m] for m in mods))
    text = "".join(out)
    # Final sigma: σ at word boundary -> ς
    text = re.sub(r"σ(?=$|[^\w])", "ς", text)
    return unicodedata.normalize("NFC", text)


def normalize_lemma(s: str) -> str:
    """Canonical form for index lookups."""
    s = unicodedata.normalize("NFC", s)
    # Strip Perseus' disambiguation digits ("θεός1") and hyphenation hints.
    s = re.sub(r"\d+$", "", s)
    s = s.replace("-", "").replace("_", "")
    return s


# ---------------------- XML extraction ----------------------

def local(tag: str) -> str:
    return tag.split("}")[-1]


def text_of(elem) -> str:
    return "".join(elem.itertext())


def clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    return s


def extract_definition(entry) -> str:
    """Return the shortest meaningful gloss for an LSJ entry.

    Strategy: prefer the first <tr> (translation marker — LSJ's curated
    short gloss). Fall back to the leading text of the first <sense>,
    minus citations and Greek quotations, truncated.
    """
    # First <tr>.
    for el in entry.iter():
        if local(el.tag) == "tr":
            t = clean(text_of(el))
            if t:
                return t.rstrip(",;: ")[:300]

    # First <sense>: walk children, skip noisy elements.
    SKIP = {"bibl", "cit", "foreign", "quote", "date", "biblScope", "title", "author"}
    for el in entry.iter():
        if local(el.tag) != "sense":
            continue
        parts = []
        if el.text:
            parts.append(el.text)
        for child in el:
            if local(child.tag) in SKIP:
                if child.tail:
                    parts.append(child.tail)
                continue
            parts.append(text_of(child))
            if child.tail:
                parts.append(child.tail)
        t = clean("".join(parts))
        if t:
            return t[:300]
    return ""


def iter_entries(xml_path: Path):
    """Yield (lemma_unicode, definition) pairs from one LSJ TEI file."""
    for _, elem in ET.iterparse(xml_path, events=("end",)):
        if local(elem.tag) != "entryFree":
            continue
        key = elem.get("key", "")
        if not key:
            elem.clear()
            continue
        lemma = beta_to_unicode(key)
        defn = extract_definition(elem)
        if lemma and defn:
            yield normalize_lemma(lemma), defn
        elem.clear()


# ---------------------- pipeline ----------------------

def download(n: int) -> Path:
    dst = TMP_DIR / f"eng{n}.xml"
    if dst.exists() and dst.stat().st_size > 1000:
        return dst
    url = BASE_URL.format(n=n)
    print(f"  fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "ItsAllGreekToMe-LSJ-builder"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    return dst


def build():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE lsj (lemma TEXT PRIMARY KEY, definition TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX idx_lsj_lemma ON lsj(lemma)")

    total = 0
    for n in range(1, NUM_FILES + 1):
        print(f"[{n}/{NUM_FILES}] downloading…")
        try:
            xml_path = download(n)
        except Exception as e:
            print(f"  WARN: file {n} failed ({e}); skipping")
            continue
        print(f"  parsing {xml_path.name} ({xml_path.stat().st_size // 1024} KB)…")
        batch = []
        for lemma, defn in iter_entries(xml_path):
            batch.append((lemma, defn))
            if len(batch) >= 1000:
                conn.executemany(
                    "INSERT OR IGNORE INTO lsj(lemma, definition) VALUES (?, ?)", batch
                )
                total += len(batch)
                batch.clear()
        if batch:
            conn.executemany(
                "INSERT OR IGNORE INTO lsj(lemma, definition) VALUES (?, ?)", batch
            )
            total += len(batch)
        conn.commit()
        # Free disk: keep the XMLs for resumability unless --clean is passed.
    rows = conn.execute("SELECT COUNT(*) FROM lsj").fetchone()[0]
    conn.close()
    print(f"\nDone. Inserted {total} entries; {rows} unique lemmas in {DB_PATH}.")
    if "--clean" in sys.argv:
        for f in TMP_DIR.iterdir():
            f.unlink()
        TMP_DIR.rmdir()
        print("Removed downloaded XML cache.")


if __name__ == "__main__":
    build()
