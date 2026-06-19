# Legal Shaman Wiki

Source-grounded legal knowledge wiki for first-point-of-contact legal signposting.

## Project root

**`~/Projects/legal_shaman`** — Obsidian vault and canonical data directory on pravda.

This git repository contains crawler scripts and agent instructions. Crawled data, wiki pages, and logs live under the project root.

## Structure

```
~/Projects/legal_shaman/
  raw/          # Crawled source documents (do not edit manually)
  wiki/         # Generated knowledge pages
  logs/         # Ingestion, update, audit, and conflict reports

shaman_llm-/scripts/   # Approved source crawlers (this repo)
```

## Sources

Citizens Advice, Advicenow, Civil Procedure Rules, and Lawhive Knowledge Hub are crawled into `~/Projects/legal_shaman/raw/`. Additional sources (Law Centres, Shelter, GOV.UK, LawWorks, Advocate) are planned.

## Working with the wiki

- Agent instructions: [AGENTS.md](AGENTS.md)
- Wiki index: `~/Projects/legal_shaman/wiki/index.md`
- Page template: `~/Projects/legal_shaman/wiki/_page-template.md`

## Crawlers

Run from this repository:

```bash
cd ~/Documents/GitHub/shaman_llm-/scripts
python crawl_citizens_advice.py
python crawl_advicenow.py
python crawl_cpr.py
python crawl_lawhive.py
```

Output: `~/Projects/legal_shaman/raw/`  
Reports: `~/Projects/legal_shaman/logs/*-crawl-report.md`
