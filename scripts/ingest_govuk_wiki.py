#!/usr/bin/env python3
"""Ingest GOV.UK citizenship raw markdown into Legal Shaman wiki pages."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from project_paths import LOGS_DIR, RAW_DIR, WIKI_DIR

SOURCE_NAME = "GOV.UK"
RAW_ROOT = RAW_DIR / "govuk" / "citizenship"
WIKI_PROCEDURES = WIKI_DIR / "procedures"
WIKI_TOPICS = WIKI_DIR / "topics"
WIKI_CONCEPTS = WIKI_DIR / "concepts"
HUB_TITLE = "British citizenship"

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
HEADING = re.compile(r"^#{1,3}\s+(.+)$", re.M)
BULLET = re.compile(r"^[-*]\s+(.+)$", re.M)


@dataclass
class RawDoc:
    path: Path
    title: str
    url: str
    document_type: str
    page_type: str
    section: str
    collection: str
    description: str
    body: str


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
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
                document_type=meta.get("document_type", ""),
                page_type=meta.get("page_type", "page"),
                section=meta.get("section", ""),
                collection=meta.get("collection", ""),
                description=meta.get("description", ""),
                body=body.strip(),
            )
        )
    return docs


def wiki_category(doc: RawDoc) -> str:
    if doc.page_type == "index":
        return "topics"
    if doc.document_type in {"answer", "guide"} and doc.page_type not in {
        "collection",
        "publication",
    }:
        if any(
            word in doc.title.lower()
            for word in ("dual", "types of", "check if", "eligibility", "right of abode")
        ):
            return "concepts"
    return "procedures"


def wiki_title_for(doc: RawDoc) -> str:
    if doc.page_type == "index":
        return HUB_TITLE
    return f"British citizenship — {doc.title}"


def first_paragraph(body: str) -> str:
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("-", "*", ">")):
            if stripped.startswith(">"):
                lines.append(stripped.lstrip("> ").strip())
            continue
        lines.append(stripped)
        if len(" ".join(lines)) > 140:
            break
    return " ".join(lines)[:500]


def extract_points(body: str, limit: int = 8) -> list[str]:
    points: list[str] = []
    for match in HEADING.finditer(body):
        heading = match.group(1).strip()
        if heading.lower() not in {"eligibility", "how to apply", "fees", "documents"}:
            continue
        points.append(heading)
    for line in body.splitlines():
        bullet = BULLET.match(line.strip())
        if bullet and len(bullet.group(1)) > 25:
            points.append(bullet.group(1).strip())
        if len(points) >= limit:
            break
    return points[:limit]


def build_summary(doc: RawDoc) -> str:
    if doc.page_type == "index":
        return (
            "GOV.UK guidance on British citizenship, covering eligibility checks, "
            "naturalisation and registration routes, citizenship ceremonies, passport "
            "eligibility, forms, and related nationality matters."
        )
    lead = first_paragraph(doc.body)
    parts = [f"GOV.UK guidance on {doc.title.lower()}."]
    if doc.description:
        parts.append(doc.description)
    if doc.section:
        parts.append(f"Listed under: {doc.section}.")
    if lead:
        parts.append(lead)
    return " ".join(parts)


def build_key_information(doc: RawDoc) -> list[str]:
    items: list[str] = []
    if doc.section:
        items.append(f"**Browse section** — {doc.section}")
    if doc.collection:
        items.append(f"**Collection** — {doc.collection}")
    if doc.document_type:
        items.append(f"**Document type** — {doc.document_type}")
    for point in extract_points(doc.body):
        items.append(point)
    if not items:
        items.append("See source for full eligibility and application details.")
    return items[:8]


def build_practical_guidance(doc: RawDoc) -> list[str]:
    if doc.page_type == "index":
        return [
            "Check eligibility before applying — routes differ depending on birth, parentage, marriage, ILR/settled status, or special circumstances.",
            "Most applicants must meet residence, good character, English language, and Life in the UK Test requirements.",
            "Use the linked forms and guidance pages for the correct application route.",
        ]
    guidance = [
        point
        for point in extract_points(doc.body, limit=12)
        if any(
            word in point.lower()
            for word in ("apply", "must", "need", "eligible", "fee", "form", "test", "ceremony")
        )
    ]
    if not guidance:
        lead = first_paragraph(doc.body)
        if lead:
            guidance.append(lead)
    return guidance[:6]


def related_links(doc: RawDoc) -> list[str]:
    common = [f"[[{HUB_TITLE}]]", "[[Citizenship and living in the UK]]"]
    extras = {
        "index": [
            "[[British citizenship — Check if you can become a British citizen]]",
            "[[British citizenship — Life in the UK Test]]",
        ],
        "collection": ["[[British citizenship — Citizenship application forms]]"],
    }
    return common + extras.get(doc.page_type, ["[[British citizenship — Check if you can become a British citizen]]"])


def related_organisations() -> list[str]:
    return [
        "[[Home Office]]",
        "[[UK Visas and Immigration]]",
        "[[GOV.UK]]",
    ]


def write_wiki_page(doc: RawDoc) -> Path:
    title = wiki_title_for(doc)
    category = wiki_category(doc)
    wiki_dir = {"topics": WIKI_TOPICS, "concepts": WIKI_CONCEPTS, "procedures": WIKI_PROCEDURES}[category]

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
    lines.extend(f"- {link}" for link in related_links(doc))
    lines.extend(["", "## Related Organisations", ""])
    lines.extend(f"- {link}" for link in related_organisations())

    if doc.page_type == "index":
        docs = [d for d in load_raw_docs() if d.page_type != "index"]
        by_section: dict[str, list[RawDoc]] = {}
        for d in docs:
            by_section.setdefault(d.section or "Other", []).append(d)
        for section_name in sorted(by_section):
            lines.extend(["", f"## {section_name}", ""])
            for item in sorted(by_section[section_name], key=lambda d: d.title)[:30]:
                lines.append(f"- [[British citizenship — {item.title}]]")
            remaining = len(by_section[section_name]) - 30
            if remaining > 0:
                lines.append(f"- …and {remaining} more pages (see raw index).")

    lines.extend(
        [
            "",
            "## Sources",
            "",
            f"- **{SOURCE_NAME}** — [{doc.title}]({doc.url}) — "
            f"`raw/govuk/citizenship/{doc.path.name}`",
            "",
        ]
    )

    wiki_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{title}.md"
    if len(filename) > 120:
        filename = f"{title[:100].rstrip()} — {doc.path.stem}.md"
    out = wiki_dir / filename
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def update_indexes(titles_by_category: dict[str, list[str]]) -> None:
    for category, titles in titles_by_category.items():
        index_path = WIKI_DIR / category / "_index.md"
        existing = index_path.read_text(encoding="utf-8") if index_path.exists() else f"# {category.title()}\n"
        marker = "## British citizenship (GOV.UK)"
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
        f"| {date.today().isoformat()} | GOV.UK / British citizenship | "
        f"{count} | {count} | 0 | Citizenship |"
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

    titles_by_category: dict[str, list[str]] = {}
    for doc in docs:
        out = write_wiki_page(doc)
        title = wiki_title_for(doc)
        category = wiki_category(doc)
        titles_by_category.setdefault(category, []).append(title)
        print(f"Wrote {category}/{out.name}")

    update_indexes(titles_by_category)
    append_ingestion_log(len(docs))
    print(f"Ingested {len(docs)} wiki pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
