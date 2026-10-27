"""Parse RFC plain-text into sections and a table of contents."""

from __future__ import annotations

import re

HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.? {1,2}(\S.*)$")
PAGE_FOOTER_RE = re.compile(r"\[Page \d+\]\s*$")
PAGE_HEADER_HINTS = ("Internet-Draft", "Workgroup", "Request for Comments:",
                     "Best Current Practice", "Standards Track", "Informational",
                     "Experimental", "Historical")


def clean_text(text: str) -> str:
    """Remove form feeds and page headers/footers from RFC body text."""
    out = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line == "\f" or line.startswith("\f"):
            continue
        if PAGE_FOOTER_RE.search(line):
            continue
        if ("\f" not in line and line.strip()
                and i + 1 < len(lines)
                and PAGE_FOOTER_RE.search(lines[i + 1])
                and any(h in line for h in PAGE_HEADER_HINTS)):
            continue
        out.append(line)
    cleaned = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def find_headings(text: str) -> list[dict]:
    """Locate numbered section headings (flush-left), skipping the TOC."""
    lines = text.split("\n")
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1

    found: dict[str, dict] = {}
    order: list[str] = []
    for idx, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if not m:
            continue
        num, title = m.group(1), m.group(2).strip().rstrip(".")
        entry = {
            "number": num,
            "title": title,
            "start": offsets[idx],
            "depth": num.count("."),
        }
        if num in found:
            # duplicate heading numbers: keep the later occurrence (body
            # wins over any stray early match such as an inline TOC)
            found[num] = entry
        else:
            found[num] = entry
            order.append(num)

    headings = [found[n] for n in order]
    headings.sort(key=lambda h: h["start"])
    for i, h in enumerate(headings):
        h["end"] = headings[i + 1]["start"] if i + 1 < len(headings) else len(text)
    return headings


def section_span(headings: list[dict], number: str,
                 include_subsections: bool = True) -> tuple[int, int] | None:
    """Byte span of a section within the text, optionally including children."""
    target = next((h for h in headings if h["number"] == number), None)
    if target is None:
        return None
    if not include_subsections:
        return target["start"], target["end"]
    prefix = number + "."
    end = len_target_end = target["end"]
    for h in headings:
        if h["start"] <= target["start"]:
            continue
        if h["number"] == number or not h["number"].startswith(prefix):
            return target["start"], h["start"]
    return target["start"], len_target_end


def parse_sections(text: str, include_subsections: bool = True) -> list[dict]:
    """Split RFC text into [{number, title, depth, content}, ...]."""
    headings = find_headings(text)
    sections = []
    for h in headings:
        if include_subsections:
            span = section_span(headings, h["number"], True)
            content = text[span[0]:span[1]] if span else ""
        else:
            content = text[h["start"]:h["end"]]
        sections.append({
            "number": h["number"],
            "title": h["title"],
            "depth": h["depth"],
            "content": clean_text(content),
        })
    return sections


def table_of_contents(text: str) -> list[dict]:
    """Return [{number, title}] for every numbered section."""
    return [{"number": h["number"], "title": h["title"]}
            for h in find_headings(text)]
