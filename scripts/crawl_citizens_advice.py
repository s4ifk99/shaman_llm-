#!/usr/bin/env python3
"""Crawl Citizens Advice advice sections and save as Markdown."""

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

BASE_URL = "https://www.citizensadvice.org.uk"
START_URLS = (
    f"{BASE_URL}/about-us/information/consumer-education-resources/",
    f"{BASE_URL}/work/",
    f"{BASE_URL}/debt-and-money/",
    f"{BASE_URL}/consumer/",
    f"{BASE_URL}/housing/",
    f"{BASE_URL}/family/",
    f"{BASE_URL}/law-and-courts/",
    f"{BASE_URL}/immigration/",
    f"{BASE_URL}/health/",
)
SITEMAP_URLS = (
    f"{BASE_URL}/sitemap/advice.xml",
    f"{BASE_URL}/sitemap/corporate.xml",
)
OUTPUT_DIR = RAW_DIR / "citizens-advice"
LOG_PATH = LOGS_DIR / "citizens-advice-crawl-report.md"
INDEX_PATH = OUTPUT_DIR / "index.json"

USER_AGENT = "LegalShamanObsidianCrawler/1.0 (+local research; respects robots.txt)"
REQUEST_DELAY_SEC = 2.0
REQUEST_TIMEOUT_SEC = 30
MAX_RETRIES = 6
RETRY_BASE_DELAY_SEC = 10.0

ALLOWED_PREFIXES = (
    "/work/",
    "/debt-and-money/",
    "/consumer/",
    "/housing/",
    "/family/",
    "/law-and-courts/",
    "/immigration/",
    "/health/",
    "/about-us/information/consumer-education-resources/",
)

ALLOWED_ROOTS = tuple(prefix.rstrip("/") for prefix in ALLOWED_PREFIXES)

REGIONAL_PREFIXES = (
    "/scotland/",
    "/wales/",
    "/cymraeg/",
    "/northern-ireland/",
)

EXCLUDED_SEGMENTS = frozenset(
    {
        "donate",
        "donation",
        "volunteer",
        "volunteering",
        "jobs",
        "job",
        "career",
        "careers",
        "press",
        "policy",
        "policies",
        "campaign",
        "campaigns",
        "contact",
        "login",
        "account",
        "search",
        "tag",
        "tags",
        "advisernet",
        "cookie-preferences",
        "find-a-local",
        "local",
    }
)

SECTION_LABELS = {
    "work": "Work",
    "debt-and-money": "Debt and money",
    "consumer": "Consumer",
    "housing": "Housing",
    "family": "Family",
    "law-and-courts": "Law and courts",
    "immigration": "Immigration",
    "health": "Health",
    "about-us": "Consumer education resources",
}

SKIP_TITLE_PATTERNS = (
    "page not found",
)


@dataclass
class PageRecord:
    url: str
    canonical_url: str
    title: str
    section: str
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
    if host and host not in ("www.citizensadvice.org.uk", "citizensadvice.org.uk"):
        return None

    path = parsed.path or "/"
    path = re.sub(r"/+", "/", path)
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and not path.endswith("/"):
        path = f"{path}/"

    return urllib.parse.urlunparse(("https", "www.citizensadvice.org.uk", path, "", "", ""))


def is_allowed_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path

    if any(path.startswith(prefix) for prefix in REGIONAL_PREFIXES):
        return False

    allowed = any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES)
    if not allowed and path.rstrip("/") not in ALLOWED_ROOTS:
        return False

    if path.startswith("/about-us/") and not path.startswith(
        "/about-us/information/consumer-education-resources/"
    ):
        return False

    segments = [segment.lower() for segment in path.split("/") if segment]
    return not any(segment in EXCLUDED_SEGMENTS for segment in segments)


def section_slug_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path.strip("/")
    if path.startswith("about-us/information/consumer-education-resources"):
        return "consumer-education-resources"
    return path.split("/")[0] if path else "unknown"


def section_label_from_url(url: str) -> str:
    slug = section_slug_from_url(url)
    if slug == "consumer-education-resources":
        return "Consumer education resources"
    return SECTION_LABELS.get(slug, slug.replace("-", " ").title())


def discover_from_sitemaps() -> set[str]:
    urls: set[str] = set()
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for sitemap_url in SITEMAP_URLS:
        _, _, xml_text = fetch_url(sitemap_url)
        root = ET.fromstring(xml_text)
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
    urls: set[str] = set(START_URLS)
    try:
        urls |= discover_from_sitemaps()
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: sitemap discovery failed: {exc}", file=sys.stderr)

    for start_url in START_URLS:
        try:
            _, final_url, html = fetch_url(start_url)
            urls |= discover_from_page(final_url, html)
            time.sleep(REQUEST_DELAY_SEC)
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: hub link discovery failed for {start_url}: {exc}", file=sys.stderr)

    return sorted(urls)


def extract_canonical(html: str) -> str:
    match = re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', html, re.IGNORECASE)
    if not match:
        match = re.search(r'<link[^>]+href="([^"]+)"[^>]+rel="canonical"', html, re.IGNORECASE)
    return unescape(match.group(1)).strip() if match else ""


def extract_title(html: str) -> str:
    match = re.search(r'<span[^>]+data-title[^>]*>([^<]+)</span>', html, re.IGNORECASE)
    if match:
        return unescape(match.group(1)).strip()

    match = re.search(r"<h1[^>]*class=\"cads-page-title\"[^>]*>.*?<span[^>]*>([^<]+)</span>", html, re.DOTALL)
    if match:
        return unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()

    title = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if title:
        value = unescape(title.group(1)).strip()
        if value.endswith(" - Citizens Advice"):
            value = value[: -len(" - Citizens Advice")].strip()
        return value

    match = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, re.IGNORECASE)
    return unescape(match.group(1)).strip() if match else ""


