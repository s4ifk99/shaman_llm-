#!/usr/bin/env python3
"""Ingest crawled raw markdown into Legal Shaman wiki pages."""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from project_paths import LOGS_DIR, RAW_DIR, WIKI_DIR

SOURCE_ORGS = {
    "advicenow": "Advicenow",
    "citizens-advice": "Citizens Advice",
    "lawhive": "Lawhive Knowledge Hub",
    "cpr": "Civil Procedure Rules",
    "taylor-rose": "Taylor Rose",
    "legislation-govuk": "legislation.gov.uk",
    "sentencing-council": "Sentencing Council",
    "govuk": "GOV.UK",
}

SOURCE_ORDER = ("advicenow", "citizens-advice", "lawhive", "cpr", "taylor-rose", "legislation-govuk", "sentencing-council", "govuk")

INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')
MULTI_SPACE = re.compile(r"\s+")
FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
JUNK_PATTERNS = (
    re.compile(r"You must have JavaScript enabled.*", re.DOTALL | re.I),
    re.compile(r"Was this information useful\?.*", re.DOTALL | re.I),
    re.compile(r"Choose as many as you want\..*", re.DOTALL | re.I),
    re.compile(
        r"Take 5 minutes to tell us if you found what you needed on our website\..*?(?=\n\n|\Z)",
        re.DOTALL | re.I,
    ),
    re.compile(r"## Help us improve our website\s*\n.*?(?=\n## |\Z)", re.DOTALL | re.I),
    re.compile(r"### Table of Contents\s*\n.*?(?=\n### |\n## |\Z)", re.DOTALL | re.I),
    re.compile(r"^Open\s*$", re.M),
    re.compile(r"^Close\s*$", re.M),
    re.compile(r"^Next Section.*$", re.M),
    re.compile(r"^Tell us what you think\s*$", re.M),
    re.compile(r"^Please help us\s*$", re.M),
    re.compile(r"^\[Quality checked\].*$", re.M),
    re.compile(r"^Last updated:.*$", re.M),
    re.compile(r"^!\[.*?\]\(.*?\)\s*$", re.M),
    re.compile(r"Cookies at taylor-rose\.co\.uk.*", re.DOTALL | re.I),
    re.compile(r"We use cookies to provide you with the best possible experience\..*", re.DOTALL | re.I),
    re.compile(r"Essential Cookies\s*\n.*", re.DOTALL | re.I),
    re.compile(r"Get Practical Legal Updates\..*", re.DOTALL | re.I),
    re.compile(r"Send Enquiry\s*\n.*", re.DOTALL | re.I),
)

CONTENT_CATEGORIES = ("procedures", "concepts", "courts", "regulations", "funding")

SKIP_HEADINGS = frozenset(
    {
        "summary",
        "thanks",
        "help us improve our website",
        "table of contents",
        "contents of this part",
        "contents of this practice direction",
        "title",
        "number",
        "please help us",
        "scotland and northern ireland",
    }
)


@dataclass
class RawPage:
    source_key: str
    source_name: str
    filename: str
    title: str
    url: str
    topic: str
    section: str
    body: str
    raw_path: str

    @property
    def wiki_title(self) -> str:
        return self.title.strip() or Path(self.filename).stem

    @property
    def resolved_topic(self) -> str:
        if self.topic.strip():
            return self.topic.strip()
        if self.section.strip():
            return self.section.strip()
        return topic_from_url(self.url, self.source_key)


@dataclass
class IngestStats:
    source: str
    processed: int = 0
    created: int = 0
    updated: int = 0
    skipped_curated: int = 0
    topics: set[str] = field(default_factory=set)


def topic_from_url(url: str, source_key: str) -> str:
    if source_key == "advicenow":
        m = re.search(r"/get-help/([^/]+)", url)
        if m:
            return slug_to_title(m.group(1))
    if source_key == "citizens-advice":
        m = re.search(r"citizensadvice\.org\.uk/([^/]+)", url)
        if m:
            return slug_to_title(m.group(1))
    if source_key == "lawhive":
        m = re.search(r"/knowledge-hub/([^/]+)", url)
        if m:
            return slug_to_title(m.group(1))
    if source_key == "cpr":
        return "Civil Procedure Rules"
    if source_key == "taylor-rose":
        return "Insights"
    return "General"


