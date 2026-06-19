# Legal Shaman Wiki — Agent Instructions

Build and maintain a source-grounded legal knowledge wiki for Legal Shaman. The wiki organises trusted public legal guidance and serves as the knowledge layer for first-point-of-contact legal signposting.

## Project root

**`~/Projects/legal_shaman`** (`/home/saif/Projects/legal_shaman` on pravda)

All wiki, raw, and log paths are relative to this directory. Crawlers in this repository write output there via `scripts/project_paths.py`.

## Primary sources

| Source | Raw folder | Crawler | Status |
|--------|------------|---------|--------|
| Citizens Advice | `raw/citizens-advice/` | `scripts/crawl_citizens_advice.py` | Active |
| Advicenow | `raw/advicenow/` | `scripts/crawl_advicenow.py` | Active |
| Civil Procedure Rules | `raw/cpr/` | `scripts/crawl_cpr.py` | Active |
| Lawhive Knowledge Hub | `raw/lawhive/` | `scripts/crawl_lawhive.py` | Active |
| Taylor Rose | `raw/taylor-rose/` | `scripts/crawl_taylor_rose.py` | Active |
| Law Centres | `raw/law-centres/` | — | Planned |
| Shelter | `raw/shelter/` | — | Planned |
| GOV.UK | `raw/govuk/` | — | Planned |
| LawWorks | `raw/lawworks/` | — | Planned |
| Advocate | `raw/advocate/` | — | Planned |

## Folder rules

Paths below are under `~/Projects/legal_shaman/`:

- **`raw/`** — Original crawled/source documents. Never modify manually except through approved crawlers.
- **`wiki/`** — Generated knowledge pages.
- **`logs/`** — Ingestion, update, audit, conflict, and maintenance reports.

Crawler scripts live in this git repository under **`scripts/`**.

## Core rules

1. Preserve the meaning of the original source material.
2. Do not invent legal advice.
3. Do not create unsupported legal conclusions.
4. Every factual claim must be traceable to source material.
5. Update existing wiki pages before creating new ones.
6. Avoid duplicate pages.
7. Use Obsidian links: `[[Page Name]]`
8. Keep source attribution visible.
9. If sources conflict, flag the conflict instead of choosing one silently.

## Wiki page structure

Every wiki page must use this structure:

```markdown
# Title

## Summary

## Key Information

## Practical Guidance

## Related Concepts

## Related Organisations

## Sources
```

Use `wiki/_page-template.md` as the starting point for new pages.

## Wiki categories

| Directory | Contents |
|-----------|----------|
| `wiki/topics/` | Broad legal subject areas |
| `wiki/concepts/` | Legal concepts, rights, obligations |
| `wiki/procedures/` | Step-by-step processes and deadlines |
| `wiki/organisations/` | Advisers, courts, regulators, support bodies |
| `wiki/funding/` | Legal aid, pro bono, fee routes |
| `wiki/courts/` | Courts, tribunals, hearings |
| `wiki/regulations/` | Rules, practice directions, statutory references |

## Ingesting new `/raw` files

1. Read all new or changed files in `/raw`.
2. Extract legal topics, concepts, procedures, organisations, funding routes, rights, obligations, deadlines, and practical steps.
3. Create or update pages under the wiki category directories above.
4. Create backlinks between related concepts.
5. Update `wiki/index.md`.
6. Update `logs/ingestion-log.md`.

## Daily update provision

Once every 24 hours, check all indexed source URLs and approved source sections for:

- new pages
- updated pages
- removed pages
- redirects
- title changes
- content changes
- date changes

**For new pages:** save the source file, update/create relevant wiki pages, add backlinks, update indexes and logs.

**For updated pages:** compare old and new versions, identify meaningful legal/content changes, update only affected wiki sections, preserve links and source attribution, update citations where needed.

**For removed pages:** do not delete wiki pages automatically; mark source as retired, log the removal, flag affected wiki pages for review.

## Logs

| Log | Purpose |
|-----|---------|
| `logs/ingestion-log.md` | Raw-to-wiki ingestion runs |
| `logs/source-update-log.md` | Source URL/content change tracking |
| `logs/daily-update-report.md` | Daily crawl and wiki maintenance summary |
| `logs/conflict-report.md` | Conflicting source material |
| `logs/wiki-audit.md` | Wiki quality and coverage audits |

Daily reports must include: pages checked, pages added, pages updated, pages removed/retired, wiki pages modified, conflicts detected, errors encountered.

Crawler-specific reports (`*-crawl-report.md`) remain separate from wiki maintenance logs.

## Wiki-first policy

The wiki is the **system of record**.

Do not repeatedly analyse the raw source corpus if equivalent information already exists in the wiki. The wiki must become progressively more valuable and remain the primary knowledge repository.

### Preferred workflow

```
Question → Wiki Search → Answer
```

### Fallback workflow (only when necessary)

```
Question → Wiki Search → Raw Sources → Wiki Update → Answer
```

Use the fallback path only when the wiki lacks sufficient information to answer. After consulting raw sources, update the wiki so future questions can use the preferred path.

## Question answering

The wiki is the primary knowledge source.

### Workflow

1. **Search the wiki first.**
2. **Use wiki pages before consulting raw documents.**
3. **If the answer exists in the wiki** — answer using wiki content and cite relevant wiki pages and underlying source documents.
4. **If the wiki contains partial information** — answer using available wiki content, identify what is missing, search relevant source documents if necessary, and update the wiki with newly discovered information.
5. **If the answer cannot be found in the wiki** — search approved source documents in `/raw`, create or update relevant wiki pages, answer the question, and cite the source material used.
6. **Never answer legal questions using unsupported assumptions.**
7. **When multiple sources disagree** — identify the conflict, cite each source, explain the difference, and record it in `logs/conflict-report.md`.

### Response format

Every answer must include these three sections:

```markdown
## Answer

## Relevant Wiki Pages

## Source Documents
```

Use Obsidian links for wiki pages (e.g. `[[Housing]]`, `[[Section 21]]`). List underlying sources by name with links to raw files where available.

### Example

**Question:** Can my landlord evict me with a Section 21 notice?

**Workflow:** Search wiki → Housing → Eviction → Section 21 → answer using wiki content.

**Relevant Wiki Pages:**
- [[Housing]]
- [[Section 21]]

**Source Documents:**
- Citizens Advice — `raw/citizens-advice/...`
- Shelter — (when crawled)
- GOV.UK — (when crawled)
