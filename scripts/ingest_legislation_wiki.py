#!/usr/bin/env python3
"""Ingest legislation.gov.uk raw markdown into Legal Shaman wiki pages."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from project_paths import LOGS_DIR, RAW_DIR, WIKI_DIR

SOURCE_NAME = "legislation.gov.uk"
INSTRUMENT_SLUG = "criminal-procedure-rules-2025"
INSTRUMENT_TITLE = "The Criminal Procedure Rules 2025"
INSTRUMENT_SHORT = "CrPR 2025"
BASE_URL = "https://www.legislation.gov.uk/uksi/2025/909"
RAW_ROOT = RAW_DIR / "legislation-govuk" / INSTRUMENT_SLUG
WIKI_REG = WIKI_DIR / "regulations"

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
RULE_HEADING = re.compile(r"^#### Rule ([0-9.]+)\s*$", re.M)
SECTION_HEADING = re.compile(r"^### (.+)$", re.M)
BULLET_LINE = re.compile(r"^\s+[a-zivx]+\.\s+(.+)$", re.M)


@dataclass
class RawDoc:
    path: Path
    title: str
    url: str
    part_number: str
    section: str
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
                part_number=meta.get("part_number", ""),
                section=meta.get("section", ""),
                body=body.strip(),
            )
        )
    return docs


def wiki_title_for(doc: RawDoc) -> str:
    if doc.section == "contents":
        return INSTRUMENT_TITLE
    if doc.section == "introduction":
        return f"{INSTRUMENT_TITLE} — Introduction"
    if doc.part_number:
        section = doc.section.title() if doc.section else doc.title
        return f"CrPR Part {doc.part_number} — {section}"
    return doc.title


def extract_rule_summaries(body: str) -> list[tuple[str, str, list[str]]]:
    rules: list[tuple[str, str, list[str]]] = []
    current_section = ""
    for line in body.splitlines():
        section_match = SECTION_HEADING.match(line.strip())
        if section_match:
            current_section = section_match.group(1).strip()
            continue
        rule_match = RULE_HEADING.match(line.strip())
        if not rule_match:
            continue
        rule_num = rule_match.group(1)
        section = current_section or f"Rule {rule_num}"
        rules.append((rule_num, section, []))

    for idx, (rule_num, section, _) in enumerate(rules):
        start = body.find(f"#### Rule {rule_num}")
        if start < 0:
            continue
        end = body.find("#### Rule ", start + 1)
        chunk = body[start:end] if end >= 0 else body[start:]
        points: list[str] = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if re.match(r"^[0-9]+\.$", line):
                continue
            bullet = re.match(r"^[a-zivx]+\.\s+(.+)$", line)
            if bullet:
                points.append(bullet.group(1).strip())
            elif len(line) > 20 and not line.startswith("Source:"):
                points.append(line)
        rules[idx] = (rule_num, section, points[:8])
    return rules


def first_sentences(text: str, limit: int = 2) -> str:
    sentences = re.split(r"(?<=[.;])\s+", text)
    picked = [s.strip() for s in sentences if s.strip()][:limit]
    return " ".join(picked)


def build_summary(doc: RawDoc, rules: list[tuple[str, str, list[str]]]) -> str:
    if doc.section == "contents":
        return (
            f"{INSTRUMENT_TITLE} (UKSI 2025/909) is the procedural code for criminal cases "
            "in England and Wales. Made on 15 July 2025 and in force from 6 October 2025, "
            "the Rules revoke and replace the Criminal Procedure Rules 2020. "
            "The instrument contains 50 Parts covering case management, evidence, trial, "
            "sentencing, appeals, bail, disclosure, and related criminal court procedure."
        )
    if doc.section == "introduction":
        return (
            "The Criminal Procedure Rule Committee made these Rules under section 69 of the "
            "Courts Act 2003 after consultation under section 72(1)(a). "
            "They revoke the Criminal Procedure Rules 2020 (S.I. 2020/759) and associated "
            "amending instruments, consolidate that provision, and may be cited as the "
            "Criminal Procedure Rules 2025. They come into force on 6 October 2025."
        )
    if not rules:
        return f"This Part of {INSTRUMENT_SHORT} sets out court procedure for {doc.section.lower()}."
    lead = rules[0]
    lead_text = lead[2][0] if lead[2] else lead[1]
    return (
        f"Part {doc.part_number} of {INSTRUMENT_SHORT} covers {doc.section.lower()}. "
        f"{first_sentences(lead_text)}"
    )


def build_key_information(rules: list[tuple[str, str, list[str]]]) -> list[str]:
    items: list[str] = []
    for rule_num, section, points in rules[:6]:
        if points:
            items.append(f"**Rule {rule_num} ({section})** — {points[0]}")
        else:
            items.append(f"**Rule {rule_num}** — {section}")
    return items


def build_practical_guidance(doc: RawDoc, rules: list[tuple[str, str, list[str]]]) -> list[str]:
    if doc.section == "contents":
        return [
            "Use the Part links below to locate the procedural rules for a specific stage of criminal proceedings.",
            "Part 1 sets out the overriding objective that applies across all criminal cases.",
            "Check the commencement date (6 October 2025) and whether later amendments apply to the version you rely on.",
        ]
    if doc.section == "introduction":
        return [
            "These Rules apply to criminal procedure in the senior courts and magistrates' courts in England and Wales.",
            "They replace the Criminal Procedure Rules 2020 and related amending instruments.",
            "Cite as the Criminal Procedure Rules 2025 (S.I. 2025/909).",
        ]
    guidance: list[str] = []
    for rule_num, section, points in rules[:5]:
        for point in points[:2]:
            if any(
                word in point.lower()
                for word in ("must", "should", "duty", "apply", "court", "party", "defendant")
            ):
                guidance.append(f"Rule {rule_num}: {point}")
    if not guidance and rules:
        rule_num, _, points = rules[0]
        if points:
            guidance.append(f"Rule {rule_num}: {points[0]}")
    return guidance[:8]


def related_concepts(doc: RawDoc) -> list[str]:
    common = ["[[The Criminal Procedure Rules 2025]]", "[[Criminal courts]]"]
    part_links = {
        "1": ["[[CrPR Part 3 — Case Management]]"],
        "3": ["[[CrPR Part 1 — The Overriding Objective]]"],
        "14": ["[[Bail]]"],
        "15": ["[[Disclosure in criminal cases]]"],
        "24": ["[[Magistrates' court]]"],
        "25": ["[[Crown Court]]"],
        "34": ["[[Appeals to the Crown Court]]"],
        "50": ["[[Extradition]]"],
    }
    return common + part_links.get(doc.part_number, [])


def related_organisations() -> list[str]:
    return [
        "[[Ministry of Justice]]",
        "[[Crown Prosecution Service]]",
        "[[Magistrates' courts]]",
        "[[Crown Court]]",
    ]


def write_wiki_page(doc: RawDoc) -> Path:
    title = wiki_title_for(doc)
    rules = extract_rule_summaries(doc.body)
    summary = build_summary(doc, rules)
    key_info = build_key_information(rules)
    guidance = build_practical_guidance(doc, rules)

    lines = [
        f"# {title}",
        "",
        "## Summary",
        "",
        summary,
        "",
        "## Key Information",
        "",
    ]
    if key_info:
        lines.extend(f"- {item}" for item in key_info)
    else:
        lines.append(f"- See source for full text of {doc.section or doc.title}.")
    lines.extend(["", "## Practical Guidance", ""])
    if guidance:
        lines.extend(f"- {item}" for item in guidance)
    else:
        lines.append("- Consult the source rules for procedural steps and court powers in this Part.")
    lines.extend(["", "## Related Concepts", ""])
    for link in related_concepts(doc):
        lines.append(f"- {link}")
    lines.extend(["", "## Related Organisations", ""])
    for link in related_organisations():
        lines.append(f"- {link}")
    if doc.section == "contents":
        part_links = []
        for line in doc.body.splitlines():
            match = re.match(r"^- \*\*PART (\d+)\*\* — (.+)$", line.strip())
            if match:
                part_num, part_title = match.groups()
                link_title = f"CrPR Part {part_num} — {part_title.title()}"
                part_links.append(f"- [[{link_title}]]")
        lines.extend(["", "## Parts", ""])
        lines.extend(part_links or ["- See source table of contents."])
    lines.extend(
        [
            "",
            "## Sources",
            "",
            f"- **{SOURCE_NAME}** — [{doc.title}]({doc.url}) — "
            f"`raw/legislation-govuk/{INSTRUMENT_SLUG}/{doc.path.name}`",
            "",
        ]
    )

    WIKI_REG.mkdir(parents=True, exist_ok=True)
    filename = f"{title}.md"
    out = WIKI_REG / filename
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def update_regulations_index(titles: list[str]) -> None:
    index_path = WIKI_REG / "_index.md"
    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else "# Regulations\n"
    new_links = [f"- [[{title}]]" for title in sorted(titles)]
    marker = "## Criminal Procedure Rules 2025"
    block = "\n".join([marker, ""] + new_links) + "\n"
    if marker in existing:
        prefix = existing.split(marker, 1)[0].rstrip()
        index_path.write_text(prefix + "\n\n" + block, encoding="utf-8")
    else:
        index_path.write_text(existing.rstrip() + "\n\n" + block, encoding="utf-8")


def append_ingestion_log(count: int) -> None:
    log_path = LOGS_DIR / "ingestion-log.md"
    entry = (
        f"| {date.today().isoformat()} | legislation.gov.uk / {INSTRUMENT_SHORT} | "
        f"{count} | {count} | 0 | Criminal procedure |"
    )
    text = log_path.read_text(encoding="utf-8")
    if "Entries" in text:
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
