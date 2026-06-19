#!/usr/bin/env python3
"""Crawl legislation.gov.uk instruments and save as Markdown in /raw."""

from __future__ import annotations

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from project_paths import LOGS_DIR, RAW_DIR

NS = "http://www.legislation.gov.uk/namespaces/legislation"
NS_MAP = {"leg": NS}
USER_AGENT = "LegalShamanObsidianCrawler/1.0 (+local research; respects robots.txt)"
SOURCE_LABEL = "legislation.gov.uk"

INSTRUMENTS = {
    "uksi/2025/909": {
        "slug": "criminal-procedure-rules-2025",
        "short_title": "CrPR 2025",
        "source_name": "The Criminal Procedure Rules 2025",
    },
}


@dataclass
class PageRecord:
    url: str
    title: str
    part_number: str
    section: str
    file: str
    status: str
    error: str = ""


@dataclass
class CrawlStats:
    instrument: str
    discovered: int = 0
    saved: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    pages: list[PageRecord] = field(default_factory=list)


def fetch_xml(url: str) -> ET.Element:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return ET.fromstring(resp.read())


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def text_of(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return clean_text("".join(element.itertext()))


def render_inline(element: ET.Element) -> str:
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in element:
        tag = child.tag.split("}")[-1]
        child_text = render_inline(child)
        if tag in {"FootnoteRef", "CommentaryRef"}:
            continue
        if tag == "Emphasis":
            parts.append(f"*{child_text}*")
        elif tag == "Strong":
            parts.append(f"**{child_text}**")
        elif tag == "Citation":
            parts.append(child_text)
        elif child_text:
            parts.append(child_text)
        if child.tail:
            parts.append(child.tail)
    return clean_text("".join(parts))


def render_block(element: ET.Element, depth: int = 0) -> list[str]:
    tag = element.tag.split("}")[-1]
    lines: list[str] = []

    if tag in {"Footnote", "Commentary", "FootnoteText", "CommentaryText"}:
        return lines

    if tag == "Title":
        text = text_of(element)
        if text:
            level = min(depth + 2, 6)
            lines.append(f"{'#' * level} {text}")
            lines.append("")
        return lines

    if tag == "P1group":
        title = text_of(element.find(f"{{{NS}}}Title"))
        if title:
            lines.append(f"### {title}")
            lines.append("")
        for child in element:
            if child.tag.split("}")[-1] == "P1":
                lines.extend(render_block(child, depth + 1))
        return lines

    if tag == "P1":
        number = text_of(element.find(f"{{{NS}}}Pnumber"))
        para = element.find(f"{{{NS}}}P1para")
        if number:
            lines.append(f"#### Rule {number}")
            lines.append("")
        if para is not None:
            lines.extend(render_block(para, depth + 1))
        return lines

    if tag in {"P1para", "P2para", "P3para", "P4para", "IntroductoryText", "Body", "Pblock"}:
        for child in element:
            lines.extend(render_block(child, depth))
        if not list(element) and element.text:
            text = clean_text(element.text)
            if text:
                lines.append(text)
                lines.append("")
        return lines

    if tag in {"P2", "P3", "P4"}:
        number = text_of(element.find(f"{{{NS}}}Pnumber"))
        para_tag = tag + "para"
        para = element.find(f"{{{NS}}}{para_tag}")
        prefix = "  " * depth
        if number:
            lines.append(f"{prefix}{number}.")
        if para is not None:
            for child in para:
                child_tag = child.tag.split("}")[-1]
                if child_tag == "Text":
                    text = render_inline(child)
                    if text:
                        lines.append(f"{prefix}  {text}")
                else:
                    lines.extend(render_block(child, depth + 1))
            lines.append("")
        return lines

    if tag == "Text":
        text = render_inline(element)
        if text:
            lines.append(text)
            lines.append("")
        return lines

    if tag == "Para":
        for child in element:
            lines.extend(render_block(child, depth))
        return lines

    if tag == "Part":
        for child in element:
            lines.extend(render_block(child, depth))
        return lines

    for child in element:
        lines.extend(render_block(child, depth))
    return lines


def render_part(part: ET.Element) -> str:
    lines = render_block(part)
    body = "\n".join(lines)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def render_introduction(root: ET.Element) -> str:
    prelims = root.find(f".//{{{NS}}}SecondaryPrelims")
    if prelims is None:
        return ""
    lines: list[str] = []
    for child in prelims:
        lines.extend(render_block(child))
    body = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:120]


