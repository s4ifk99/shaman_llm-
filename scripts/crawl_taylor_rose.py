#!/usr/bin/env python3
"""Crawl Taylor Rose Insights articles and save as Markdown."""

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

BASE_URL = "https://www.taylor-rose.co.uk"
START_URL = f"{BASE_URL}/insights"
OUTPUT_DIR = RAW_DIR / "taylor-rose"
LOG_PATH = LOGS_DIR / "taylor-rose-crawl-report.md"
INDEX_PATH = OUTPUT_DIR / "index.json"

USER_AGENT = "LegalShamanObsidianCrawler/1.0 (+local research; respects robots.txt)"
REQUEST_DELAY_SEC = 1.5
REQUEST_TIMEOUT_SEC = 30
MAX_RETRIES = 6
RETRY_BASE_DELAY_SEC = 10.0

SECTION_LABEL = "Insights"
POST_PREFIX = "/posts/"

SKIP_TITLE_PATTERNS = ("page not found", "404")

TOPIC_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("family", "Family Law"),
    ("divorce", "Family Law"),
    ("cohabitation", "Family Law"),
    ("prenuptial", "Family Law"),
    ("child maintenance", "Family Law"),
    ("probate", "Private Client"),
    ("will", "Private Client"),
    ("trust", "Private Client"),
    ("estate", "Private Client"),
    ("inheritance", "Private Client"),
    ("conveyancing", "Property"),
    ("lease", "Property"),
    ("leasehold", "Property"),
    ("buyer", "Property"),
    ("property", "Property"),
    ("employment", "Employment Law"),
    ("immigration", "Immigration"),
    ("personal injury", "Personal Injury"),
    ("medical negligence", "Medical Negligence"),
    ("commercial", "Commercial Litigation"),
    ("litigation", "Commercial Litigation"),
    ("corporate", "Corporate"),
    ("construction", "Construction law"),
    ("insolvency", "Business law"),
    ("crime", "Business crime and regulatory"),
)


@dataclass
class PageRecord:
    url: str
    canonical_url: str
    title: str
    section: str
    topic: str
    author: str
    date_published: str
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
    host = parsed.netloc.lower()
    if host and host not in ("taylor-rose.co.uk", "www.taylor-rose.co.uk"):
        return None

    path = parsed.path or "/"
    path = re.sub(r"/+", "/", path)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    # Percent-encode non-ASCII path characters for reliable fetching
    if any(ord(ch) > 127 for ch in path):
        parts = path.split("/")
        path = "/".join(urllib.parse.quote(urllib.parse.unquote(p), safe="") for p in parts)
    return urllib.parse.urlunparse(("https", "www.taylor-rose.co.uk", path, "", "", ""))


def is_post_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path
    return path.startswith(POST_PREFIX) and path != POST_PREFIX.rstrip("/")


def discover_from_insights_page(html: str, base_url: str) -> set[str]:
    parser = LinkExtractor()
    parser.feed(html)
    found: set[str] = set()
    for href in parser.links:
        normalized = normalize_url(href, base=base_url)
        if normalized and is_post_url(normalized):
            found.add(normalized)
    return found


def total_insights_pages(html: str) -> int:
    match = re.search(r'total_pages(?:&quot;|"):\s*(\d+)', html)
    if match:
        return int(match.group(1))
    return 1


def discover_insights_posts() -> list[str]:
    urls: set[str] = set()
    _, final_url, first_html = fetch_url(START_URL)
    urls |= discover_from_insights_page(first_html, final_url)
    pages = total_insights_pages(first_html)
    time.sleep(REQUEST_DELAY_SEC)

    for page in range(2, pages + 1):
        page_url = f"{START_URL}?pg={page}"
        print(f"Discovering page {page}/{pages}...")
        _, final, html = fetch_url(page_url)
        urls |= discover_from_insights_page(html, final)
        time.sleep(REQUEST_DELAY_SEC)

    return sorted(urls)


