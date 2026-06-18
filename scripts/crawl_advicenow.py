#!/usr/bin/env python3
"""Crawl Advicenow /get-help pages and save as Markdown."""

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
from typing import Iterable

BASE_URL = "https://www.advicenow.org.uk"
START_URL = f"{BASE_URL}/get-help"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "raw" / "advicenow"
LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "advicenow-crawl-report.md"
INDEX_PATH = OUTPUT_DIR / "index.json"

USER_AGENT = "LegalShamanObsidianCrawler/1.0 (+local research; respects robots.txt)"
REQUEST_DELAY_SEC = 2.5
REQUEST_TIMEOUT_SEC = 30
MAX_RETRIES = 6
RETRY_BASE_DELAY_SEC = 10.0

EXCLUDED_PATH_KEYWORDS = (
    "login",
    "register",
    "donate",
    "search",
    "tag",
    "account",
    "event",
    "training",
    "research",
    "about",
)

SKIP_CONTENT_PATTERNS = (
    "we're sorry - we can't find that page",
    "we are sorry - we can't find that page",
)


@dataclass
class PageRecord:
    url: str
    title: str
    topic: str
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

    host = parsed.netloc.lower()
    if host and host not in ("www.advicenow.org.uk", "advicenow.org.uk"):
        return None

    path = parsed.path or "/"
    path = re.sub(r"/+", "/", path)
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urllib.parse.urlunparse(("https", "www.advicenow.org.uk", path, "", "", ""))


def is_allowed_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()

    if not path.startswith("/get-help"):
        return False

    segments = [segment for segment in path.split("/") if segment]
    return not any(keyword in segments for keyword in EXCLUDED_PATH_KEYWORDS)


def discover_from_sitemap() -> set[str]:
    _, _, xml_text = fetch_url(SITEMAP_URL)
    root = ET.fromstring(xml_text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls: set[str] = set()
    for loc in root.findall(".//sm:loc", ns):
        if loc.text:
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
    """Discover crawl targets from sitemap plus link extraction from the hub page."""
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
        print(f"Warning: hub page link discovery failed: {exc}", file=sys.stderr)

    return sorted(urls)


def extract_tag_content(html: str, tag: str) -> str:
    match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html, re.IGNORECASE | re.DOTALL)
    return unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip() if match else ""


def extract_title(html: str) -> str:
    title = extract_tag_content(html, "title")
    if title.endswith(" | Advicenow"):
        title = title[: -len(" | Advicenow")].strip()
    h1_match = re.search(r"<h1[^>]*>.*?<span[^>]*>\s*<span>(.*?)</span>", html, re.DOTALL)
    if h1_match:
        h1 = unescape(re.sub(r"<[^>]+>", "", h1_match.group(1))).strip()
        if h1:
            return h1
    og_match = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
    if og_match:
        return unescape(og_match.group(1)).strip()
    return title


def extract_breadcrumbs(html: str) -> list[str]:
    crumbs = re.findall(
        r'<a[^>]*class="crumb[^"]*"[^>]*>([^<]+)</a>',
        html,
        flags=re.IGNORECASE,
    )
    return [unescape(c).strip() for c in crumbs if c.strip()]


def extract_topic(crumbs: list[str]) -> str:
    for crumb in reversed(crumbs):
        if crumb.lower() not in {"home", "get help"}:
            return crumb
    return ""