def make_filename(part_number: str, title: str, page_type: str) -> str:
    if page_type == "contents":
        return "contents.md"
    if page_type == "introduction":
        return "introduction.md"
    part_slug = f"part-{int(part_number):02d}" if part_number.isdigit() else slugify(part_number)
    title_slug = slugify(title.replace("PART", "").strip())[:80]
    return f"{part_slug}-{title_slug}.md"


def yaml_escape(value: str) -> str:
    return value.replace('"', '\\"')


def write_markdown(
    path: Path,
    *,
    source: str,
    url: str,
    title: str,
    instrument_id: str,
    part_number: str,
    section: str,
    last_updated: str,
    crawl_date: str,
    body: str,
) -> None:
    frontmatter = (
        "---\n"
        f"source: {source}\n"
        f'url: "{yaml_escape(url)}"\n'
        f'title: "{yaml_escape(title)}"\n'
        f'instrument_id: "{yaml_escape(instrument_id)}"\n'
        f'part_number: "{yaml_escape(part_number)}"\n'
        f'section: "{yaml_escape(section)}"\n'
        f'last_updated: "{yaml_escape(last_updated)}"\n'
        f'crawl_date: "{crawl_date}"\n'
        "---\n\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter + body + "\n", encoding="utf-8")


def build_contents_markdown(parts: list[tuple[str, str, str]]) -> str:
    lines = ["## Table of contents", ""]
    for part_number, title, url in parts:
        lines.append(f"- **PART {part_number}** — {title}")
        lines.append(f"  - Source: {url}")
    return "\n".join(lines)


def crawl_instrument(instrument_path: str, config: dict[str, str]) -> CrawlStats:
    stats = CrawlStats(instrument=instrument_path)
    crawl_date = date.today().isoformat()
    base_url = f"https://www.legislation.gov.uk/{instrument_path}"
    xml_url = f"{base_url}/data.xml"
    output_dir = RAW_DIR / "legislation-govuk" / config["slug"]
    index_path = output_dir / "index.json"

    print(f"Fetching {xml_url}")
    root = fetch_xml(xml_url)

    metadata = root.find(f".//{{{NS}}}Metadata")
    last_updated = ""
    if metadata is not None:
        modified = metadata.find(".//{http://purl.org/dc/elements/1.1/}modified")
        if modified is not None and modified.text:
            last_updated = modified.text.strip()

    instrument_title = config["source_name"]
    part_records: list[tuple[str, str, str]] = []

    intro_body = render_introduction(root)
    if intro_body:
        intro_url = f"{base_url}/introduction"
        intro_file = make_filename("", "", "introduction")
        intro_path = output_dir / intro_file
        write_markdown(
            intro_path,
            source=SOURCE_LABEL,
            url=intro_url,
            title=f"{instrument_title} — Introductory Text",
            instrument_id=instrument_path,
            part_number="",
            section="introduction",
            last_updated=last_updated,
            crawl_date=crawl_date,
            body=intro_body,
        )
        stats.saved += 1
        stats.pages.append(
            PageRecord(
                url=intro_url,
                title=f"{instrument_title} — Introductory Text",
                part_number="",
                section="introduction",
                file=str(intro_path.relative_to(RAW_DIR)),
                status="saved",
            )
        )

    parts = root.findall(f".//{{{NS}}}Part")
    stats.discovered = len(parts) + 2

    for part in parts:
        doc_uri = part.get("DocumentURI", "")
        part_url = doc_uri or base_url
        number_el = part.find(f"{{{NS}}}Number")
        title_el = part.find(f"{{{NS}}}Title")
        part_number = text_of(number_el).replace("PART", "").strip()
        part_title = text_of(title_el)
        full_title = f"PART {part_number} — {part_title}" if part_title else f"PART {part_number}"

        part_records.append((part_number, part_title, part_url))

        body = render_part(part)
        if not body:
            stats.skipped += 1
            stats.pages.append(
                PageRecord(
                    url=part_url,
                    title=full_title,
                    part_number=part_number,
                    section=part_title,
                    file="",
                    status="skipped",
                    error="no extractable content",
                )
            )
            continue

        filename = make_filename(part_number, part_title, "part")
        file_path = output_dir / filename
        write_markdown(
            file_path,
            source=SOURCE_LABEL,
            url=part_url,
            title=full_title,
            instrument_id=instrument_path,
            part_number=part_number,
            section=part_title,
            last_updated=last_updated,
            crawl_date=crawl_date,
            body=body,
        )
        stats.saved += 1
        stats.pages.append(
            PageRecord(
                url=part_url,
                title=full_title,
                part_number=part_number,
                section=part_title,
                file=str(file_path.relative_to(RAW_DIR)),
                status="saved",
            )
        )
        print(f"Saved {filename}")

    contents_url = f"{base_url}/contents"
    contents_body = build_contents_markdown(part_records)
    contents_file = make_filename("", "", "contents")
    contents_path = output_dir / contents_file
    write_markdown(
        contents_path,
        source=SOURCE_LABEL,
        url=contents_url,
        title=f"{instrument_title} — Table of Contents",
        instrument_id=instrument_path,
        part_number="",
        section="contents",
        last_updated=last_updated,
        crawl_date=crawl_date,
        body=contents_body,
    )
    stats.saved += 1
    stats.pages.append(
        PageRecord(
            url=contents_url,
            title=f"{instrument_title} — Table of Contents",
            part_number="",
            section="contents",
            file=str(contents_path.relative_to(RAW_DIR)),
            status="saved",
        )
    )

    index_data = {
        "instrument": instrument_path,
        "title": instrument_title,
        "source": SOURCE_LABEL,
        "base_url": base_url,
        "crawl_date": crawl_date,
        "last_updated": last_updated,
        "pages_saved": stats.saved,
        "pages": [
            {
                "url": page.url,
                "title": page.title,
                "part_number": page.part_number,
                "section": page.section,
                "file": page.file,
                "status": page.status,
            }
            for page in stats.pages
        ],
    }
    index_path.write_text(json.dumps(index_data, indent=2) + "\n", encoding="utf-8")
    return stats


def write_report(all_stats: list[CrawlStats]) -> None:
    lines = [
        "# legislation.gov.uk crawl report",
        "",
        f"Date: {date.today().isoformat()}",
        "",
    ]
    for stats in all_stats:
        lines.extend(
            [
                f"## {stats.instrument}",
                "",
                f"- Discovered: {stats.discovered}",
                f"- Saved: {stats.saved}",
                f"- Skipped: {stats.skipped}",
                f"- Errors: {len(stats.errors)}",
                "",
            ]
        )
        if stats.errors:
            lines.append("### Errors")
            lines.extend(f"- {error}" for error in stats.errors)
            lines.append("")
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (LOGS_DIR / "legislation-govuk-crawl-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    targets = argv[1:] or list(INSTRUMENTS)
    all_stats: list[CrawlStats] = []

    for instrument in targets:
        config = INSTRUMENTS.get(instrument)
        if config is None:
            print(f"Unknown instrument: {instrument}", file=sys.stderr)
            return 1
        try:
            stats = crawl_instrument(instrument, config)
            all_stats.append(stats)
            print(f"Done: {stats.saved} files saved for {instrument}")
        except Exception as exc:  # noqa: BLE001
            print(f"Failed {instrument}: {exc}", file=sys.stderr)
            return 1

    write_report(all_stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