def slug_to_title(slug: str) -> str:
    return MULTI_SPACE.sub(" ", slug.replace("-", " ").replace("_", " ")).strip().title()


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().strip('"').strip("'")
        meta[key.strip()] = value
    return meta, text[match.end() :]


def normalize_text(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_link_only(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    without_links = re.sub(r"\[([^\]]+)\]\([^)]+\)", "", stripped)
    return len(without_links.strip()) < 20


def clean_body(body: str) -> str:
    body = body.strip()
    for pattern in JUNK_PATTERNS:
        body = pattern.sub("", body).strip()
    cleaned: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if re.fullmatch(r"\d+", s):
            continue
        if s in {"Open", "Close", "To the top"}:
            continue
        cleaned.append(line)
    body = "\n".join(cleaned)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body


def iter_sections(body: str) -> list[tuple[str, str]]:
    parts = re.split(r"^(#{1,4}\s+.+)$", body, flags=re.M)
    sections: list[tuple[str, str]] = []
    if parts and parts[0].strip():
        sections.append(("", parts[0].strip()))
    i = 1
    while i < len(parts) - 1:
        heading = re.sub(r"^#{1,4}\s+", "", parts[i]).strip()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        sections.append((heading, content))
        i += 2
    return sections


def extract_summary(body: str, max_chars: int = 1200) -> str:
    paragraphs: list[str] = []
    for _heading, content in iter_sections(body):
        for block in re.split(r"\n\s*\n", content):
            block = block.strip()
            if not block or block.startswith("#") or block.startswith("!["):
                continue
            if is_link_only(block):
                continue
            line = normalize_text(block)
            if len(line) < 40:
                continue
            if "Take 5 minutes to tell us" in line:
                continue
            paragraphs.append(line)
            if sum(len(p) for p in paragraphs) >= max_chars:
                break
        if sum(len(p) for p in paragraphs) >= max_chars:
            break
    if not paragraphs:
        for block in re.split(r"\n\s*\n", body):
            line = normalize_text(block.strip())
            if len(line) >= 40 and not is_link_only(line):
                paragraphs.append(line)
                break
    if not paragraphs:
        return ""
    summary = " ".join(paragraphs)
    if len(summary) <= max_chars:
        return summary
    cut = summary[:max_chars].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "."


def extract_key_points(body: str, limit: int = 12) -> list[str]:
    seen: set[str] = set()
    points: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith(("- ", "* ", "• ")):
            continue
        item = normalize_text(line[2:].strip())
        if not (25 < len(item) < 500):
            continue
        if is_link_only(item):
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        points.append(item)
        if len(points) >= limit:
            break
    if len(points) < limit:
        for _heading, content in iter_sections(body):
            for block in re.split(r"\n\s*\n", content):
                line = normalize_text(block.strip())
                if len(line) < 60 or is_link_only(line):
                    continue
                key = line.lower()
                if key in seen:
                    continue
                seen.add(key)
                points.append(line)
                if len(points) >= limit:
                    return points
    return points


def extract_numbered_steps(body: str, limit: int = 10) -> list[str]:
    steps: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^(\d+)[.)]\s+(.+)$", line.strip())
        if not m:
            continue
        step = normalize_text(m.group(2))
        if len(step) > 20 and not is_link_only(step):
            steps.append(step)
        if len(steps) >= limit:
            break
    return steps


def extract_practical_guidance(body: str, limit: int = 12) -> list[str]:
    guidance: list[str] = []
    seen: set[str] = set()

    def add(item: str) -> None:
        item = normalize_text(item)
        if len(item) < 30 or is_link_only(item):
            return
        key = item.lower()
        if key in seen:
            return
        seen.add(key)
        guidance.append(item)

    for step in extract_numbered_steps(body, limit=limit):
        add(step)

    for heading, content in iter_sections(body):
        h = heading.lower().strip()
        if h in SKIP_HEADINGS:
            continue
        if heading and len(heading) > 8:
            for line in content.splitlines():
                line = line.strip()
                if line.startswith(("- ", "* ", "• ")):
                    add(line[2:].strip())
            for block in re.split(r"\n\s*\n", content):
                block = block.strip()
                if block.startswith(("- ", "* ")):
                    continue
                if len(block) > 50 and not block.startswith("#"):
                    add(block)
        if len(guidance) >= limit:
            break

    return guidance[:limit]


def is_curated_page(content: str) -> bool:
    """True when an existing wiki page has substantive curated content."""
    if "Full step-by-step guidance:" in content:
        return False
    summary_m = re.search(r"## Summary\n\n(.*?)\n\n## Key Information", content, re.S)
    if not summary_m:
        return False
    summary = summary_m.group(1).strip()
    if not summary or summary == "_See source material._" or summary.endswith("…"):
        return False
    if "Take 5 minutes to tell us" in summary:
        return False
    practical_m = re.search(
        r"## Practical Guidance\n\n(.*?)\n\n## Related Concepts", content, re.S
    )
    if not practical_m:
        return False
    practical = practical_m.group(1).strip()
    substantive = [
        ln.strip()
        for ln in practical.splitlines()
        if (
            (ln.startswith("- ") or re.match(r"^\d+\.\s+", ln.strip()))
            and "Refer to source" not in ln
            and "Full step-by-step guidance:" not in ln
            and not ln.strip().startswith("- Source:")
            and len(ln.strip()) > 40
        )
    ]
    return len(substantive) >= 2


def classify_category(page: RawPage) -> str:
    if page.source_key == "cpr":
        return "regulations"
    title = page.wiki_title.lower()
    if title.startswith("what is") or title.startswith("what are") or title.startswith("who is"):
        return "concepts"
    if any(
        token in title
        for token in (
            "how to",
            "appeal",
            "apply",
            "challenge",
            "check if",
            "getting",
            "making a",
            "dealing with",
            "if you",
            "when you",
            "before you",
            "after you",
        )
    ):
        return "procedures"
    if page.resolved_topic.lower() in {"courts", "going to court", "law and courts"}:
        return "courts"
    return "procedures"


def wiki_filename(title: str) -> str:
    safe = INVALID_CHARS.sub("", title).strip()
    safe = MULTI_SPACE.sub(" ", safe)
    return f"{safe}.md"


def load_pages(source_key: str) -> list[RawPage]:
    source_dir = RAW_DIR / source_key
    source_name = SOURCE_ORGS[source_key]
    index_path = source_dir / "index.json"
    entries: list[dict] = []
    indexed_files: set[str] = set()

    if index_path.exists():
        data = json.loads(index_path.read_text(encoding="utf-8"))
        entries = data.get("pages", [])
        indexed_files = {e.get("file") or e.get("filename", "") for e in entries}

    for path in sorted(source_dir.glob("*.md")):
        if path.name not in indexed_files:
            entries.append({"file": path.name})

    pages: list[RawPage] = []
    for entry in entries:
        filename = entry.get("file") or entry.get("filename", "")
        if not filename:
            continue
        path = source_dir / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_frontmatter(text)
        pages.append(
            RawPage(
                source_key=source_key,
                source_name=meta.get("source") or source_name,
                filename=filename,
                title=meta.get("title") or entry.get("title") or slug_to_title(path.stem),
                url=meta.get("url") or entry.get("url", ""),
                topic=meta.get("topic") or entry.get("topic", ""),
                section=meta.get("section") or entry.get("section", ""),
                body=clean_body(body),
                raw_path=f"raw/{source_key}/{filename}",
            )
        )
    return pages


def parse_existing_sources(content: str) -> set[str]:
    existing: set[str] = set()
    for line in content.splitlines():
        if line.startswith("- **") and "** —" in line:
            name = line.split("**")[1]
            existing.add(name)
    return existing


def source_line(page: RawPage) -> str:
    url = page.url or "#"
    return (
        f"- **{page.source_name}** — [{page.title}]({url}) — "
        f"`{page.raw_path}`"
    )


def build_page_content(
    page: RawPage,
    category: str,
    related_topics: list[str],
    related_orgs: list[str],
    existing_content: str | None,
) -> str:
    summary = extract_summary(page.body)
    key_points = extract_key_points(page.body)
    practical = extract_practical_guidance(page.body)

    if not key_points and summary:
        sentences = re.split(r"(?<=[.!?])\s+", summary)
        key_points = [s.strip() for s in sentences[:4] if len(s.strip()) > 30]

    lines = [
        f"# {page.wiki_title}",
        "",
        "## Summary",
        "",
        summary or "_See source material._",
        "",
        "## Key Information",
        "",
    ]
    if key_points:
        for point in key_points:
            lines.append(f"- {point}")
    else:
        lines.append("- See source document for details.")

    lines.extend(["", "## Practical Guidance", ""])
    if practical:
        for idx, item in enumerate(practical, 1):
            lines.append(f"{idx}. {item}")
    elif key_points:
        for point in key_points[:5]:
            lines.append(f"- {point}")
    elif summary:
        lines.append(f"- {summary}")

    if page.url:
        lines.append(f"- Source: [{page.title}]({page.url})")

    lines.extend(["", "## Related Concepts", ""])
    added_related = False
    topic = page.resolved_topic
    if topic and topic != page.wiki_title:
        lines.append(f"- [[{topic}]]")
        added_related = True
    for rel in related_topics:
        if rel != page.wiki_title:
            lines.append(f"- [[{rel}]]")
            added_related = True
    if not added_related:
        lines.append("- _None listed yet._")

    lines.extend(["", "## Related Organisations", ""])
    for org in related_orgs:
        lines.append(f"- [[{org}]]")
    if not related_orgs:
        lines.append(f"- [[{page.source_name}]]")

    lines.extend(["", "## Sources", ""])
    source_lines: list[str] = []
    if existing_content:
        source_lines = [
            ln
            for ln in existing_content.splitlines()
            if ln.startswith("- **") and "`raw/" in ln
        ]
    current = source_line(page)
    if not any(page.raw_path in ln for ln in source_lines):
        source_lines.append(current)
    lines.extend(source_lines or [current])
    lines.append("")
    return "\n".join(lines)


def build_topic_page(topic: str, pages: list[RawPage], org_names: set[str]) -> str:
    lines = [
        f"# {topic}",
        "",
        "## Summary",
        "",
        f"Topic hub for **{topic}** — {len(pages)} wiki pages extracted from approved sources.",
        "",
        "## Key Information",
        "",
    ]
    for page in sorted(pages, key=lambda p: p.wiki_title.lower()):
        snippet = extract_summary(page.body, max_chars=120)
        link = f"[[{page.wiki_title}]]"
        if snippet:
            lines.append(f"- {link} — {snippet}")
        else:
            lines.append(f"- {link}")

    org_list = ", ".join(f"[[{o}]]" for o in sorted(org_names))
    lines.extend(
        [
            "",
            "## Practical Guidance",
            "",
            f"- Browse the wiki pages above for guidance on **{topic}**.",
            "- Answers should cite the source URL and raw file path from each page's Sources section.",
            "",
            "## Related Concepts",
            "",
            f"- [[{topic}]]",
            "",
            "## Related Organisations",
            "",
        ]
    )
    for org in sorted(org_names):
        lines.append(f"- [[{org}]]")

    lines.extend(["", "## Sources", ""])
    seen: set[str] = set()
    for page in pages:
        if page.raw_path in seen:
            continue
        seen.add(page.raw_path)
        lines.append(source_line(page))
    lines.append("")
    return "\n".join(lines)


def build_organisation_page(org_name: str, source_key: str, pages: list[RawPage]) -> str:
    lines = [
        f"# {org_name}",
        "",
        "## Summary",
        "",
        f"**{org_name}** is an approved public source for Legal Shaman wiki content.",
        "",
        "## Key Information",
        "",
        f"- Source folder: `raw/{source_key}/`",
        f"- Pages ingested: {len(pages)}",
        "",
        "## Practical Guidance",
        "",
        f"- Use [[{org_name}]]-sourced wiki pages for first-point-of-contact signposting.",
        "- Always cite the underlying source URL in answers.",
        "",
        "## Related Concepts",
        "",
    ]
    topics = sorted({p.resolved_topic for p in pages if p.resolved_topic})[:20]
    for topic in topics:
        lines.append(f"- [[{topic}]]")
    lines.extend(["", "## Related Organisations", "", f"- [[{org_name}]]", "", "## Sources", ""])
    for page in pages[:15]:
        lines.append(source_line(page))
    if len(pages) > 15:
        lines.append(f"- _Plus {len(pages) - 15} more pages from this source._")
    lines.append("")
    return "\n".join(lines)


def update_category_index(category: str, titles: set[str]) -> None:
    index_path = WIKI_DIR / category / "_index.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    title = category.replace("-", " ").title()
    lines = [f"# {title}", "", f"Pages in `wiki/{category}/`.", "",]
    for name in sorted(titles, key=str.lower):
        lines.append(f"- [[{Path(name).stem}]]")
    lines.append("")
    index_path.write_text("\n".join(lines), encoding="utf-8")


def ingest_content_pages(
    pages: list[RawPage], stats: IngestStats, preserve_curated: bool = True
) -> dict[str, set[str]]:
    by_category: dict[str, set[str]] = defaultdict(set)
    for page in pages:
        category = classify_category(page)
        dest = WIKI_DIR / category / wiki_filename(page.wiki_title)
        dest.parent.mkdir(parents=True, exist_ok=True)
        existing = dest.read_text(encoding="utf-8") if dest.exists() else None
        if preserve_curated and existing and is_curated_page(existing):
            stats.skipped_curated += 1
            by_category[category].add(dest.name)
            continue
        content = build_page_content(
            page, category, [], [page.source_name], existing
        )
        if existing is None:
            stats.created += 1
        else:
            stats.updated += 1
        dest.write_text(content, encoding="utf-8")
        by_category[category].add(dest.name)
    return by_category


def rebuild_all_topic_hubs() -> int:
    by_topic: dict[str, list[RawPage]] = defaultdict(list)
    for source_key in SOURCE_ORDER:
        for page in load_pages(source_key):
            topic = page.resolved_topic
            if topic and topic != "General":
                by_topic[topic].append(page)

    topics_dir = WIKI_DIR / "topics"
    topics_dir.mkdir(parents=True, exist_ok=True)
    for topic, topic_pages in sorted(by_topic.items()):
        org_names = {p.source_name for p in topic_pages}
        path = topics_dir / wiki_filename(topic)
        path.write_text(build_topic_page(topic, topic_pages, org_names), encoding="utf-8")
    return len(by_topic)


def rebuild_all_category_indexes() -> None:
    for category in CONTENT_CATEGORIES + ("topics", "organisations", "courts"):
        category_dir = WIKI_DIR / category
        if not category_dir.exists():
            continue
        titles = sorted(
            p.stem for p in category_dir.glob("*.md") if p.name != "_index.md"
        )
        title = category.replace("-", " ").title()
        lines = [f"# {title}", "", f"Pages in `wiki/{category}/`.", ""]
        for name in titles:
            lines.append(f"- [[{name}]]")
        lines.append("")
        (category_dir / "_index.md").write_text("\n".join(lines), encoding="utf-8")


def ingest_source(source_key: str, preserve_curated: bool = True) -> IngestStats:
    stats = IngestStats(source=source_key)
    pages = load_pages(source_key)
    org_name = SOURCE_ORGS[source_key]

    ingest_content_pages(pages, stats, preserve_curated=preserve_curated)

    by_topic: dict[str, list[RawPage]] = defaultdict(list)
    for page in pages:
        stats.processed += 1
        by_topic[page.resolved_topic].append(page)
        stats.topics.add(page.resolved_topic)

    topics_dir = WIKI_DIR / "topics"
    topics_dir.mkdir(parents=True, exist_ok=True)
    for topic, topic_pages in sorted(by_topic.items()):
        if not topic or topic == "General":
            continue
        topic_path = topics_dir / wiki_filename(topic)
        org_names = {p.source_name for p in topic_pages}
        topic_path.write_text(build_topic_page(topic, topic_pages, org_names), encoding="utf-8")

    org_path = WIKI_DIR / "organisations" / wiki_filename(org_name)
    org_path.parent.mkdir(parents=True, exist_ok=True)
    org_path.write_text(build_organisation_page(org_name, source_key, pages), encoding="utf-8")

    return stats


def ingested_sources() -> list[str]:
    found: list[str] = []
    org_dir = WIKI_DIR / "organisations"
    if not org_dir.exists():
        return found
    for key, org in SOURCE_ORGS.items():
        if (org_dir / wiki_filename(org)).exists():
            found.append(key)
    return [key for key in SOURCE_ORDER if key in found]


def count_wiki_pages() -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for category in CONTENT_CATEGORIES + ("topics", "organisations", "courts"):
        d = WIKI_DIR / category
        if d.exists():
            counts[category] = len([p for p in d.glob("*.md") if p.name != "_index.md"])
    return dict(counts)


def update_wiki_index() -> None:
    ingested = ingested_sources()
    raw_counts = {}
    for key in SOURCE_ORDER:
        d = RAW_DIR / key
        raw_counts[key] = len(list(d.glob("*.md"))) if d.exists() else 0
    wiki_counts = count_wiki_pages()
    total_content = sum(wiki_counts.get(c, 0) for c in CONTENT_CATEGORIES)

    lines = [
        "# Legal Shaman Wiki",
        "",
        "Source-grounded legal knowledge for first-point-of-contact signposting.",
        "",
        "## Status",
        "",
        f"**{total_content} content pages** extracted from {sum(raw_counts.values())} raw source files.",
        "",
        "Pages contain extracted summaries, key points, and practical guidance — not raw link mirrors.",
        "",
        "## Categories",
        "",
        "- [[Topics]] — topic hubs linking to wiki pages",
        "- [[Concepts]] — rights, obligations, definitions",
        "- [[Procedures]] — step-by-step processes",
        "- [[Organisations]] — approved source organisations",
        "- [[Funding]] — legal aid and fee routes",
        "- [[Courts]] — courts and tribunals",
        "- [[Regulations]] — rules and practice directions",
        "",
        "## Wiki page counts",
        "",
        "| Category | Pages |",
        "|----------|-------|",
    ]
    for cat in CONTENT_CATEGORIES + ("topics", "organisations", "courts"):
        lines.append(f"| {cat.title()} | {wiki_counts.get(cat, 0)} |")
    lines.extend(
        [
            "",
            "## Source coverage",
            "",
            "| Source | Raw path | Pages crawled | Wiki status |",
            "|--------|----------|---------------|-------------|",
        ]
    )
    for key in SOURCE_ORDER:
        org = SOURCE_ORGS[key]
        status = "Ingested" if key in ingested else "Pending"
        lines.append(f"| {org} | `raw/{key}/` | {raw_counts[key]} | {status} |")
    lines.extend(
        [
            "",
            "Planned sources: Law Centres, Shelter, GOV.UK, LawWorks, Advocate.",
            "",
            "## Maintenance",
            "",
            "- Ingestion log: `logs/ingestion-log.md`",
            "- Source updates: `logs/source-update-log.md`",
            "- Daily reports: `logs/daily-update-report.md`",
            "- Conflicts: `logs/conflict-report.md`",
            "- Audits: `logs/wiki-audit.md`",
            "",
        ]
    )
    (WIKI_DIR / "index.md").write_text("\n".join(lines), encoding="utf-8")


def append_ingestion_log(all_stats: list[IngestStats]) -> None:
    log_path = LOGS_DIR / "ingestion-log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text(
            "# Ingestion log\n\n"
            "Records each run that converts new or changed `/raw` files into wiki pages.\n\n"
            "| Date | Source | Raw files processed | Wiki pages created | Wiki pages updated | Topics |\n"
            "|------|--------|---------------------|--------------------|--------------------|--------|\n",
            encoding="utf-8",
        )
    today = date.today().isoformat()
    rows = []
    for stats in all_stats:
        rows.append(
            f"| {today} | {SOURCE_ORGS[stats.source]} | {stats.processed} | "
            f"{stats.created} | {stats.updated} | {len(stats.topics)} |"
            f" (skipped curated: {stats.skipped_curated})"
        )
    content = log_path.read_text(encoding="utf-8")
    if "## Entries" not in content:
        content += "\n## Entries\n\n"
    content = content.replace("_No ingestion runs recorded yet._\n", "")
    content += "\n".join(rows) + "\n"
    log_path.write_text(content, encoding="utf-8")


