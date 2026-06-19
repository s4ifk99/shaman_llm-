#!/usr/bin/env python3
"""Ingest Sentencing Council raw markdown into Legal Shaman wiki pages."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from project_paths import LOGS_DIR, RAW_DIR, WIKI_DIR

SOURCE_NAME = "Sentencing Council"
RAW_ROOT = RAW_DIR / "sentencing-council" / "magistrates"
WIKI_REG = WIKI_DIR / "regulations"
HUB_TITLE = "Magistrates' courts sentencing guidelines"

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
HEADING = re.compile(r"^#{1,3}\s+(.+)$", re.M)
STEP_HEADING = re.compile(r"^##\s+Step\s+\d+", re.M)
BULLET = re.compile(r"^[-*]\s+(.+)$", re.M)
BLOCKQUOTE = re.compile(r"^>\s+(.+)$", re.M)


@dataclass
class RawDoc:
    path: Path
    title: str
    url: str
    guideline_type: str
    acts: str
    collection: str
    effective_from: str
    body: str


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if line.startswith("tags:") or line.startswith("court_types:"):
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip('"')
    return meta, text[match.end() :]


def load_raw_docs() -> list[RawDoc]:
    docs: list[RawDoc] = []
    for path in sorted(RAW_ROOT.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        docs.append(
            RawDoc(
                path=path,
                title=meta.get("title", path.stem),
                url=meta.get("url", ""),
                guideline_type=meta.get("guideline_type", "offence"),
                acts=meta.get("acts", ""),
                collection=meta.get("collection", ""),
                effective_from=meta.get("effective_from", ""),
                body=body.strip(),
            )
        )
    return docs


INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')


MAX_FILENAME_LEN = 120


def sanitize_title(title: str) -> str:
    return INVALID_CHARS.sub(" - ", title).strip()


def wiki_filename(title: str) -> str:
    safe = sanitize_title(title)
    if len(safe) <= MAX_FILENAME_LEN:
        return f"{safe}.md"
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", safe.lower()).strip("-")
    return f"{safe[:80].rstrip(' -')}-{slug[-40:]}.md"


def wiki_title_for(doc: RawDoc) -> str:
    if doc.guideline_type == "index":
        return HUB_TITLE
    safe = sanitize_title(doc.title)
    if doc.guideline_type == "supplementary":
        return f"Magistrates sentencing — {safe}"
    if doc.guideline_type == "overarching":
        return f"Magistrates sentencing — {safe}"
    return f"Magistrates sentencing — {safe}"


def extract_points(body: str, limit: int = 8) -> list[str]:
    points: list[str] = []
    for match in STEP_HEADING.finditer(body):
        points.append(match.group(0).replace("## ", ""))
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(">") and len(stripped) > 20:
            points.append(stripped.lstrip("> ").strip())
        bullet = BULLET.match(stripped)
        if bullet and len(bullet.group(1)) > 30:
            points.append(bullet.group(1).strip())
        if len(points) >= limit:
            break
    return points[:limit]


def first_paragraph(body: str) -> str:
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in {"Crown Court", "Magistrates", "* * *"}:
            continue
        if stripped.startswith(">"):
            lines.append(stripped.lstrip("> ").strip())
        elif not stripped.startswith(("-", "*")):
            lines.append(stripped)
        if len(" ".join(lines)) > 120:
            break
    return " ".join(lines)[:500]


def build_summary(doc: RawDoc) -> str:
    if doc.guideline_type == "index":
        return (
            "The Sentencing Council publishes definitive sentencing guidelines for "
            "magistrates' courts in England and Wales. The hub lists overarching "
            "principles, offence-specific guidelines, and supplementary information on "
            "fines, compensation, guilty pleas, and related sentencing matters."
        )
    lead = first_paragraph(doc.body)
    parts = [f"This Sentencing Council guideline covers {doc.title.lower()} in magistrates' courts."]
    if doc.acts:
        parts.append(f"Legislation: {doc.acts.strip(' -')}.")
    if doc.effective_from:
        parts.append(f"Effective from {doc.effective_from}.")
    if lead:
        parts.append(lead)
    return " ".join(parts)


def build_key_information(doc: RawDoc) -> list[str]:
    items: list[str] = []
    if doc.acts:
        items.append(f"**Legislation** — {doc.acts.strip(' -')}")
    if doc.collection:
        items.append(f"**Collection** — {doc.collection}")
    if doc.effective_from:
        items.append(f"**Effective from** — {doc.effective_from}")
    for point in extract_points(doc.body, limit=6):
        items.append(point)
    if not items:
        items.append(f"See source for the full {doc.guideline_type} guideline text.")
    return items


def build_practical_guidance(doc: RawDoc) -> list[str]:
    if doc.guideline_type == "index":
        return [
            "Locate the relevant offence-specific guideline before sentencing in the magistrates' court.",
            "Apply overarching guidelines (for example guilty plea reduction and totality) alongside offence guidelines.",
            "Use supplementary information for fines, compensation, and ancillary orders.",
        ]
    guidance: list[str] = []
    for point in extract_points(doc.body, limit=10):
        if any(word in point.lower() for word in ("must", "should", "court", "step", "sentence")):
            guidance.append(point)
    if not guidance:
        guidance = extract_points(doc.body, limit=4)
    return guidance[:8]


def related_concepts(doc: RawDoc) -> list[str]:
    links = [f"[[{HUB_TITLE}]]", "[[Sentencing]]", "[[Magistrates' court]]"]
    type_links = {
        "overarching": ["[[Magistrates sentencing — General guideline: overarching principles]]"],
        "offence": ["[[Magistrates sentencing — Reduction in sentence for a guilty plea - first hearing on or after 1 June 2017]]"],
        "supplementary": ["[[Magistrates sentencing — Using the guidelines]]"],
    }
    return links + type_links.get(doc.guideline_type, [])


def related_organisations() -> list[str]:
    return [
        "[[Sentencing Council]]",
        "[[Ministry of Justice]]",
        "[[Magistrates' courts]]",
        "[[Crown Prosecution Service]]",
    ]


def write_wiki_page(doc: RawDoc) -> Path:
    title = wiki_title_for(doc)
    lines = [
        f"# {title}",
        "",
        "## Summary",
        "",
        build_summary(doc),
        "",
        "## Key Information",
        "",
    ]
    lines.extend(f"- {item}" for item in build_key_information(doc))
    lines.extend(["", "## Practical Guidance", ""])
    lines.extend(f"- {item}" for item in build_practical_guidance(doc))
    lines.extend(["", "## Related Concepts", ""])
    lines.extend(f"- {link}" for link in related_concepts(doc))
    lines.extend(["", "## Related Organisations", ""])
    lines.extend(f"- {link}" for link in related_organisations())

    if doc.guideline_type == "index":
        offences = sorted(
            d.title for d in load_raw_docs() if d.guideline_type == "offence"
        )
        overarching = sorted(
            wiki_title_for(d)
            for d in load_raw_docs()
            if d.guideline_type == "overarching"
        )
        supplementary = sorted(
            wiki_title_for(d)
            for d in load_raw_docs()
            if d.guideline_type == "supplementary"
        )
        lines.extend(["", "## Overarching guidelines", ""])
        lines.extend(f"- [[{name}]]" for name in overarching)
        lines.extend(["", "## Supplementary information", ""])
        lines.extend(f"- [[{name}]]" for name in supplementary)
        lines.extend(["", "## Offence guidelines", ""])
        lines.extend(
            f"- [[Magistrates sentencing — {sanitize_title(name)}]]"
            for name in offences[:50]
        )
        if len(offences) > 50:
            lines.append(f"- …and {len(offences) - 50} further offence guidelines (see raw index).")

    lines.extend(
        [
            "",
            "## Sources",
            "",
            f"- **{SOURCE_NAME}** — [{doc.title}]({doc.url}) — "
            f"`raw/sentencing-council/magistrates/{doc.path.name}`",
            "",
        ]
    )

    WIKI_REG.mkdir(parents=True, exist_ok=True)
    out = WIKI_REG / wiki_filename(title)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def update_regulations_index(titles: list[str]) -> None:
    index_path = WIKI_REG / "_index.md"
    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else "# Regulations\n"
    marker = "## Sentencing Council — Magistrates' court guidelines"
    new_links = [f"- [[{title}]]" for title in sorted(set(titles))]
    block = "\n".join([marker, ""] + new_links) + "\n"
    if marker in existing:
        prefix = existing.split(marker, 1)[0].rstrip()
        index_path.write_text(prefix + "\n\n" + block, encoding="utf-8")
    else:
        index_path.write_text(existing.rstrip() + "\n\n" + block, encoding="utf-8")


def append_ingestion_log(count: int) -> None:
    log_path = LOGS_DIR / "ingestion-log.md"
    entry = (
        f"| {date.today().isoformat()} | Sentencing Council / magistrates | "
        f"{count} | {count} | 0 | Sentencing |"
    )
    text = log_path.read_text(encoding="utf-8")
    if "## Entries" in text:
        text = text.replace("## Entries\n", f"## Entries\n\n{entry}\n", 1)
    else:
        text += f"\n{entry}\n"
    log_path.write_text(text, encoding="utf-8")


def main() -> int:
    docs = load_raw_docs()
    if not docs:
        print(f"No raw files found in {RAW_ROOT}", file=sys.stderr)
        return 1

    titles: list[str] = []
    for doc in docs:
        out = write_wiki_page(doc)
        titles.append(wiki_title_for(doc))
        print(f"Wrote {out.name}")

    update_regulations_index(titles)
    append_ingestion_log(len(docs))
    print(f"Ingested {len(docs)} wiki pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
