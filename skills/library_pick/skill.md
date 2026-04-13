---
id: library_pick
name: Library Pick
type: prompt
description: Search Zotero by keyword/author/criteria, locate markdowns and PDFs, then COPY them to a specified literature group folder. NEVER deletes or moves originals from the main library. Handles missing markdowns (locate PDF fallback, optional PDF-to-markdown conversion).
---

You are performing a **Library Pick** operation for the user's PhD reference library. Follow the steps below precisely and interactively.

> ⚠️ **CRITICAL RULE — READ THIS FIRST:**
> This is **always a COPY operation**. You **MUST NEVER delete, move, or modify** any file in the main library (`00_main_library/markdowns/`) or anywhere else in the source. The originals stay exactly where they are. You are only making copies into the destination folder. If you ever feel tempted to "move" a file, stop — copy it instead.

---

## Paths (fixed, never change these)

- **Zotero local API**: `http://localhost:23119/api/`
- **Markdowns folder**: `/mnt/c/Users/thene/projects/UON_PhD/Barry's PhD/00_main_library/markdowns/`
- **Main library index**: `/mnt/c/Users/thene/projects/UON_PhD/Barry's PhD/00_main_library/main_library_index.md`
- **Literature groups root**: `/mnt/c/Users/thene/projects/UON_PhD/Barry's PhD/07_references/literature_group/`

## Naming Protocol v1.1

All markdown files must be named: `LastName_Year_first_6_words_of_title.md`
- `LastName` = first creator's last name (from Zotero `creators[0].lastName`)
- `Year` = publication year
- `first_6_words_of_title` = first 6 words of the title, joined by `_`, lowercased, with non-alphanumeric characters stripped
- Example: `Alvesson_2002_identity_regulation_as_organizational_control_producing.md`

---

## Step 1 — Collect search criteria

Ask the user:
1. **Search criteria**: What keyword(s), tag(s), or author name to search in Zotero? (e.g. `keyword: identity`, `author: Alvesson`, `tag: ANT`)
2. **Destination folder**: Which literature group folder to copy files into? (provide full path, or just the folder name under `07_references/literature_group/`)

Confirm both before proceeding.

---

## Step 2 — Query Zotero

Query the Zotero local API at `http://localhost:23119/api/` to find all matching items.

**For keyword/tag search**, query:
```
GET http://localhost:23119/api/users/0/items?tag=<keyword>&limit=100&format=json
```

**For author search**, query:
```
GET http://localhost:23119/api/users/0/items?q=<author>&qmode=everything&limit=100&format=json
```

If results are paginated (response header `Total-Results` > 100), iterate with `&start=100`, `&start=200`, etc. until all results are retrieved.

Filter out attachment items (item type `attachment`, `note`) — keep only real library items (journal articles, book chapters, books, etc.).

For each matching item, extract:
- `key` (Zotero item key)
- `title`
- `creators[0].lastName` + `creators[0].firstName`
- `date` or `year`
- `DOI` (if present)

Report the total count and list all items to the user.

---

## Step 3 — Locate markdown files

For each item, look up the markdown in two ways:

**Method A — Main library index lookup**
Read `/mnt/c/Users/thene/projects/UON_PhD/Barry's PhD/00_main_library/main_library_index.md` and search for the item's Zotero key. If found, extract the markdown filename and path from the `markdown` column.

**Method B — Filename match**
Construct the expected filename using Naming Protocol v1.1, then check if it exists in the markdowns folder.

Classify each item as:
- ✅ **Markdown found** — path known
- ⚠️ **Markdown missing** — proceed to Step 4
- (Note if the same paper appears under two Zotero keys, treat it as one unique item)

---

## Step 4 — Handle missing markdowns

For each item with no markdown found:

**Sub-step 4a — Locate PDF via Zotero**
Query the item's attachments:
```
GET http://localhost:23119/api/users/0/items/<item_key>/children?format=json
```
Look for attachment items where `contentType` is `application/pdf`. Use the `path` field (may be absolute or relative to the Zotero data directory) to resolve the PDF file location.

Common Zotero data directory on this machine: `/mnt/c/Users/thene/Zotero/storage/`

Classify each missing-markdown item as:
- 📄 **PDF found** — path known
- ❌ **Nothing found** — neither markdown nor PDF

**Sub-step 4b — Report and ask**

Present a summary to the user:
```
Items with markdown: X
Items with PDF only: Y  →  [list with titles]
Items with nothing:  Z  →  [list with titles]
```

Ask the user:
1. For PDF-only items: "要把PDF复制过去，还是先转成Markdown再复制？(Copy PDF as-is, or convert to Markdown first?)"
2. For nothing-found items: "这些找不到文件的，要去下载吗？需要小樱列出DOI/链接吗？(Download needed? Want me to list DOIs/links?)"

Wait for user's answer before continuing.

---

## Step 5 — PDF to Markdown conversion (if requested)

If the user wants PDFs converted to markdown:

Use the following Python approach with `pymupdf` (fitz):

```python
import fitz  # pymupdf
import re

def pdf_to_markdown(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    pages = []
    for page in doc:
        text = page.get_text("text")
        pages.append(text)
    return "\n\n---\n\n".join(pages)
```

Name each output file using Naming Protocol v1.1 (use the Zotero metadata — `creators[0].lastName`, year, first 6 words of title).

Save the converted markdown to:
`/mnt/c/Users/thene/projects/UON_PhD/Barry's PhD/00_main_library/markdowns/<filename>.md`

After conversion, update the main library index (`main_library_index.md`) for each converted item: fill in the `markdown` column with the filename, relative path, and obsidian wikilink `[[filename]]`.

**Test the conversion on the first file before proceeding with the rest.** Confirm it looks reasonable before bulk-converting.

---

## Step 6 — Copy files to destination

**COPY** (never delete, never move) all resolved markdown files to the destination folder specified by the user in Step 1. Originals in `00_main_library/markdowns/` must remain untouched.

If the user chose to copy PDFs for some items, copy those PDFs too.

After copying, list all files now in the destination folder and confirm the count.

---

## Step 7 — Final report

Report to the user:
- Total items matched in Zotero: N
- Markdowns **copied** to destination: X (list filenames)
- PDFs **copied** to destination (as PDF): Y (list filenames)
- PDFs converted to markdown then **copied**: Z (list filenames)
- ✅ All originals in `00_main_library/markdowns/` preserved — nothing deleted
- Items still missing (nothing found): W (list titles + DOI if available)

Ask if there's anything else to do.

---

## User's command

{prompt}
