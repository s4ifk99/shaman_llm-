#!/usr/bin/env python3
"""Purge auto-generated wiki content and reset to curated scaffold."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from project_paths import LOGS_DIR, WIKI_DIR

CONTENT_DIRS = (
    "topics",
    "concepts",
    "procedures",
    "organisations",
    "funding",
    "courts",
    "regulations",
)

KEEP_FILES = {"_index.md", "_page-template.md"}


def category_stub(category: str) -> str:
    title = category.replace("-", " ").title()
    return (
        f"# {title}\n\n"
        f"_No curated pages yet. Do not bulk-generate from `/raw`. "
        f"Create pages only after extracting value-add guidance from sources._\n"
    )


def purge_content_pages() -> int:
    removed = 0
    for category in CONTENT_DIRS:
        category_dir = WIKI_DIR / category
        if not category_dir.exists():
            category_dir.mkdir(parents=True)
        for path in category_dir.glob("*.md"):
            if path.name in KEEP_FILES:
                continue
            path.unlink()
            removed += 1
        (category_dir / "_index.md").write_text(category_stub(category), encoding="utf-8")
    return removed


def write_wiki_index() -> None:
    (WIKI_DIR / "index.md").write_text(
        """# Legal Shaman Wiki

Source-grounded legal knowledge for first-point-of-contact signposting.

## Status

**Purged — awaiting curated pages.**

The wiki must contain **value-add guidance only**. It must not mirror `/raw` as link lists,
truncated excerpts, or unprocessed internal links.

When building pages:
1. Read the full source document (and follow internal links where needed).
2. Extract rights, obligations, deadlines, and practical steps into the wiki page.
3. Resolve kernel/internal links — do not leave raw navigation bullets in wiki text.
4. Cite underlying sources in the **Sources** section.

## Categories

- [[Topics]] — curated subject hubs (not raw indexes)
- [[Concepts]] — rights, obligations, definitions
- [[Procedures]] — step-by-step processes
- [[Organisations]] — advisers, courts, regulators
- [[Funding]] — legal aid and fee routes
- [[Courts]] — courts and tribunals
- [[Regulations]] — rules and practice directions

## Source coverage

| Source | Raw path | Pages crawled | Wiki status |
|--------|----------|---------------|-------------|
| Advicenow | `raw/advicenow/` | 275 | Pending curation |
| Citizens Advice | `raw/citizens-advice/` | 1009 | Pending curation |
| Lawhive Knowledge Hub | `raw/lawhive/` | 825 | Pending curation |
| Civil Procedure Rules | `raw/cpr/` | 259 | Pending curation |

Planned sources: Law Centres, Shelter, GOV.UK, LawWorks, Advocate.

## Maintenance

- Ingestion log: `logs/ingestion-log.md`
- Wiki audit: `logs/wiki-audit.md`
- Page template: `wiki/_page-template.md`
""",
        encoding="utf-8",
    )


def append_logs(removed: int) -> None:
    today = date.today().isoformat()
    ingestion = LOGS_DIR / "ingestion-log.md"
    if ingestion.exists():
        content = ingestion.read_text(encoding="utf-8")
        content += (
            f"\n| {today} | Wiki purge | {removed} pages removed | 0 | 0 | "
            f"reset to curated scaffold |\n"
        )
        ingestion.write_text(content, encoding="utf-8")

    audit = LOGS_DIR / "wiki-audit.md"
    entry = (
        f"\n| {today} | Agent | Full wiki purge | Removed {removed} low-value pages. "
        f"Wiki reset. Bulk auto-ingestion disabled. | Rebuild curated pages from `/raw` |\n"
    )
    if audit.exists():
        content = audit.read_text(encoding="utf-8")
        if "## Entries" in content:
            content = content.replace("_No audits recorded yet._\n", "")
        content += entry
        audit.write_text(content, encoding="utf-8")


def main() -> int:
    removed = purge_content_pages()
    write_wiki_index()
    append_logs(removed)
    print(f"Purged {removed} wiki pages. Wiki reset to curated scaffold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
