"""Extract clean line-numbered Greek text from Perseus TEI XML."""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"tei": "http://www.tei-c.org/ns/1.0"}


def text_of(elem) -> str:
    """Concatenate all text within an element, ignoring tags but preserving inner content."""
    # itertext() walks descendants in document order.
    return "".join(elem.itertext())


def extract(xml_path: Path) -> list[str]:
    """Return list of output lines: '[Speaker]' markers and 'NN  text' for verse lines."""
    tree = ET.parse(xml_path)
    body = tree.find(".//tei:body", NS)
    if body is None:
        raise SystemExit(f"No body in {xml_path}")

    out: list[str] = []
    last_speaker = None

    # Walk in document order.
    for elem in body.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "speaker":
            sp = re.sub(r"\s+", " ", text_of(elem)).strip()
            if sp and sp != last_speaker:
                out.append(f"[{sp}]")
                last_speaker = sp
        elif tag == "l":
            n = elem.get("n", "").strip()
            txt = re.sub(r"\s+", " ", text_of(elem)).strip()
            if not txt:
                continue
            if n and n != "0":
                out.append(f"{n}\t{txt}")
            elif n == "0":
                # Skip the display-only first-speaker repetition.
                continue
            else:
                out.append(txt)
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: extract_tei.py input.xml [output.txt]")
        sys.exit(1)
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".txt")
    lines = extract(src)
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(lines)} lines to {dst}")