def extract_last_updated(html: str) -> str:
    match = re.search(
        r"Last updated:\s*</strong>\s*([^<]+)</p>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return re.sub(r"\s+", " ", unescape(match.group(1))).strip()

    match = re.search(r"Last updated:\s*([^<\n]+)", html, flags=re.IGNORECASE)
    if match:
        return re.sub(r"\s+", " ", unescape(match.group(1))).strip()
    return ""


def extract_article_html(html: str) -> str:
    article_match = re.search(r"<article>(.*?)</article>", html, re.IGNORECASE | re.DOTALL)
    if not article_match:
        return ""

    article = article_match.group(1)

    # Remove non-content UI blocks.
    removals = [
        r'<div class="socialrow[^"]*">.*?</div>',
        r'<div class="flag flag-bookmark">.*?</div>\s*</div>\s*</div>',
        r'<div id="bookmark-login-prompt".*?</div>\s*</div>\s*</div>',
        r'<form[^>]*id="search-form".*?</form>',
        r"<script[^>]*>.*?</script>",
        r"<style[^>]*>.*?</style>",
        r'<nav[^>]*class="breadcrumbs".*?</nav>',
    ]
    for pattern in removals:
        article = re.sub(pattern, "", article, flags=re.IGNORECASE | re.DOTALL)

    return article.strip()


def is_error_page(title: str, content_text: str) -> bool:
    lowered_title = title.lower()
    lowered_content = content_text.lower()
    if "can't find that page" in lowered_title:
        return True
    return any(pattern in lowered_content for pattern in SKIP_CONTENT_PATTERNS)


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
    md = re.sub(r"^\[.*?\]\(javascript:[^\)]*\)\s*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^\[\].*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^\[\].*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^\[\]\(/cdn-cgi/.*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^\[ Print\].*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^Share on:.*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^Bookmark this.*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^Quality checked.*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^Contents\s*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^\s*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def slugify_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path.strip("/")
    if path == "get-help":
        return "get-help"
    if path.startswith("get-help/"):
        path = path[len("get-help/") :]
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", path).strip("-").lower()
    return slug or "index"


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
    topic: str,
    last_updated: str,
    crawl_date: str,
    body: str,
) -> str:
    frontmatter = "\n".join(
        [
            "---",
            "source: Advicenow",
            f"url: {yaml_escape(url)}",
            f"title: {yaml_escape(title)}",
            f"topic: {yaml_escape(topic)}",
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
        "# Advicenow crawl report",
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
            lines.append(f"- [{page.title}]({page.file}) — `{page.url}`")

    if stats.skipped:
        lines.extend(["", "## Skipped pages", ""])
        for page in stats.pages:
            if page.status == "skipped":
                reason = page.error or "skipped"
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
    print(f"Discovered {len(urls)} URLs under /get-help")

    index_entries: list[dict[str, str]] = []

    for i, url in enumerate(urls, start=1):
        slug = slugify_url(url)
        out_file = OUTPUT_DIR / f"{slug}.md"
        print(f"[{i}/{len(urls)}] {url}")

        try:
            status_code, final_url, html = fetch_url(url)
            stats.fetched += 1

            title = extract_title(html)
            crumbs = extract_breadcrumbs(html)
            topic = extract_topic(crumbs)
            last_updated = extract_last_updated(html)
            article_html = extract_article_html(html)
            markdown_body = html_to_markdown(article_html)
            plain = markdown_body.lower()

            if status_code >= 400 or is_error_page(title, plain):
                stats.skipped += 1
                stats.pages.append(
                    PageRecord(
                        url=final_url,
                        title=title,
                        topic=topic,
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
                        title=title,
                        topic=topic,
                        last_updated=last_updated,
                        crawl_date=crawl_date,
                        file="",
                        status="skipped",
                        error="no extractable content",
                    )
                )
                continue

            content = build_markdown(
                title=title,
                url=final_url,
                topic=topic,
                last_updated=last_updated,
                crawl_date=crawl_date,
                body=markdown_body,
            )
            out_file.write_text(content, encoding="utf-8")
            stats.saved += 1
            stats.pages.append(
                PageRecord(
                    url=final_url,
                    title=title,
                    topic=topic,
                    last_updated=last_updated,
                    crawl_date=crawl_date,
                    file=out_file.name,
                    status="saved",
                )
            )
            index_entries.append(
                {
                    "title": title,
                    "url": final_url,
                    "source": "Advicenow",
                    "topic": topic,
                    "last_updated": last_updated,
                    "crawl_date": crawl_date,
                    "file": out_file.name,
                }
            )
        except urllib.error.HTTPError as exc:
            stats.skipped += 1
            msg = f"HTTP {exc.code} for {url}"
            stats.errors.append(msg)
            stats.pages.append(
                PageRecord(
                    url=url,
                    title="",
                    topic="",
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
                    title="",
                    topic="",
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
                "source": "Advicenow",
                "crawl_date": crawl_date,
                "start_url": START_URL,
                "total_discovered": stats.discovered,
                "total_saved": stats.saved,
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
        f"skipped={stats.skipped}, errors={len(stats.errors)}"
    )
    print(f"Index: {INDEX_PATH}")
    print(f"Report: {LOG_PATH}")
    return 0 if not stats.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
