"""PCM documentation navigation; file execution and permission policy stay in PAO.

Only the small catalogue is assembled by agent_fyi. Detailed source is read
explicitly, with scoped links, content revisions and visible continuation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urldefrag, urlsplit

CATALOGUE_MARKER = "<!-- hashi-fyi-catalogue:v2 -->"
CATALOGUE_SECTIONS = (
    "Purpose and architecture", "Ownership", "Universal principles",
    "Domain catalogue", "Find and read", "Evidence and authority",
)
MANDATORY_DOMAINS = frozenset({
    "engineering", "lifecycle", "pcm", "messaging", "voice", "compute",
    "permissions", "files-and-media",
})
ROOT_DOCUMENTS = frozenset({"AGENTS.md", "ARCHITECTURE.md", "CONTRIBUTING.md"})
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


class DocumentationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Domain:
    id: str
    description: str
    when_to_read: str
    guide: str


def slugify(title: str) -> str:
    title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title)
    title = re.sub("[\\x60*]", "", title).strip().casefold()
    title = re.sub(r"[^\w\s-]", "", title)
    return re.sub(r"\s", "-", title)


def headings(text: str) -> list[dict]:
    result, counts = [], {}
    fence = None
    position = 0
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        stripped = line.lstrip()
        marker = chr(96) * 3 if stripped.startswith(chr(96) * 3) else "~~~"
        if stripped.startswith((chr(96) * 3, "~~~")):
            fence = None if fence == marker else marker
        elif not fence:
            match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line.rstrip())
            if match:
                title = match[2]
                base = slugify(title)
                repeat = counts.get(base, 0)
                counts[base] = repeat + 1
                result.append({
                    "title": title, "anchor": base + (f"-{repeat}" if repeat else ""),
                    "level": len(match[1]), "line": number, "offset": position,
                })
        position += len(line)
    return result


def section_text(text: str, section: str) -> tuple[str, int]:
    if not section:
        return text, 0
    wanted = unquote(section).lstrip("#")
    candidates = headings(text)
    for index, item in enumerate(candidates):
        if wanted != item["anchor"] and slugify(wanted) != item["anchor"]:
            continue
        end = next(
            (later["offset"] for later in candidates[index + 1:]
             if later["level"] <= item["level"]), len(text),
        )
        return text[item["offset"]:end], item["offset"]
    raise DocumentationError("anchor_missing", f"Documentation section does not exist: {wanted}")


def documentation_root(configured_root: Path | str) -> Path:
    """Use the existing qualified-generation binding, including CLI gateway hosts."""
    configured = Path(configured_root).resolve()
    generation = os.environ.get("HASHI_FUNCTION_GENERATION_ROOT")
    source = os.environ.get("HASHI_SOURCE_ROOT")
    if generation:
        installed = Path(generation).resolve()
        if Path(__file__).resolve().is_relative_to(installed) or (source and configured == Path(source).resolve()):
            return installed
    return configured


def documentation_path(root: Path, reference: str, *, from_path: str = "docs/AGENT_FYI.md") -> tuple[Path, str]:
    root = root.resolve()
    raw, fragment = urldefrag(unquote(str(reference)))
    if not raw:
        raw = from_path
    if urlsplit(raw).scheme or raw.startswith(("/", "\\")) or "\\" in raw:
        raise DocumentationError("outside_documentation", "Use a local Markdown documentation link.")
    if raw.startswith("docs/") or raw in ROOT_DOCUMENTS:
        target = root / raw
    else:
        source = (root / from_path).resolve()
        if not (source.is_relative_to(root / "docs") or source in {root / p for p in ROOT_DOCUMENTS}):
            raise DocumentationError("outside_documentation", "Link source is outside approved documentation roots.")
        target = source.parent / raw
    target = target.resolve()
    if not (target.is_relative_to(root / "docs") or target in {root / p for p in ROOT_DOCUMENTS}):
        raise DocumentationError("outside_documentation", "Target is outside approved documentation roots.")
    if target.suffix.casefold() != ".md":
        raise DocumentationError("unsupported_document", "Documentation retrieval accepts Markdown only.")
    if not target.is_file():
        raise DocumentationError("document_missing", f"Documentation is unavailable: {target.relative_to(root).as_posix()}")
    return target, fragment


def read_document_bytes(path: Path) -> tuple[str, str]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise DocumentationError("document_unavailable", "Documentation could not be read.") from exc
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise DocumentationError("document_oversized", "Document exceeds the read safety budget; no partial document was returned.")
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeError as exc:
        raise DocumentationError("invalid_document", "Documentation must be UTF-8 Markdown.") from exc
    if not text.strip():
        raise DocumentationError("document_empty", "Documentation exists but is empty.")
    return text, hashlib.sha256(raw).hexdigest()


def _meaningful(text: str) -> bool:
    prose = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip(" \t\r\n`*_-.")
    return bool(prose) and prose.casefold() not in {"todo", "tbd", "n/a", "pending", "placeholder"}


def catalogue_domains(text: str) -> list[Domain]:
    if CATALOGUE_MARKER not in text:
        raise DocumentationError("invalid_catalogue", "Catalogue format marker is missing.")
    titles = {item["title"] for item in headings(text) if item["level"] == 2}
    missing = set(CATALOGUE_SECTIONS) - titles
    if missing:
        raise DocumentationError("invalid_catalogue", "Missing catalogue sections: " + ", ".join(sorted(missing)))
    for title in CATALOGUE_SECTIONS:
        section, _ = section_text(text, title)
        if not _meaningful(section.partition("\n")[2]):
            raise DocumentationError("invalid_catalogue", f"Catalogue section is empty: {title}")
    body, _ = section_text(text, "Domain catalogue")
    result, seen = [], set()
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4 or cells[0] == "Domain" or set(cells[0]) <= {"-", ":"}:
            continue
        identifier, description, trigger, link = cells
        identifier = identifier.strip(chr(96))
        match = _LINK.fullmatch(link)
        if not re.fullmatch(r"[a-z][a-z0-9-]*", identifier) or identifier in seen:
            raise DocumentationError("invalid_catalogue", f"Invalid or duplicate domain identifier: {identifier}")
        if not _meaningful(description) or not _meaningful(trigger) or not match:
            raise DocumentationError("invalid_catalogue", f"{identifier}: description, when-to-read trigger and guide are required.")
        seen.add(identifier)
        result.append(Domain(identifier, description, trigger, match[1]))
    missing_domains = MANDATORY_DOMAINS - seen
    if missing_domains:
        raise DocumentationError("invalid_catalogue", "Missing domains: " + ", ".join(sorted(missing_domains)))
    return result


def _check_links(root: Path, source: Path, text: str) -> list[str]:
    errors = []
    anchors = {item["anchor"] for item in headings(text)}
    anchors.update(re.findall(r'<a\s+(?:id|name)=["\x27]([^"\x27]+)', text))
    for match in _LINK.finditer(text):
        reference = match[1]
        if urlsplit(reference).scheme:
            continue
        if reference.startswith("#"):
            if unquote(reference[1:]) not in anchors:
                errors.append(f"{source.relative_to(root)}: broken anchor {reference}")
            continue
        try:
            target, fragment = documentation_path(root, reference, from_path=source.relative_to(root).as_posix())
            if fragment:
                target_text, _ = read_document_bytes(target)
                target_anchors = {item["anchor"] for item in headings(target_text)}
                target_anchors.update(re.findall(r'<a\s+(?:id|name)=["\x27]([^"\x27]+)', target_text))
                if unquote(fragment) not in target_anchors:
                    raise DocumentationError("anchor_missing", f"broken anchor {reference}")
        except DocumentationError as exc:
            errors.append(f"{source.relative_to(root)}: {exc}")
    return errors


def validate_catalogue(path: Path, *, full: bool = False, text: str | None = None) -> list[str]:
    """Bootstrap validation reads the entry only; CI explicitly inspects guides."""
    root = path.parent.parent if path.parent.name == "docs" else path.parent
    try:
        if text is None:
            text, _ = read_document_bytes(path)
        domains = catalogue_domains(text)
        if re.search(r"(?im)^\s*(?:!include|<!--\s*include)\b", text):
            raise DocumentationError("automatic_inclusion", "Automatic document inclusion is not supported.")
        guides = [(domain, documentation_path(root, domain.guide, from_path=path.relative_to(root).as_posix())[0])
                  for domain in domains]
        for domain, guide in guides:
            if not guide.is_relative_to(root.resolve() / "docs/agent-guide"):
                raise DocumentationError("invalid_catalogue", f"{domain.id}: domain guide must be under docs/agent-guide/.")
    except DocumentationError as exc:
        return [str(exc)]
    errors = _check_links(root, path, text) if full else []
    if not full:
        return errors
    for domain, guide in guides:
        try:
            body, _ = read_document_bytes(guide)
            guide_headings = headings(body)
            for term in ("purpose", "when", "ownership", "distinctions", "authoritative", "source", "verification", "adoption"):
                matches = [item for item in guide_headings if term in item["title"].casefold()]
                if not matches:
                    errors.append(f"{domain.id}: guide requires {term} guidance")
                for item in matches:
                    section, _ = section_text(body, item["anchor"])
                    if not _meaningful(section.partition("\n")[2]):
                        errors.append(f"{domain.id}: {item['title']} guidance is empty")
            authority, _ = section_text(body, "Authoritative documents")
            links = list(_LINK.finditer(authority))
            contract_destinations = 0
            if not links:
                errors.append(f"{domain.id}: at least one authoritative destination is required")
            for link in links:
                destination, _ = documentation_path(root, link[1], from_path=guide.relative_to(root).as_posix())
                if not destination.is_relative_to(root.resolve() / "docs/agent-guide"):
                    contract_destinations += 1
                destination_text, _ = read_document_bytes(destination)
                if re.search(r"(?im)^status:\s*(?:historical|superseded)\b", destination_text[:2000]):
                    errors.append(f"{domain.id}: historical document is advertised as current: {link[1]}")
            if links and not contract_destinations:
                errors.append(f"{domain.id}: navigation guides cannot replace authoritative contracts")
            if re.search(r"(?im)^\s*(?:!include|<!--\s*include)\b", body):
                errors.append(f"{domain.id}: automatic inclusion is not supported")
            errors.extend(_check_links(root, guide, body))
        except DocumentationError as exc:
            errors.append(f"{domain.id}: {exc}")
    return errors


def read_documentation(arguments: dict, *, code_root: Path) -> dict:
    """Read a complete document/section through explicit, revision-bound pages."""
    try:
        operation = arguments.get("operation", "read")
        if operation not in {"read", "outline"}:
            raise DocumentationError("invalid_arguments", "Operation must be read or outline.")
        offset = arguments.get("offset", 0)
        maximum = arguments.get("max_chars", 4000)
        if type(offset) is not int or offset < 0 or type(maximum) is not int or not 256 <= maximum <= 8000:
            raise DocumentationError("invalid_arguments", "Use a nonnegative offset and max_chars between 256 and 8000.")
        target, fragment = documentation_path(
            code_root, arguments.get("path", "docs/AGENT_FYI.md"),
            from_path=arguments.get("from_path", "docs/AGENT_FYI.md"),
        )
        text, revision = read_document_bytes(target)
        expected = arguments.get("revision")
        if (offset and not expected) or (expected and expected != revision):
            raise DocumentationError("revision_changed", "Document revision changed or continuation has no revision; reread from offset zero.")
        selected = arguments.get("section") or fragment
        body, section_offset = section_text(text, selected)
        source = {
            "path": target.relative_to(code_root.resolve()).as_posix(),
            "sha256": revision, "section": selected or None,
            "line": text.count("\n", 0, section_offset) + 1,
        }
        if operation == "outline":
            outline = headings(body)
            if offset > len(outline):
                raise DocumentationError("invalid_arguments", "Outline continuation is beyond its headings.")
            page = outline[offset:offset + 40]
            end = min(len(outline), offset + len(page))
            payload = {"sections": page}
            complete = end == len(outline)
        else:
            if offset > len(body):
                raise DocumentationError("invalid_arguments", "Continuation is beyond the selected document section.")
            end = min(len(body), offset + maximum)
            if end < len(body):
                boundary = body.rfind("\n\n", offset, end)
                if boundary > offset + maximum // 2:
                    end = boundary + 2
            payload = {"text": body[offset:end]}
            complete = end == len(body)
        result = {"ok": True, "source": source, **payload, "complete": complete}
        if not complete:
            result["continuation"] = {
                "operation": operation, "path": source["path"], "section": selected or "",
                "offset": end, "max_chars": maximum, "revision": revision,
            }
            result["notice"] = "Partial document/section. Follow continuation before treating its conditions and exceptions as complete."
        return result
    except (DocumentationError, OSError, TypeError, ValueError) as exc:
        return {"ok": False, "error": getattr(exc, "code", "invalid_arguments"), "message": str(exc)}


def validate_knowledge_migration(root: Path) -> list[str]:
    path = root / "docs/agent-guide/fyi-v1-migration.json"
    try:
        inventory = json.loads(path.read_text(encoding="utf-8-sig"))
        baseline = inventory["baseline"]
        text = baseline["text"]
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != baseline["sha256"]:
            return ["migration: baseline content digest differs"]
        # Re-enumerate the original paragraphs/bullets instead of trusting a
        # manually shortened list of topics in the review inventory.
        original_blocks, pending = [], []
        def flush_block():
            if pending:
                original_blocks.append("\n".join(pending).strip())
                pending.clear()
        for line in text.splitlines():
            if not line.strip() or line.startswith("#"):
                flush_block()
                continue
            if line.startswith("- "):
                flush_block()
            pending.append(line)
        flush_block()
        if original_blocks != [block["text"] for block in baseline["blocks"]]:
            return ["migration: original paragraph inventory is incomplete"]
        records = inventory["records"]
        ids = [record["source_id"] for record in records]
        expected_ids = [block["id"] for block in baseline["blocks"]]
        if len(ids) != len(set(ids)) or sorted(ids) != sorted(expected_ids):
            return ["migration: original topics are missing or duplicated"]
        originals = {block["id"]: block["text"] for block in baseline["blocks"]}
        errors = _check_links(root, path.with_suffix(".md"), path.with_suffix(".md").read_text(encoding="utf-8"))
        for record in records:
            if record["original_text"] != originals[record["source_id"]]:
                errors.append(f"migration {record['source_id']}: original knowledge differs")
            target, fragment = documentation_path(root, record["destination"], from_path="docs/AGENT_FYI.md")
            destination, _ = read_document_bytes(target)
            section, _ = section_text(destination, fragment)
            if record["verification_text"] not in section:
                errors.append(f"migration {record['source_id']}: actual destination lost retained content")
            errors.extend(_check_links(root, target, record["verification_text"]))
        return errors
    except (KeyError, ValueError, OSError) as exc:
        return [f"migration: {exc}"]
# HASHI-Fingerprint: hfp_a07a96d4621b40a288ee856eee3323a5
