#!/usr/bin/env python3
"""Crawl GOV.UK browse sections via the Content API and save as Markdown."""

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

BASE_URL = "https://www.gov.uk"
API_BASE = f"{BASE_URL}/api/content"
SOURCE_LABEL = "GOV.UK"

USER_AGENT = "LegalShamanObsidianCrawler/1.0 (+local research; respects robots.txt)"
REQUEST_DELAY_SEC = 0.75
REQUEST_TIMEOUT_SEC = 30
MAX_RETRIES = 6
RETRY_BASE_DELAY_SEC = 10.0

SECTIONS = {
    "citizenship": {
        "browse_path": "/browse/citizenship/citizenship",
        "slug": "citizenship",
        "title": "British citizenship",
        "extra_paths": (
            "/get-child-citizenship-fee-waiver",
            "/renounce-british-nationality",
            "/windrush-prove-your-right-to-be-in-the-uk",
            "/government/publications/application-for-review-when-british-citizenship-is-refused-form-nr",
        ),
        "collections": (
            "/government/collections/citizenship-application-forms",
            "/government/collections/citizenship-guidance",
        ),
    },
}

SKIP_PATH_PREFIXES = (
    "/browse/",
    "/contact",
    "/help",
    "/cymraeg",
    "/government/organisations",
    "/government/get-involved",
    "/government/how-government-works",
    "/search/",
)


@dataclass
class PageTarget:
    base_path: str
    title: str
    page_type: str
    section: str = ""
    collection: str = ""


@dataclass
class PageRecord:
    url: str
    title: str
    page_type: str
    file: str
    status: str
    error: str = ""


@dataclass
class CrawlStats:
    section: str
    discovered: int = 0
    saved: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    pages: list[PageRecord] = field(default_factory=list)


def fetch_json(path: str) -> dict:
    url = f"{API_BASE}{path}"
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (429, 403) and attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_DELAY_SEC * (2**attempt))
                continue
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_DELAY_SEC)
                continue
            raise
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        return resp.read().decode(resp.headers.get_content_charset() or "utf-8", errors="replace")


def clean_text(value: str) -> str:
    text = unescape(value)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def slugify(value: str, max_len: int = 100) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:max_len].strip("-") or "page"