def extract_breadcrumbs(html: str) -> list[str]:
    block = re.search(r'class="cads-breadcrumbs.*?</nav>', html, re.IGNORECASE | re.DOTALL)
    if not block:
        return []

    crumbs = re.findall(
        r'<a[^>]*class="cads-breadcrumb"[^>]*>([^<]+)</a>',
        block.group(0),
        flags=re.IGNORECASE,
    )
    current = re.findall(
        r'<span[^>]*class="cads-breadcrumb"[^>]*aria-current="location"[^>]*>([^<]+)</span>',
        block.group(0),
        flags=re.IGNORECASE,
    )
    return [unescape(c).strip() for c in crumbs + current if c.strip()]


def extract_topic(crumbs: list[str], title: str) -> str:
    filtered = [crumb for crumb in crumbs if crumb.lower() != "home"]
    if filtered and filtered[-1].lower() == title.lower():
        filtered = filtered[:-1]
    if len(filtered) >= 2:
        return filtered[-1]
    return ""


def extract_last_updated(html: str) -> str:
    match = re.search(
        r"Page last reviewed on\s*<strong>\s*([^<]+?)\s*</strong>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return re.sub(r"\s+", " ", unescape(match.group(1))).strip()

    match = re.search(r"Last updated[^<]*<strong>\s*([^<]+?)\s*</strong>", html, re.IGNORECASE)
    if match:
        return re.sub(r"\s+", " ", unescape(match.group(1))).strip()
    return ""


def extract_main_html(html: str) -> str:
    match = re.search(
        r'<main[^>]*class="[^"]*layout__content[^"]*"[^>]*>(.*?)</main>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        match = re.search(r'id="cads-main-content".*?<main[^>]*>(.*?)</main>', html, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""

    main = match.group(1)
    removals = [
        r'<div class="location-switcher".*?</div>\s*</div>',
        r'<div[^>]*data-testid="impact-survey".*?</div>\s*</div>',
        r'<nav[^>]*class="cads-breadcrumbs.*?</nav>',
        r"<script[^>]*>.*?</script>",
        r"<style[^>]*>.*?</style>",
        r'<p>\s*Page last reviewed on.*?</p>',
    ]
    for pattern in removals:
        main = re.sub(pattern, "", main, flags=re.IGNORECASE | re.DOTALL)
    return main.strip()


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
    md = re.sub(r"^This advice applies to England\..*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^See advice for.*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^Help us improve our website.*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^Take 5 minutes to tell us.*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^Page last reviewed on.*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def slugify(value: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:max_len].strip("-") or "page"


def make_filename(section_slug: str, title: str, canonical_url: str, used: set[str]) -> str:
    title_slug = slugify(title)
    path_slug = slugify(urllib.parse.urlparse(canonical_url).path.strip("/").replace("/", "-"))
    base = f"{section_slug}-{title_slug}" if title_slug else f"{section_slug}-{path_slug}"
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
    last_updated: str,
    crawl_date: str,
    body: str,
) -> str:
    frontmatter = "\n".join(
        [
            "---",
            "source: Citizens Advice",
            f"url: {yaml_escape(url)}",
            f"title: {yaml_escape(title)}",
            f"section: {yaml_escape(section)}",
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
        "# Citizens Advice crawl report",
        "",
        f"- **Started:** {started_at.isoformat()}",
        f"- **Finished:** {finished_at.isoformat()}",
        f"- **Duration:** {duration:.1f}s",
        f"- **Start URLs:** {len(START_URLS)} section hubs",
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
    print(f"Discovered {len(urls)} URLs in target sections")

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
                        section=section_label_from_url(canonical),
                        topic="",
                        last_updated="",
                        crawl_date=crawl_date,
                        file="",
                        status="duplicate",
                        error=f"duplicate of {seen_canonical[canonical]}",
                    )
                )
                continue

            title = extract_title(html)
            section = section_label_from_url(canonical)
            section_slug = section_slug_from_url(canonical)
            crumbs = extract_breadcrumbs(html)
            topic = extract_topic(crumbs, title)
            last_updated = extract_last_updated(html)
            main_html = extract_main_html(html)
            markdown_body = html_to_markdown(main_html)

            if status_code >= 400 or is_error_page(title):
                stats.skipped += 1
                stats.pages.append(
                    PageRecord(
                        url=final_url,
                        canonical_url=canonical,
                        title=title,
                        section=section,
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
                        canonical_url=canonical,
                        title=title,
                        section=section,
                        topic=topic,
                        last_updated=last_updated,
                        crawl_date=crawl_date,
                        file="",
                        status="skipped",
                        error="no extractable content",
                    )
                )
                continue

            filename = make_filename(section_slug, title, canonical, used_filenames)
            out_file = OUTPUT_DIR / filename
            content = build_markdown(
                title=title,
                url=canonical,
                section=section,
                topic=topic,
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
                    section=section,
                    topic=topic,
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
                    "source": "Citizens Advice",
                    "section": section,
                    "topic": topic,
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
                    section="",
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
                    canonical_url="",
                    title="",
                    section="",
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
                "source": "Citizens Advice",
                "crawl_date": crawl_date,
                "start_urls": list(START_URLS),
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