def parse_json_ld_blocks(html: str) -> list[dict]:
    blocks: list[dict] = []
    for match in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            blocks.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            blocks.append(data)
    return blocks


def json_ld_type(data: dict) -> str:
    value = data.get("@type", "")
    if isinstance(value, list):
        return value[0] if value else ""
    return str(value)


def extract_article_ld(html: str) -> dict | None:
    for block in parse_json_ld_blocks(html):
        if json_ld_type(block) == "Article":
            return block
    return None


def extract_canonical(html: str) -> str:
    match = re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', html, re.IGNORECASE)
    if not match:
        match = re.search(r'<link[^>]+href="([^"]+)"[^>]+rel="canonical"', html, re.IGNORECASE)
    return unescape(match.group(1)).strip() if match else ""


def extract_title(html: str, article_ld: dict | None = None) -> str:
    if article_ld and article_ld.get("headline"):
        return unescape(str(article_ld["headline"])).strip()
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    if match:
        return unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
    return ""


def extract_author(article_ld: dict | None) -> str:
    if not article_ld:
        return ""
    author = article_ld.get("author")
    if isinstance(author, dict) and author.get("name"):
        return unescape(str(author["name"])).strip()
    if isinstance(author, str):
        return unescape(author).strip()
    return ""


def extract_author_job(article_ld: dict | None) -> str:
    if not article_ld:
        return ""
    author = article_ld.get("author")
    if isinstance(author, dict) and author.get("jobTitle"):
        return unescape(str(author["jobTitle"])).strip()
    return ""


def format_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    if match:
        return match.group(1)
    return value


def extract_dates(article_ld: dict | None) -> tuple[str, str]:
    if not article_ld:
        return "", ""
    published = format_date(str(article_ld.get("datePublished", "")))
    updated = format_date(str(article_ld.get("dateModified", "")))
    return published, updated


def article_section(article_ld: dict | None) -> str:
    if not article_ld:
        return ""
    return unescape(str(article_ld.get("articleSection", ""))).strip()


def infer_topic(title: str, slug: str, author_job: str) -> str:
    haystack = f"{title} {slug} {author_job}".lower()
    for keyword, topic in TOPIC_KEYWORDS:
        if keyword in haystack:
            return topic
    if author_job:
        for keyword, topic in TOPIC_KEYWORDS:
            if keyword in author_job.lower():
                return topic
    return "Insights"


def extract_article_html(html: str) -> str:
    start = html.find('id="sec_0"')
    end = html.find('id="sec_author"')
    if start == -1:
        match = re.search(r'<div class="cms-block">', html)
        if not match:
            return ""
        start = match.start()
    if end == -1:
        end = len(html)
    chunk = html[start:end]
    match = re.search(r'<div class="cms-block">(.*)', chunk, re.IGNORECASE | re.DOTALL)
    if not match:
        return chunk
    body = match.group(1)
    body = re.sub(r"</div>\s*</div>\s*$", "", body.strip(), flags=re.DOTALL)
    body = re.sub(r"<script[^>]*>.*?</script>", "", body, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<style[^>]*>.*?</style>", "", body, flags=re.IGNORECASE | re.DOTALL)
    return body.strip()


def is_error_page(title: str) -> bool:
    lowered = title.lower()
    return any(pattern in lowered for pattern in SKIP_TITLE_PATTERNS)


def clean_markdown(md: str) -> str:
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    cut_markers = (
        "Cookies at taylor-rose.co.uk",
        "We use cookies to provide",
        "Essential Cookies",
        "Your Name",
        "Send Enquiry",
        "Get Practical Legal Updates",
        "Manage cookies",
    )
    for marker in cut_markers:
        pos = md.find(marker)
        if pos > 0:
            md = md[:pos].rstrip()
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


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
        return clean_markdown(unescape(re.sub(r"\n{3,}", "\n\n", text)))
    return clean_markdown(proc.stdout.strip())


def slugify(value: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:max_len].strip("-") or "page"


def make_filename(topic: str, title: str, canonical_url: str, used: set[str]) -> str:
    topic_slug = slugify(topic) if topic else "insights"
    title_slug = slugify(title)
    if not title_slug:
        path = urllib.parse.urlparse(canonical_url).path.strip("/")
        title_slug = slugify(path.replace("posts/", ""))
    base = f"{topic_slug}-{title_slug}"
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
    section: str,
    topic: str,
    author: str,
    date_published: str,
    last_updated: str,
    crawl_date: str,
    body: str,
) -> str:
    frontmatter = "\n".join(
        [
            "---",
            "source: Taylor Rose",
            f"url: {yaml_escape(url)}",
            f"title: {yaml_escape(title)}",
            f"section: {yaml_escape(section)}",
            f"topic: {yaml_escape(topic)}",
            f"author: {yaml_escape(author)}",
            f"date_published: {yaml_escape(date_published)}",
            f"last_updated: {yaml_escape(last_updated)}",
            f"crawl_date: {yaml_escape(crawl_date)}",
            "---",
        ]
    )
    return f"{frontmatter}\n\n# {title}\n\n{body}\n"


