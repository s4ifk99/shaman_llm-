#!/usr/bin/env python3
"""Crawl Sentencing Council magistrates' court guidelines and save as Markdown."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from html import unescape
from pathlib import Path

from project_paths import LOGS_DIR, RAW_DIR

BASE_URL = "https://sentencingcouncil.org.uk"
START_URL = f"{BASE_URL}/guidelines/magistrates/"
OUTPUT_DIR = RAW_DIR / "sentencing-council" / "magistrates"
LOG_PATH = LOGS_DIR / "sentencing-council-crawl-report.md"
INDEX_PATH = OUTPUT_DIR / "index.json"
SOURCE_LABEL = "Sentencing Council"

USER_AGENT = "LegalShamanObsidianCrawler/1.0 (+local research; respects robots.txt)"
REQUEST_DELAY_SEC = 1.0
REQUEST_TIMEOUT_SEC = 30
MAX_RETRIES = 6
RETRY_BASE_DELAY_SEC = 10.0

OVERARCHING_PATHS = (
    "/guidelines/allocation-and-committal-for-sentence/",
    "/guidelines/domestic-abuse-overarching-principles/",
    "/guidelines/driving-disqualification/",
    "/guidelines/general-guideline-overarching-principles/",
    "/guidelines/imposition-of-community-and-custodial-sentences/",
    "/guidelines/offences-taken-into-consideration/",
    "/guidelines/reduction-in-sentence-for-a-guilty-plea-first-hearing-on-or-after-1-june-2017/",
    "/guidelines/sentencing-children-and-young-people/",
    "/guidelines/sentencing-offenders-with-mental-disorders-developmental-disorders-or-neurological-impairments/",
    "/guidelines/totality/",
)

SUPPLEMENTARY_PATHS = (
    "/supplementary-information/ancillary-orders/",
    "/supplementary-information/approach-to-fines/",
    "/supplementary-information/compensation/",
    "/supplementary-information/deferred-sentences/",
    "/supplementary-information/hate-crime/",
    "/supplementary-information/offences-in-a-domestic-abuse-context/",
    "/supplementary-information/other-financial-orders/",
    "/supplementary-information/out-of-court-disposals/",
    "/supplementary-information/road-traffic-offences-disqualification/",
    "/supplementary-information/using-the-guidelines/",
    "/supplementary-information/victims/",
)

SKIP_TITLE_PATTERNS = ("page not found", "404")


@dataclass
class GuidelineTarget:
    url: str
    title: str
    guideline_type: str
    acts: str = ""
    tags: list[str] = field(default_factory=list)
    collection: str = ""
    court_types: list[str] = field(default_factory=list)


@dataclass
class PageRecord:
    url: str
    title: str
    guideline_type: str
    file: str
    status: str
    error: str = ""


@dataclass
class CrawlStats:
    discovered: int = 0
    saved: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    pages: list[PageRecord] = field(default_factory=list)


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


def clean_text(value: str) -> str:
    text = unescape(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def slugify(value: str, max_len: int = 100) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:max_len].strip("-") or "page"


def yaml_escape(value: str) -> str:
    return json.dumps(value)


def discover_targets(hub_html: str) -> list[GuidelineTarget]:
    targets: list[GuidelineTarget] = []
    seen: set[str] = set()

    def add(target: GuidelineTarget) -> None:
        canonical = target.url.split("?")[0].rstrip("/") + "/"
        if canonical in seen:
            return
        seen.add(canonical)
        targets.append(
            GuidelineTarget(
                url=canonical,
                title=target.title,
                guideline_type=target.guideline_type,
                acts=target.acts,
                tags=target.tags,
                collection=target.collection,
                court_types=target.court_types,
            )
        )

    add(
        GuidelineTarget(
            url=START_URL,
            title="Magistrates' courts sentencing guidelines",
            guideline_type="index",
            court_types=["Magistrates"],
        )
    )

    for path in OVERARCHING_PATHS:
        add(
            GuidelineTarget(
                url=f"{BASE_URL}{path}",
                title=path.strip("/").split("/")[-1].replace("-", " ").title(),
                guideline_type="overarching",
                court_types=["Magistrates"],
            )
        )

    for path in SUPPLEMENTARY_PATHS:
        add(
            GuidelineTarget(
                url=f"{BASE_URL}{path}",
                title=path.strip("/").split("/")[-1].replace("-", " ").title(),
                guideline_type="supplementary",
                court_types=["Magistrates"],
            )
        )

    match = re.search(r"var guidelineData = (\[.*?\]);\s*\n", hub_html, re.S)
    if not match:
        raise RuntimeError("Could not parse guidelineData from magistrates hub page")

    for item in json.loads(match.group(1)):
        court_types = item.get("courtType", [])
        if "Magistrates" not in court_types:
            continue
        rel_url = item.get("url", "").split("?")[0]
        if not rel_url:
            continue
        collection = ""
        collections = item.get("relevantCollections") or []
        if collections:
            collection = str(collections[0].get("name", "")).strip()
        add(
            GuidelineTarget(
                url=f"{BASE_URL}{rel_url}",
                title=str(item.get("name", "")).strip(),
                guideline_type="offence",
                acts=str(item.get("acts", "")).strip(),
                tags=[str(tag) for tag in item.get("tags", [])],
                collection=collection,
                court_types=[str(c) for c in court_types],
            )
        )

    return targets


def extract_title(html: str) -> str:
    match = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if match:
        return clean_text(match.group(1))
    return ""


def extract_effective_from(html: str) -> str:
    match = re.search(r"Effective from</[^>]+>\s*([^<]+)", html, re.IGNORECASE)
    if match:
        return clean_text(match.group(1))
    match = re.search(r"\*\*Effective from\*\*\s*([^\n<]+)", html, re.IGNORECASE)
    if match:
        return clean_text(match.group(1))
    return ""


def extract_main_html(html: str) -> str:
    match = re.search(
        r'<main[^>]*id="main-content"[^>]*>(.*?)</main>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""

    main = match.group(1)
    removals = (
        r"<button[^>]*>.*?</button>",
        r"<svg[^>]*>.*?</svg>",
        r"<form[^>]*>.*?</form>",
        r"<script[^>]*>.*?</script>",
        r"<style[^>]*>.*?</style>",
        r"<nav[^>]*>.*?</nav>",
        r"Add to bookmarks.*?Remove from bookmarks",
        r"Toggle all dropdowns",
        r"Go to guideline bookmarks",
    )
    for pattern in removals:
        main = re.sub(pattern, "", main, flags=re.IGNORECASE | re.DOTALL)
    return main.strip()


def html_to_markdown(html: str) -> str:
    if not html:
        return ""

    proc = subprocess.run(
        ["html2text", "--body-width", "0", "--ignore-links"],
        input=html,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        text = re.sub(r"<[^>]+>", "", html)
        md = unescape(re.sub(r"\n{3,}", "\n\n", text)).strip()
    else:
        md = proc.stdout.strip()

    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"^[⇓⇑* ]+$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^Crown Court\s*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"^Magistrates\s*$", "", md, flags=re.MULTILINE)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def is_error_page(title: str) -> bool:
    lowered = title.lower()
    return any(pattern in lowered for pattern in SKIP_TITLE_PATTERNS)


def make_filename(target: GuidelineTarget, used: set[str]) -> str:
    path = urllib.parse.urlparse(target.url).path.strip("/")
    if target.guideline_type == "index":
        base = "magistrates-courts-sentencing-guidelines"
    elif target.guideline_type == "supplementary":
        base = "supplementary-" + slugify(path.split("/")[-1])
    else:
        base = slugify(path.split("/")[-1])
    candidate = base
    counter = 2
    while candidate in used:
        candidate = f"{base}-{counter}"
        counter += 1
    used.add(candidate)
    return f"{candidate}.md"


def write_markdown(
    path: Path,
    *,
    target: GuidelineTarget,
    title: str,
    effective_from: str,
    crawl_date: str,
    body: str,
) -> None:
    tags_yaml = json.dumps(target.tags)
    courts_yaml = json.dumps(target.court_types)
    frontmatter = (
        "---\n"
        f"source: {SOURCE_LABEL}\n"
        f"url: {yaml_escape(target.url)}\n"
        f"title: {yaml_escape(title)}\n"
        f"guideline_type: {yaml_escape(target.guideline_type)}\n"
        f"court: magistrates\n"
        f"acts: {yaml_escape(target.acts)}\n"
        f"collection: {yaml_escape(target.collection)}\n"
        f"tags: {tags_yaml}\n"
        f"court_types: {courts_yaml}\n"
        f"effective_from: {yaml_escape(effective_from)}\n"
        f'crawl_date: "{crawl_date}"\n'
        "---\n\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter + body + "\n", encoding="utf-8")


def crawl_magistrates() -> CrawlStats:
    stats = CrawlStats()
    crawl_date = date.today().isoformat()
    used_filenames: set[str] = set()

    print(f"Fetching hub {START_URL}")
    _, _, hub_html = fetch_url(START_URL)
    targets = discover_targets(hub_html)
    stats.discovered = len(targets)
    print(f"Discovered {len(targets)} pages")

    for index, target in enumerate(targets, start=1):
        if index > 1:
            time.sleep(REQUEST_DELAY_SEC)
        try:
            status_code, final_url, html = fetch_url(target.url)
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"{target.url}: {exc}")
            stats.skipped += 1
            stats.pages.append(
                PageRecord(
                    url=target.url,
                    title=target.title,
                    guideline_type=target.guideline_type,
                    file="",
                    status="error",
                    error=str(exc),
                )
            )
            continue

        title = extract_title(html) or target.title
        if status_code >= 400 or is_error_page(title):
            stats.skipped += 1
            stats.pages.append(
                PageRecord(
                    url=target.url,
                    title=title,
                    guideline_type=target.guideline_type,
                    file="",
                    status="skipped",
                    error="error page",
                )
            )
            continue

        markdown_body = html_to_markdown(extract_main_html(html))
        if not markdown_body:
            stats.skipped += 1
            stats.pages.append(
                PageRecord(
                    url=target.url,
                    title=title,
                    guideline_type=target.guideline_type,
                    file="",
                    status="skipped",
                    error="no extractable content",
                )
            )
            continue

        filename = make_filename(target, used_filenames)
        file_path = OUTPUT_DIR / filename
        write_markdown(
            file_path,
            target=target,
            title=title,
            effective_from=extract_effective_from(html),
            crawl_date=crawl_date,
            body=markdown_body,
        )
        stats.saved += 1
        stats.pages.append(
            PageRecord(
                url=final_url.split("?")[0].rstrip("/") + "/",
                title=title,
                guideline_type=target.guideline_type,
                file=str(file_path.relative_to(RAW_DIR)),
                status="saved",
            )
        )
        if index % 25 == 0 or index == len(targets):
            print(f"Progress: {index}/{len(targets)} saved={stats.saved} skipped={stats.skipped}")

    index_data = {
        "source": SOURCE_LABEL,
        "hub_url": START_URL,
        "crawl_date": crawl_date,
        "pages_saved": stats.saved,
        "pages_discovered": stats.discovered,
        "pages": [
            {
                "url": page.url,
                "title": page.title,
                "guideline_type": page.guideline_type,
                "file": page.file,
                "status": page.status,
                "error": page.error,
            }
            for page in stats.pages
        ],
    }
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index_data, indent=2) + "\n", encoding="utf-8")
    return stats


def write_report(stats: CrawlStats) -> None:
    lines = [
        "# Sentencing Council crawl report",
        "",
        f"Date: {date.today().isoformat()}",
        "",
        "## Magistrates' court guidelines",
        "",
        f"- Hub: {START_URL}",
        f"- Discovered: {stats.discovered}",
        f"- Saved: {stats.saved}",
        f"- Skipped: {stats.skipped}",
        f"- Errors: {len(stats.errors)}",
        "",
    ]
    if stats.errors:
        lines.append("### Errors")
        lines.extend(f"- {error}" for error in stats.errors[:50])
        lines.append("")
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    try:
        stats = crawl_magistrates()
        write_report(stats)
        print(f"Done: {stats.saved} files saved to {OUTPUT_DIR}")
        return 0 if not stats.errors else 1
    except Exception as exc:  # noqa: BLE001
        print(f"Crawl failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