def main(argv: list[str]) -> int:
    if "--allow-bulk" not in argv:
        print(
            "Bulk wiki ingestion is disabled.\n"
            "The wiki must contain curated, value-add pages — not raw link mirrors.\n"
            "See AGENTS.md (Wiki value-add policy).\n"
            "To reset low-value pages: python3 purge_wiki.py",
            file=sys.stderr,
        )
        return 1

    sources = [a for a in argv if not a.startswith("--")] or list(SOURCE_ORDER)
    preserve_curated = "--overwrite-curated" not in argv
    for key in sources:
        if key not in SOURCE_ORGS:
            print(f"Unknown source: {key}", file=sys.stderr)
            return 1

    all_stats: list[IngestStats] = []
    ingested: list[str] = []
    for key in SOURCE_ORDER:
        if key not in sources:
            continue
        print(f"Ingesting {key}...")
        stats = ingest_source(key, preserve_curated=preserve_curated)
        all_stats.append(stats)
        ingested.append(key)
        print(
            f"  processed={stats.processed} created={stats.created} "
            f"updated={stats.updated} skipped_curated={stats.skipped_curated} "
            f"topics={len(stats.topics)}"
        )

    rebuild_all_topic_hubs()
    rebuild_all_category_indexes()
    update_wiki_index()
    append_ingestion_log(all_stats)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