def write_report(stats: CrawlStats, started_at: datetime, finished_at: datetime) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    duration = (finished_at - started_at).total_seconds()
    lines = [
        "# Taylor Rose crawl report",
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
    LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def crawl() -> CrawlStats:
    started_at = datetime.now(timezone.utc)
    crawl_date = date.today().isoformat()
    stats = CrawlStats()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Discovering insights posts...")
    urls = discover_insights_posts()
    stats.discovered = len(urls)
    print(f"Discovered {len(urls)} insights posts")

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
                continue

            article_ld = extract_article_ld(html)
            section = article_section(article_ld)
            if section and section.lower() != "insights":
                stats.skipped += 1
                stats.pages.append(
                    PageRecord(
                        url=final_url,
                        canonical_url=canonical,
                        title="",
                        section=section,
                        topic="",
                        author="",
                        date_published="",
                        last_updated="",
                        crawl_date=crawl_date,
                        file="",
                        status="skipped",
                        error=f"articleSection={section}",
                    )
                )
                continue

            title = extract_title(html, article_ld)
            author = extract_author(article_ld)
            author_job = extract_author_job(article_ld)
            date_published, last_updated = extract_dates(article_ld)
            slug = urllib.parse.urlparse(canonical).path.rsplit("/", 1)[-1]
            topic = infer_topic(title, slug, author_job)
            article_html = extract_article_html(html)
            markdown_body = html_to_markdown(article_html)

            if status_code >= 400 or is_error_page(title):
                stats.skipped += 1
                continue

            if not markdown_body or len(markdown_body) < 100:
                stats.skipped += 1
                stats.pages.append(
                    PageRecord(
                        url=final_url,
                        canonical_url=canonical,
                        title=title,
                        section=SECTION_LABEL,
                        topic=topic,
                        author=author,
                        date_published=date_published,
                        last_updated=last_updated,
                        crawl_date=crawl_date,
                        file="",
                        status="skipped",
                        error="no extractable content",
                    )
                )
                continue

            filename = make_filename(topic, title, canonical, used_filenames)
            out_file = OUTPUT_DIR / filename
            content = build_markdown(
                title=title,
                url=canonical,
                section=SECTION_LABEL,
                topic=topic,
                author=author,
                date_published=date_published,
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
                    section=SECTION_LABEL,
                    topic=topic,
                    author=author,
                    date_published=date_published,
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
                    "source": "Taylor Rose",
                    "section": SECTION_LABEL,
                    "topic": topic,
                    "author": author,
                    "date_published": date_published,
                    "last_updated": last_updated,
                    "crawl_date": crawl_date,
                    "file": filename,
                }
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"{url}: {exc}"
            stats.errors.append(msg)
            stats.pages.append(
                PageRecord(
                    url=url,
                    canonical_url="",
                    title="",
                    section=SECTION_LABEL,
                    topic="",
                    author="",
                    date_published="",
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
                "source": "Taylor Rose",
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
