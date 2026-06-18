#!/usr/bin/env python3
"""Crawl Lawhive Knowledge Hub pages and save as Markdown."""

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

BASE_URL = "https://lawhive.co.uk"
START_URL = f"{BASE_URL}/knowledge-hub"
SITEMAP_URL = f"{BASE_URL}/sitemap/GB.xml"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "raw" / "lawhive"
LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "lawhive-crawl-report.md"
INDEX_PATH = OUTPUT_DIR / "index.json"

USER_AGENT = "LegalShamanObsidianCrawler/1.0 (+local research; respects robots.txt)"
REQUEST_DELAY_SEC = 1.5
REQUEST_TIMEOUT_SEC = 30
MAX_RETRIES = 6
RETRY_BASE_DELAY_SEC = 10.0

ALLOWED_PREFIX = "/knowledge-hub"
SECTION_LABEL = "Knowledge Hub"

EXCLUDED_SEGMENTS = frozenset(
    {
        "pricing",
        "contact",
        "login",
        "account",
        "booking",
        "search",
        "tag",
        "tags",
        "intake",
        "onboarding",
        "api",
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
    if any(ord(ch) < 32 for ch in parsed.path):
        return None
    if " " in parsed.path:
        return None
    if parsed.path.lower().endswith(".pdf"):
        return None

    host = parsed.netloc.lower()
    if host and host not in ("lawhive.co.uk", "www.lawhive.co.uk"):
        return None

    path = parsed.path or "/"
    path = re.sub(r"/+", "/", path)
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urllib.parse.urlunparse(("https", "lawhive.co.uk", path, "", "", ""))


def is_allowed_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path

    if not (path == ALLOWED_PREFIX or path.startswith(f"{ALLOWED_PREFIX}/")):
        return False

    segments = [segment.lower() for segment in path.split("/") if segment]
    return not any(segment in EXCLUDED_SEGMENTS for segment in segments)


def topic_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path.strip("/")
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "knowledge-hub":
        return parts[1].replace("-", " ").title()
    return ""


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
            if "@graph" in data and isinstance(data["@graph"], list):
                blocks.extend(item for item in data["@graph"] if isinstance(item, dict))
            else:
                blocks.append(data)
    return blocks


def json_ld_type(data: dict) -> str:
    value = data.get("@type", "")
    if isinstance(value, list):
        return value[0] if value else ""
    return str(value)


def extract_canonical(html: str) -> str:
    match = re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', html, re.IGNORECASE)
    if not match:
        match = re.search(r'<link[^>]+href="([^"]+)"[^>]+rel="canonical"', html, re.IGNORECASE)
    return unescape(match.group(1)).strip() if match else ""


def extract_title(html: str, article_ld: dict | None = None) -> str:
    if article_ld and article_ld.get("headline"):
        return unescape(str(article_ld["headline"])).strip()

    match = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if match:
        title = unescape(match.group(1)).strip()
        if title.endswith(" | Lawhive"):
            title = title[: -len(" | Lawhive")].strip()
        return title

    match = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if match:
        title = unescape(match.group(1)).strip()
        if title.endswith(" | Lawhive"):
            title = title[: -len(" | Lawhive")].strip()
        return title
    return ""


def extract_breadcrumb_topic(html: str) -> str:
    for block in parse_json_ld_blocks(html):
        if json_ld_type(block) != "BreadcrumbList":
            continue
        items = block.get("itemListElement", [])
        if not isinstance(items, list):
            continue
        names: list[str] = []
        for item in items:
            if isinstance(item, dict) and item.get("name"):
                names.append(unescape(str(item["name"])).strip())
        filtered = [name for name in names if name.lower() != "knowledge hub"]
        if len(filtered) >= 2:
            return filtered[-2]
        if filtered:
            return filtered[-1]
    return ""


def extract_author(html: str, article_ld: dict | None = None) -> str:
    if article_ld:
        author = article_ld.get("author")
        if isinstance(author, dict) and author.get("name"):
            return unescape(str(author["name"])).strip()
        if isinstance(author, str):
            return unescape(author).strip()

    match = re.search(r'<meta[^>]+name="author"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    if match:
        return unescape(match.group(1)).strip()
    return ""


def format_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    if match:
        return match.group(1)
    return value


def extract_dates(html: str, article_ld: dict | None = None) -> tuple[str, str]:
    published = ""
    updated = ""

    if article_ld:
        published = format_date(str(article_ld.get("datePublished", "")))
        updated = format_date(str(article_ld.get("dateModified", "")))

    if not published:
        match = re.search(
            r'property="article:published_time"[^>]+content="([^"]+)"',
            html,
            re.IGNORECASE,
        )
        if match:
            published = format_date(unescape(match.group(1)))

    if not updated:
        match = re.search(
            r'property="article:modified_time"[^>]+content="([^"]+)"',
            html,
            re.IGNORECASE,
        )
        if match:
            updated = format_date(unescape(match.group(1)))

    return published, updated


def extract_article_ld(html: str) -> dict | None:
    for block in parse_json_ld_blocks(html):
        block_type = json_ld_type(block)
        if block_type in {"Article", "BlogPosting", "NewsArticle"}:
            return block
    return None


def extract_article_html(html: str) -> str:
    match = re.search(r"<article[^>]*>(.*?)</article>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""

    article = match.group(1)
    removals = [
        r'<section[^>]*>.*?More articles about.*?</section>',
        r"<script[^>]*>.*?</script>",
        r"<style[^>]*>.*?</style>",
    ]
    for pattern in removals:
        article = re.sub(pattern, "", article, flags=re.IGNORECASE | re.DOTALL)
    return article.strip()


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
        return unescape(re.sub(r"\n{3,}", "\n\n", text)).strip()

    md = proc.stdout.strip()
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"^More articles about.*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def slugify(value: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:max_len].strip("-") or "page"


def make_filename(topic: str, title: str, canonical_url: str, used: set[str]) -> str:
    topic_slug = slugify(topic) if topic else "knowledge-hub"
    title_slug = slugify(title)
    if not title_slug:
        path = urllib.parse.urlparse(canonical_url).path.strip("/")
        title_slug = slugify(path.replace("knowledge-hub/", "").replace("/", "-"))
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
            "source: Lawhive",
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
    return f"{frontmatter}\n\n{body}\n"


def write_report(stats: CrawlStats, started_at: datetime, finished_at: datetime) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    duration = (finished_at - started_at).total_seconds()

    lines = [
        "# Lawhive crawl report",
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
    print(f"Discovered {len(urls)} URLs under /knowledge-hub")

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
                        section=SECTION_LABEL,
                        topic="",
                        author="",
                        date_published="",
                        last_updated="",
                        crawl_date=crawl_date,
                        file="",
                        status="duplicate",
                        error=f"duplicate of {seen_canonical[canonical]}",
                    )
                )
                continue

            article_ld = extract_article_ld(html)
            title = extract_title(html, article_ld)
            topic = extract_breadcrumb_topic(html) or topic_from_url(canonical)
            author = extract_author(html, article_ld)
            date_published, last_updated = extract_dates(html, article_ld)
            article_html = extract_article_html(html)
            markdown_body = html_to_markdown(article_html)

            if status_code >= 400 or is_error_page(title):
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
                    "source": "Lawhive",
                    "section": SECTION_LABEL,
                    "topic": topic,
                    "author": author,
                    "date_published": date_published,
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
                    section=SECTION_LABEL,
                    topic="",
                    author="",
                    date_published="",
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
                "source": "Lawhive",
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