def html_to_markdown(html: str) -> str:
    if not html:
        return ""
    proc = subprocess.run(
        ["html2text", "--body-width", "0"],
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
    md = re.sub(r"^Is this page useful\?.*", "", md, flags=re.DOTALL | re.I)
    md = re.sub(r"^Help us improve GOV\.UK.*", "", md, flags=re.DOTALL | re.I)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def extract_html_main(url: str) -> str:
    html = fetch_html(url)
    match = re.search(
        r'<main[^>]*>(.*?)</main>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1) if match else ""


def build_browse_markdown(data: dict) -> str:
    lines = [f"# {clean_text(data.get('title', 'Browse page'))}", ""]
    description = clean_text(data.get("description", ""))
    if description:
        lines.extend([description, ""])
    groups = data.get("details", {}).get("groups", [])
    children = {
        child.get("content_id"): child
        for child in data.get("links", {}).get("children", [])
    }
    for group in groups:
        name = group.get("name")
        if name:
            lines.extend([f"## {name}", ""])
        for content_id in group.get("content_ids", []):
            child = children.get(content_id)
            if not child:
                continue
            title = clean_text(child.get("title", ""))
            base_path = child.get("base_path", "")
            if title and base_path:
                lines.append(f"- [{title}]({BASE_URL}{base_path})")
        lines.append("")
    return "\n".join(lines).strip()


def extract_body_from_api(data: dict) -> str:
    details = data.get("details", {})
    chunks: list[str] = []

    for key in ("body", "introduction", "introductory_paragraph", "more_information"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            chunks.append(value)

    for part in details.get("parts", []) or []:
        if isinstance(part, dict):
            for key in ("body", "introduction"):
                value = part.get(key)
                if isinstance(value, str) and value.strip():
                    chunks.append(value)

    for variant in details.get("variants", []) or []:
        if not isinstance(variant, dict):
            continue
        for key in ("body", "introductory_paragraph", "more_information"):
            value = variant.get(key)
            if isinstance(value, str) and value.strip():
                chunks.append(value)

    return "\n\n".join(chunks)


def discover_targets(config: dict[str, object]) -> list[PageTarget]:
    browse_path = str(config["browse_path"])
    browse = fetch_json(browse_path)
    targets: dict[str, PageTarget] = {}

    def add(
        base_path: str,
        title: str,
        page_type: str,
        *,
        section: str = "",
        collection: str = "",
        allow_browse: bool = False,
    ) -> None:
        if not base_path.startswith("/"):
            return
        if not allow_browse and any(base_path.startswith(prefix) for prefix in SKIP_PATH_PREFIXES):
            return
        canonical = base_path.rstrip("/") or base_path
        if canonical in targets:
            return
        targets[canonical] = PageTarget(
            base_path=canonical,
            title=title.strip(),
            page_type=page_type,
            section=section,
            collection=collection,
        )

    add(
        browse_path,
        browse.get("title", str(config["title"])),
        "index",
        section="British citizenship",
        allow_browse=True,
    )

    groups = {g.get("name", ""): g for g in browse.get("details", {}).get("groups", [])}
    children = browse.get("links", {}).get("children", [])
    for child in children:
        section_name = ""
        for group_name, group in groups.items():
            if child.get("content_id") in group.get("content_ids", []):
                section_name = group_name or ""
                break
        add(
            child.get("base_path", ""),
            child.get("title", ""),
            child.get("document_type", "page"),
            section=section_name,
        )

    for extra in config.get("extra_paths", ()):
        item = fetch_json(str(extra))
        add(
            item.get("base_path", str(extra)),
            item.get("title", ""),
            item.get("document_type", "page"),
            section="Apply for citizenship",
        )

    for collection_path in config.get("collections", ()):
        collection = fetch_json(str(collection_path))
        add(
            collection.get("base_path", str(collection_path)),
            collection.get("title", ""),
            "collection",
            section="Forms and guidance",
        )
        for doc in collection.get("links", {}).get("documents", []):
            add(
                doc.get("base_path", ""),
                doc.get("title", ""),
                doc.get("document_type", "publication"),
                section="Forms and guidance",
                collection=collection.get("title", ""),
            )

    return list(targets.values())


def make_filename(target: PageTarget, used: set[str]) -> str:
    path = target.base_path.strip("/")
    if target.page_type == "index":
        base = "british-citizenship-index"
    elif path.startswith("government/publications/"):
        base = "publication-" + slugify(path.split("/")[-1])
    elif path.startswith("government/collections/"):
        base = "collection-" + slugify(path.split("/")[-1])
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
    target: PageTarget,
    title: str,
    document_type: str,
    description: str,
    public_updated_at: str,
    crawl_date: str,
    body: str,
    browse_path: str,
) -> None:
    frontmatter = (
        "---\n"
        f"source: {SOURCE_LABEL}\n"
        f'url: "{BASE_URL}{target.base_path}"\n'
        f"title: {json.dumps(title)}\n"
        f"document_type: {json.dumps(document_type)}\n"
        f"page_type: {json.dumps(target.page_type)}\n"
        f"section: {json.dumps(target.section)}\n"
        f"collection: {json.dumps(target.collection)}\n"
        f"browse_path: {json.dumps(browse_path)}\n"
        f"description: {json.dumps(description)}\n"
        f"public_updated_at: {json.dumps(public_updated_at)}\n"
        f'crawl_date: "{crawl_date}"\n'
        "---\n\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter + body + "\n", encoding="utf-8")


def crawl_section(section_key: str, config: dict[str, object]) -> CrawlStats:
    stats = CrawlStats(section=section_key)
    crawl_date = date.today().isoformat()
    output_dir = RAW_DIR / "govuk" / str(config["slug"])
    used_filenames: set[str] = set()
    browse_path = str(config["browse_path"])

    print(f"Discovering pages for {browse_path}")
    targets = discover_targets(config)
    stats.discovered = len(targets)
    print(f"Discovered {len(targets)} pages")

    for index, target in enumerate(targets, start=1):
        if index > 1:
            time.sleep(REQUEST_DELAY_SEC)
        url = f"{BASE_URL}{target.base_path}"
        try:
            data = fetch_json(target.base_path)
            title = clean_text(data.get("title", target.title))
            description = clean_text(data.get("description", ""))
            public_updated_at = str(data.get("public_updated_at", ""))
            body = html_to_markdown(extract_body_from_api(data))
            if not body and data.get("document_type") == "mainstream_browse_page":
                body = build_browse_markdown(data)
            if not body:
                body = html_to_markdown(extract_html_main(url))
            if not body:
                stats.skipped += 1
                stats.pages.append(
                    PageRecord(
                        url=url,
                        title=title,
                        page_type=target.page_type,
                        file="",
                        status="skipped",
                        error="no extractable content",
                    )
                )
                continue

            filename = make_filename(target, used_filenames)
            file_path = output_dir / filename
            write_markdown(
                file_path,
                target=target,
                title=title,
                document_type=str(data.get("document_type", target.page_type)),
                description=description,
                public_updated_at=public_updated_at,
                crawl_date=crawl_date,
                body=body,
                browse_path=browse_path,
            )
            stats.saved += 1
            stats.pages.append(
                PageRecord(
                    url=url,
                    title=title,
                    page_type=target.page_type,
                    file=str(file_path.relative_to(RAW_DIR)),
                    status="saved",
                )
            )
            if index % 20 == 0 or index == len(targets):
                print(
                    f"Progress: {index}/{len(targets)} "
                    f"saved={stats.saved} skipped={stats.skipped}"
                )
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"{url}: {exc}")
            stats.skipped += 1
            stats.pages.append(
                PageRecord(
                    url=url,
                    title=target.title,
                    page_type=target.page_type,
                    file="",
                    status="error",
                    error=str(exc),
                )
            )

    index_data = {
        "source": SOURCE_LABEL,
        "browse_path": browse_path,
        "crawl_date": crawl_date,
        "pages_discovered": stats.discovered,
        "pages_saved": stats.saved,
        "pages": [
            {
                "url": page.url,
                "title": page.title,
                "page_type": page.page_type,
                "file": page.file,
                "status": page.status,
                "error": page.error,
            }
            for page in stats.pages
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.json").write_text(
        json.dumps(index_data, indent=2) + "\n",
        encoding="utf-8",
    )
    return stats


def write_report(all_stats: list[CrawlStats]) -> None:
    lines = ["# GOV.UK crawl report", "", f"Date: {date.today().isoformat()}", ""]
    for stats in all_stats:
        lines.extend(
            [
                f"## {stats.section}",
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
            lines.extend(f"- {error}" for error in stats.errors[:30])
            lines.append("")
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    (LOGS_DIR / "govuk-crawl-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    keys = argv[1:] or list(SECTIONS)
    all_stats: list[CrawlStats] = []
    for key in keys:
        config = SECTIONS.get(key)
        if config is None:
            print(f"Unknown section: {key}", file=sys.stderr)
            return 1
        stats = crawl_section(key, config)
        all_stats.append(stats)
        print(f"Done: {stats.saved} files saved for {key}")
    write_report(all_stats)
    return 0 if all(not s.errors for s in all_stats) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
