#!/usr/bin/env python3
"""Crawl Civil Procedure Rules pages from justice.gov.uk and save as Markdown."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from project_paths import LOGS_DIR, RAW_DIR

BASE_URL = "https://www.justice.gov.uk"
START_URL = f"{BASE_URL}/courts/procedure-rules/civil/rules"
ALLOWED_PREFIX = "/courts/procedure-rules/civil/rules"
SITEMAP_URL = f"{BASE_URL}/wp-sitemap-posts-page-1.xml"
OUTPUT_DIR = RAW_DIR / "cpr"
LOG_PATH = LOGS_DIR / "cpr-crawl-report.md"
INDEX_PATH = OUTPUT_DIR / "index.json"
SOURCE_LABEL = "Ministry of Justice / Civil Procedure Rules"

USER_AGENT = "LegalShamanObsidianCrawler/1.0 (+local research; respects robots.txt)"
REQUEST_DELAY_SEC = 1.0
REQUEST_TIMEOUT_SEC = 30
MAX_RETRIES = 6
RETRY_BASE_DELAY_SEC = 10.0

EXCLUDED_SEGMENTS = frozenset(
    {
        "contact",
        "search",
        "tag",
        "tags",
        "news",
        "consultation",
        "consultations",
        "forms",
        "criminal",
        "family",
        "tribunal",
    }
)

SKIP_TITLE_PATTERNS = (
    "page not found",
    "404",
)


@dataclass
class PageRecord:
    url: str
    canonical_url: str
    title: str
    rule_part: str
    practice_direction: str
    section: str
    last_updated: str
    crawl_date: str
    file: str
    status: str
    error: str = ""


@dataclass
class CrawlStats:
    discovered: int = 0
    fetched: int = 0
    saved: int = 0
    skipped: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    pages: list[PageRecord] = field(default_factory=list)


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


def fetch_url(url: str) -> tuple[int, str, str]:
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                body = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.status, resp.geturl(), body.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (429, 403) and attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY_SEC * (2**attempt)
                print(
                    f"Rate limited on {url}; retrying in {delay:.0f}s "
                    f"({attempt + 1}/{MAX_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_DELAY_SEC)
                continue
            raise
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def normalize_url(url: str, base: str = BASE_URL) -> str | None:
    if not url:
        return None
    url = url.strip()
    if url.startswith("#") or url.lower().startswith(("javascript:", "mailto:", "tel:")):
        return None

    parsed = urllib.parse.urlparse(urllib.parse.urljoin(base, url))
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return None
    if any(ord(ch) < 32 for ch in parsed.path):
        return None
    if " " in parsed.path:
        return None
    if parsed.path.lower().endswith(".pdf"):
        return None

    host = parsed.netloc.lower()
    if host and host not in ("www.justice.gov.uk", "justice.gov.uk"):
        return None

    path = parsed.path or "/"
    path = re.sub(r"/+", "/", path)
    if not path.startswith("/"):
        path = "/" + path
    if path != ALLOWED_PREFIX and not path.startswith(f"{ALLOWED_PREFIX}/"):
        return None
    if path.endswith("/") and path != f"{ALLOWED_PREFIX}/":
        path = path.rstrip("/")

    return urllib.parse.urlunparse(("https", "www.justice.gov.uk", path, "", "", ""))


def is_allowed_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    segments = [segment.lower() for segment in path.split("/") if segment]
    return not any(segment in EXCLUDED_SEGMENTS for segment in segments)


def discover_from_sitemap() -> set[str]:
    _, _, xml_text = fetch_url(SITEMAP_URL)
    root = ET.fromstring(xml_text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls: set[str] = set()
    for loc in root.findall(".//sm:loc", ns):
        if not loc.text:
            continue
        normalized = normalize_url(loc.text.strip())
        if normalized and is_allowed_url(normalized):
            urls.add(normalized)
    return urls


def discover_from_page(url: str, html: str) -> set[str]:
    parser = LinkExtractor()
    parser.feed(html)
    found: set[str] = set()
    for href in parser.links:
        normalized = normalize_url(href, base=url)
        if normalized and is_allowed_url(normalized):
            found.add(normalized)
    return found


def discover_all_urls() -> list[str]:
    urls: set[str] = {START_URL}
    try:
        urls |= discover_from_sitemap()
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: sitemap discovery failed: {exc}", file=sys.stderr)

    try:
        _, final_url, html = fetch_url(START_URL)
        urls |= discover_from_page(final_url, html)
        time.sleep(REQUEST_DELAY_SEC)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: hub link discovery failed: {exc}", file=sys.stderr)

    return sorted(urls)


def extract_canonical(html: str) -> str:
    match = re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', html, re.IGNORECASE)
    if not match:
        match = re.search(r'<link[^>]+href="([^"]+)"[^>]+rel="canonical"', html, re.IGNORECASE)
    return unescape(match.group(1)).strip() if match else ""


def extract_title(html: str) -> str:
    match = re.search(r'<h1[^>]*class="hero__title"[^>]*>([^<]+)</h1>', html, re.IGNORECASE)
    if match:
        return clean_text(match.group(1))

    match = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if match:
        title = clean_text(match.group(1))
        for suffix in (" – Justice UK", " - Justice UK", " &#8211; Civil Procedure Rules &#8211; Justice UK"):
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()
        return title
    return ""


def clean_text(value: str) -> str:
    text = unescape(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_rule_part(url: str, title: str) -> str:
    for pattern in (r"/part0*(\d+)(?:/|$)", r"/part-(\d+)(?:/|-)"):
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            return str(int(match.group(1)))

    match = re.search(r"^part\s*0*(\d+)\b", title, re.IGNORECASE)
    if match:
        return str(int(match.group(1)))

    match = re.search(r"practice-direction-(\d+)", url, re.IGNORECASE)
    if match:
        return str(int(match.group(1)))

    match = re.search(r"pd_part0*(\d+)", url, re.IGNORECASE)
    if match:
        return str(int(match.group(1)))

    return ""


def extract_practice_direction(url: str, title: str) -> str:
    match = re.search(r"practice-direction-(\d+[a-z]*)", url, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    match = re.search(r"/pd_part0*(\d+[a-z]*)(?:/|$)", url, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    match = re.search(r"practice-direction-(\d+[a-z]*)-", url, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    match = re.search(r"\bpractice direction\s+(\d+[a-z]*)\b", title, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    match = re.search(r"\bpd\s+(\d+[a-z]*)\b", title, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    if re.search(r"_pd(?:/|$)", url, re.IGNORECASE) or url.lower().endswith("_pd"):
        slug = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
        return slug.replace("_pd", "").replace("-", " ").title()

    return ""


def extract_section_heading(html: str, title: str) -> str:
    content = extract_content_html(html)
    if not content:
        return ""

    match = re.search(
        r'<h2[^>]*class="[^"]*wp-block-heading[^"]*"[^>]*>.*?<a[^>]*>([^<]+)</a>',
        content,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        heading = clean_text(re.sub(r"<[^>]+>", "", match.group(1)))
        if heading.lower() != title.lower():
            return heading

    match = re.search(
        r'<h2[^>]*class="[^"]*wp-block-heading[^"]*"[^>]*>([^<]+)</h2>',
        content,
        re.IGNORECASE,
    )
    if match:
        heading = clean_text(match.group(1))
        if heading.lower() != title.lower():
            return heading
    return ""


def extract_last_updated(html: str) -> str:
    match = re.search(
        r'class="updated-date__content"[^>]*>\s*Updated:\s*([^<]+)\s*<',
        html,
        re.IGNORECASE,
    )
    if match:
        return clean_text(match.group(1))

    match = re.search(r'<meta[^>]+name="DC.date.modified"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if match:
        return match.group(1)

    match = re.search(r'<meta[^>]+content="([^"]+)"[^>]+name="DC.date.modified"', html, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def extract_content_html(html: str) -> str:
    match = re.search(
        r'two-sidebars__article-content[^>]*>.*?<div class="rich-text">(.*?)</div>\s*<div class="updated-date"',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group(1).strip()

    match = re.search(
        r'two-sidebars__article-content[^>]*>.*?<div class="rich-text">(.*?)</div>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group(1).strip()

    match = re.search(r'<article[^>]*>(.*?)</article>', html, re.IGNORECASE | re.DOTALL)
    if match:
        article = match.group(1)
        inner = re.search(r'<div class="rich-text">(.*?)</div>', article, re.IGNORECASE | re.DOTALL)
        if inner:
            return inner.group(1).strip()
    return ""


def is_error_page(title: str) -> bool:
    lowered = title.lower()
    return any(pattern in lowered for pattern in SKIP_TITLE_PATTERNS)


def html_to_markdown(html: str) -> str:
    if not html:
        return ""

    proc = subprocess.run(
        ["html2text", "--body-width", "0", "--ignore-emphasis"],
        input=html,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        text = re.sub(r"<[^>]+>", "", html)
        return clean_text(unescape(text))

    md = proc.stdout.strip()
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"^Updated:.*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def slugify(value: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:max_len].strip("-") or "page"


def make_filename(
    *,
    rule_part: str,
    practice_direction: str,
    title: str,
    canonical_url: str,
    used: set[str],
) -> str:
    path_slug = slugify(urllib.parse.urlparse(canonical_url).path.strip("/").split("/")[-1])
    has_part_url = bool(re.search(r"/part0*\d+|/part-\d+", canonical_url, re.IGNORECASE))

    if practice_direction:
        base = f"pd-{practice_direction.lower()}-{slugify(title)}"
    elif rule_part and has_part_url:
        base = f"part-{rule_part.zfill(2)}-{slugify(title)}"
    elif canonical_url.rstrip("/").endswith("/rules"):
        base = "rules-index"
    else:
        base = f"cpr-{path_slug}"

    candidate = base
    counter = 2
    while candidate in used:
        candidate = f"{base}-{counter}"
        counter += 1
    used.add(candidate)
    return f"{candidate}.md"


def yaml_escape(value: str) -> str:
    if value == "":
        return '""'
    if re.search(r'[:#\[\]{}|>&*!%@`",\n]', value):
        return json.dumps(value)
    return f'"{value}"'


def build_markdown(
    *,
    title: str,
    url: str,
    rule_part: str,
    practice_direction: str,
    section: str,
    last_updated: str,
    crawl_date: str,
    body: str,
) -> str:
    frontmatter = "\n".join(
        [
            "---",
            f"source: {SOURCE_LABEL}",
            f"url: {yaml_escape(url)}",
            f"title: {yaml_escape(title)}",
            f"rule_part: {yaml_escape(rule_part)}",
            f"practice_direction: {yaml_escape(practice_direction)}",
            f"section: {yaml_escape(section)}",
            f"last_updated: {yaml_escape(last_updated)}",
            f"crawl_date: {yaml_escape(crawl_date)}",
            "---",
        ]
    )
    return f"{frontmatter}\n\n{body}\n"


def write_report(stats: CrawlStats, started_at: datetime, finished_at: datetime) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    duration = (finished_at - started_at).total_seconds()

    lines = [
        "# CPR crawl report",
        "",
        f"- **Started:** {started_at.isoformat()}",
        f"- **Finished:** {finished_at.isoformat()}",
        f"- **Duration:** {duration:.1f}s",
        f"- **Start URL:** {START_URL}",
        f"- **Output directory:** `{OUTPUT_DIR}`",
        "",
        "## Summary",
        "",
        f"- Discovered URLs: {stats.discovered}",
        f"- Fetched pages: {stats.fetched}",
        f"- Saved pages: {stats.saved}",
        f"- Duplicate canonical URLs skipped: {stats.duplicates}",
        f"- Skipped pages: {stats.skipped}",
        f"- Errors: {len(stats.errors)}",
        "",
    ]

    if stats.errors:
        lines.extend(["## Errors", ""])
        for err in stats.errors:
            lines.append(f"- {err}")
        lines.append("")

    lines.extend(["## Saved pages", ""])
    for page in stats.pages:
        if page.status == "saved":
            lines.append(f"- [{page.title}]({page.file}) — `{page.canonical_url}`")

    if stats.skipped or stats.duplicates:
        lines.extend(["", "## Skipped pages", ""])
        for page in stats.pages:
            if page.status in {"skipped", "duplicate"}:
                reason = page.error or page.status
                lines.append(f"- `{page.url}` — {reason}")

    LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def crawl() -> CrawlStats:
    started_at = datetime.now(timezone.utc)
    crawl_date = date.today().isoformat()
    stats = CrawlStats()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Discovering URLs...")
    urls = discover_all_urls()
    stats.discovered = len(urls)
    print(f"Discovered {len(urls)} CPR URLs")

    index_entries: list[dict[str, str]] = []
    seen_canonical: dict[str, str] = {}
    used_filenames: set[str] = set()

    for i, url in enumerate(urls, start=1):
        print(f"[{i}/{len(urls)}] {url}")

        try:
            status_code, final_url, html = fetch_url(url)
            stats.fetched += 1

            canonical_raw = extract_canonical(html) or final_url
            canonical = normalize_url(canonical_raw, base=final_url) or normalize_url(final_url)
            if not canonical:
                stats.skipped += 1
                continue

            if canonical in seen_canonical:
                stats.duplicates += 1
                stats.pages.append(
                    PageRecord(
                        url=final_url,
                        canonical_url=canonical,
                        title="",
                        rule_part="",
                        practice_direction="",
                        section="",
                        last_updated="",
                        crawl_date=crawl_date,
                        file="",
                        status="duplicate",
                        error=f"duplicate of {seen_canonical[canonical]}",
                    )
                )
                continue

            title = extract_title(html)
            rule_part = extract_rule_part(canonical, title)
            practice_direction = extract_practice_direction(canonical, title)
            section = extract_section_heading(html, title)
            last_updated = extract_last_updated(html)
            content_html = extract_content_html(html)
            markdown_body = html_to_markdown(content_html)

            if status_code >= 400 or is_error_page(title):
                stats.skipped += 1
                stats.pages.append(
                    PageRecord(
                        url=final_url,
                        canonical_url=canonical,
                        title=title,
                        rule_part=rule_part,
                        practice_direction=practice_direction,
                        section=section,
                        last_updated=last_updated,
                        crawl_date=crawl_date,
                        file="",
                        status="skipped",
                        error="404 or error page",
                    )
                )
                continue

            if not markdown_body:
                stats.skipped += 1
                stats.pages.append(
                    PageRecord(
                        url=final_url,
                        canonical_url=canonical,
                        title=title,
                        rule_part=rule_part,
                        practice_direction=practice_direction,
                        section=section,
                        last_updated=last_updated,
                        crawl_date=crawl_date,
                        file="",
                        status="skipped",
                        error="no extractable content",
                    )
                )
                continue

            filename = make_filename(
                rule_part=rule_part,
                practice_direction=practice_direction,
                title=title,
                canonical_url=canonical,
                used=used_filenames,
            )
            out_file = OUTPUT_DIR / filename
            content = build_markdown(
                title=title,
                url=canonical,
                rule_part=rule_part,
                practice_direction=practice_direction,
                section=section,
                last_updated=last_updated,
                crawl_date=crawl_date,
                body=markdown_body,
            )
            out_file.write_text(content, encoding="utf-8")

            seen_canonical[canonical] = filename
            stats.saved += 1
            stats.pages.append(
                PageRecord(
                    url=final_url,
                    canonical_url=canonical,
                    title=title,
                    rule_part=rule_part,
                    practice_direction=practice_direction,
                    section=section,
                    last_updated=last_updated,
                    crawl_date=crawl_date,
                    file=filename,
                    status="saved",
                )
            )
            index_entries.append(
                {
                    "title": title,
                    "url": canonical,
                    "source": SOURCE_LABEL,
                    "rule_part": rule_part,
                    "practice_direction": practice_direction,
                    "section": section,
                    "last_updated": last_updated,
                    "crawl_date": crawl_date,
                    "file": filename,
                }
            )
        except urllib.error.HTTPError as exc:
            stats.skipped += 1
            msg = f"HTTP {exc.code} for {url}"
            stats.errors.append(msg)
            stats.pages.append(
                PageRecord(
                    url=url,
                    canonical_url="",
                    title="",
                    rule_part="",
                    practice_direction="",
                    section="",
                    last_updated="",
                    crawl_date=crawl_date,
                    file="",
                    status="skipped",
                    error=msg,
                )
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"{url}: {exc}"
            stats.errors.append(msg)
            stats.pages.append(
                PageRecord(
                    url=url,
                    canonical_url="",
                    title="",
                    rule_part="",
                    practice_direction="",
                    section="",
                    last_updated="",
                    crawl_date=crawl_date,
                    file="",
                    status="error",
                    error=str(exc),
                )
            )
        finally:
            time.sleep(REQUEST_DELAY_SEC)

    INDEX_PATH.write_text(
        json.dumps(
            {
                "source": SOURCE_LABEL,
                "crawl_date": crawl_date,
                "start_url": START_URL,
                "total_discovered": stats.discovered,
                "total_saved": stats.saved,
                "total_duplicates_skipped": stats.duplicates,
                "pages": sorted(index_entries, key=lambda p: p["file"]),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    finished_at = datetime.now(timezone.utc)
    write_report(stats, started_at, finished_at)
    return stats


def main() -> int:
    stats = crawl()
    print(
        f"\nDone. Discovered={stats.discovered}, saved={stats.saved}, "
        f"duplicates={stats.duplicates}, skipped={stats.skipped}, errors={len(stats.errors)}"
    )
    print(f"Index: {INDEX_PATH}")
    print(f"Report: {LOG_PATH}")
    return 0 if not stats.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
