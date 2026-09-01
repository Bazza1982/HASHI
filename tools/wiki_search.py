"""Read-only retrieval for an instance-configured curated Wiki.

The core tool contains no deployment path or knowledge data.  Each HASHI
instance opts in through ``global.wiki_provider`` and supplies the vault root
and the relative zones that are safe to expose.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


DEFAULT_ZONES = (
    "30_GENERATED_INDEXES",
    "10_GENERATED_TOPICS",
    "Projects",
    "Agents",
    "Topics",
    "Daily",
)
DEFAULT_MAX_FILES = 2500
DEFAULT_MAX_FILE_BYTES = 1_048_576
DEFAULT_SCAN_CHARS = 180_000
DEFAULT_RESULT_LIMIT = 8
DEFAULT_OUTPUT_CHARS = 12_000


def _provider_config(global_config: Any) -> dict[str, Any]:
    if isinstance(global_config, Mapping):
        raw = global_config.get("wiki_provider")
    else:
        raw = getattr(global_config, "wiki_provider", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _relative_zone(value: Any) -> Path | None:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    return candidate


def _configured_roots(provider: Mapping[str, Any]) -> tuple[Path, tuple[Path, ...]]:
    raw_root = str(provider.get("root") or provider.get("vault_root") or "").strip()
    if not raw_root:
        raise ValueError("configured Wiki root is missing")
    root = Path(raw_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("configured Wiki root is unavailable")

    raw_zones = provider.get("zones") or DEFAULT_ZONES
    if isinstance(raw_zones, (str, bytes)):
        raw_zones = [raw_zones]
    if not isinstance(raw_zones, Sequence):
        raise ValueError("configured Wiki zones are invalid")

    zones: list[Path] = []
    for value in raw_zones:
        relative = _relative_zone(value)
        if relative is None:
            continue
        zone = (root / relative).resolve()
        if zone != root and not zone.is_relative_to(root):
            continue
        if zone.is_dir() and zone not in zones:
            zones.append(zone)
    if not zones:
        raise ValueError("configured Wiki has no readable zones")
    return root, tuple(zones)


def _inside_zones(path: Path, zones: Sequence[Path]) -> bool:
    return any(path == zone or path.is_relative_to(zone) for zone in zones)


def _safe_markdown_path(root: Path, zones: Sequence[Path], raw_path: Any) -> Path:
    relative = _relative_zone(raw_path)
    if relative is None or relative.suffix.casefold() != ".md":
        raise ValueError("Wiki source must be a relative Markdown path")
    resolved = (root / relative).resolve()
    if not resolved.is_file() or not _inside_zones(resolved, zones):
        raise ValueError("Wiki source is unavailable or outside configured zones")
    return resolved


def _query_terms(query: str) -> list[str]:
    normalized = " ".join(query.casefold().split())
    terms = [normalized]
    for term in re.findall(r"[^\W_]+(?:[-_][^\W_]+)*", normalized, re.UNICODE):
        if len(term) >= 2 and term not in terms:
            terms.append(term)
    return terms


def _excerpt(text: str, terms: Sequence[str], *, width: int = 620) -> str:
    folded = text.casefold()
    positions = [folded.find(term) for term in terms if term and folded.find(term) >= 0]
    position = min(positions) if positions else 0
    start = max(0, position - width // 3)
    end = min(len(text), start + width)
    excerpt = " ".join(text[start:end].split())
    if start:
        excerpt = "…" + excerpt
    if end < len(text):
        excerpt += "…"
    return excerpt


def _candidate_files(
    zones: Sequence[Path],
    *,
    max_files: int,
) -> list[Path]:
    files: list[Path] = []
    for zone in zones:
        for candidate in sorted(zone.rglob("*.md")):
            if len(files) >= max_files:
                return files
            if candidate.is_symlink() or any(part.startswith(".") for part in candidate.parts):
                continue
            resolved = candidate.resolve()
            if resolved.is_file() and _inside_zones(resolved, zones):
                files.append(resolved)
    return files


def _rg_candidates(
    root: Path,
    zones: Sequence[Path],
    *,
    max_files: int,
) -> list[tuple[str, Path]]:
    relative_zones = [zone.relative_to(root).as_posix() for zone in zones]
    completed = subprocess.run(
        ["rg", "--files", "--glob", "*.md", "--", *relative_zones],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise OSError("configured Wiki file index could not be read")

    candidates: list[tuple[str, Path]] = []
    for raw in sorted(set(completed.stdout.splitlines()), key=str.casefold):
        relative = _relative_zone(raw)
        if relative is None or relative.suffix.casefold() != ".md":
            continue
        candidate = root / relative
        # ``rg --files`` does not follow symlinks by default.  Keep this path
        # check lexical so a Windows-mounted Wiki does not incur one stat call
        # per page; read operations still resolve and re-check their target.
        if not _inside_zones(candidate, zones):
            continue
        candidates.append((relative.as_posix(), candidate))
        if len(candidates) >= max_files:
            break
    return candidates


def _rg_matches(
    root: Path,
    candidates: Sequence[tuple[str, Path]],
    terms: Sequence[str],
    *,
    max_file_bytes: int,
) -> dict[str, list[str]]:
    pattern = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    matches: dict[str, list[str]] = {}
    for offset in range(0, len(candidates), 180):
        chunk = [relative for relative, _path in candidates[offset : offset + 180]]
        completed = subprocess.run(
            [
                "rg",
                "--json",
                "--ignore-case",
                "--max-count",
                "8",
                "--max-columns",
                "2000",
                "--max-columns-preview",
                "--max-filesize",
                str(max_file_bytes),
                "--",
                pattern,
                *chunk,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if completed.returncode not in {0, 1}:
            raise OSError("configured Wiki search failed")
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            source = str((data.get("path") or {}).get("text") or "").strip()
            content = str((data.get("lines") or {}).get("text") or "").strip()
            if source and content:
                matches.setdefault(source, []).append(content)
    return matches


def _rank_rg_results(
    candidates: Sequence[tuple[str, Path]],
    matches: Mapping[str, Sequence[str]],
    terms: Sequence[str],
) -> list[tuple[int, str, str]]:
    exact = terms[0]
    ranked: list[tuple[int, str, str]] = []
    for relative, _path in candidates:
        name = relative.casefold().replace("_", " ")
        lines = list(matches.get(relative) or [])
        folded = " ".join(lines).casefold()
        score = 0
        if exact in name:
            score += 80
        if exact in folded:
            score += 45 + min(folded.count(exact), 5) * 3
        for term in terms[1:]:
            if term in name:
                score += 16
            score += min(folded.count(term), 5) * 4
        if relative.startswith("30_GENERATED_INDEXES/"):
            score += 3
        if score:
            excerpt_source = " ".join(lines) or relative.replace("_", " ")
            ranked.append((score, relative, _excerpt(excerpt_source, terms)))
    ranked.sort(key=lambda item: (-item[0], item[1].casefold()))
    return ranked


def _search(
    *,
    provider_id: str,
    provider: Mapping[str, Any],
    root: Path,
    zones: Sequence[Path],
    arguments: Mapping[str, Any],
) -> str:
    query = " ".join(str(arguments.get("query") or "").split()).strip()
    if not query:
        raise ValueError("wiki_search requires a non-empty query")
    if len(query) > 500:
        raise ValueError("wiki_search query exceeds 500 characters")

    limit = _bounded_int(
        arguments.get("limit"), default=DEFAULT_RESULT_LIMIT, minimum=1, maximum=12
    )
    max_chars = _bounded_int(
        arguments.get("max_chars"),
        default=DEFAULT_OUTPUT_CHARS,
        minimum=1000,
        maximum=24_000,
    )
    max_files = _bounded_int(
        provider.get("max_files"),
        default=DEFAULT_MAX_FILES,
        minimum=1,
        maximum=10_000,
    )
    max_file_bytes = _bounded_int(
        provider.get("max_file_bytes"),
        default=DEFAULT_MAX_FILE_BYTES,
        minimum=1024,
        maximum=5_000_000,
    )
    terms = _query_terms(query)
    exact = terms[0]
    ranked: list[tuple[int, str, str]] = []

    if shutil.which("rg"):
        rg_candidates = _rg_candidates(root, zones, max_files=max_files)
        ranked = _rank_rg_results(
            rg_candidates,
            _rg_matches(
                root,
                rg_candidates,
                terms,
                max_file_bytes=max_file_bytes,
            ),
            terms,
        )
        searched_files = len(rg_candidates)
    else:
        scan_chars = _bounded_int(
            provider.get("scan_chars"),
            default=DEFAULT_SCAN_CHARS,
            minimum=10_000,
            maximum=1_000_000,
        )
        candidates = _candidate_files(zones, max_files=max_files)
        searched_files = len(candidates)
        for path in candidates:
            try:
                if path.stat().st_size > max_file_bytes:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")[:scan_chars]
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            name = relative.casefold().replace("_", " ")
            folded = text.casefold()
            score = 0
            if exact in name:
                score += 80
            if exact in folded:
                score += 45 + min(folded.count(exact), 5) * 3
            for term in terms[1:]:
                if term in name:
                    score += 16
                count = folded.count(term)
                score += min(count, 5) * 4
            if relative.startswith("30_GENERATED_INDEXES/"):
                score += 3
            if score:
                ranked.append((score, relative, _excerpt(text, terms)))
        ranked.sort(key=lambda item: (-item[0], item[1].casefold()))
    results: list[dict[str, Any]] = []
    used = 0
    for score, source, excerpt in ranked[:limit]:
        remaining = max_chars - used
        if remaining <= 180:
            break
        clipped = excerpt[: max(120, min(len(excerpt), remaining - 100))]
        results.append({"source": source, "score": score, "excerpt": clipped})
        used += len(source) + len(clipped) + 80

    return json.dumps(
        {
            "provider_id": provider_id,
            "operation": "search",
            "query": query,
            "result_count": len(results),
            "results": results,
            "searched_files": searched_files,
            "truncated": len(ranked) > len(results),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _read(
    *,
    provider_id: str,
    root: Path,
    zones: Sequence[Path],
    arguments: Mapping[str, Any],
) -> str:
    path = _safe_markdown_path(root, zones, arguments.get("path"))
    max_chars = _bounded_int(
        arguments.get("max_chars"),
        default=DEFAULT_OUTPUT_CHARS,
        minimum=1000,
        maximum=24_000,
    )
    text = path.read_text(encoding="utf-8", errors="replace")
    content = text[:max_chars]
    return json.dumps(
        {
            "provider_id": provider_id,
            "operation": "read",
            "source": path.relative_to(root).as_posix(),
            "content": content,
            "truncated": len(text) > len(content),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _execute(arguments: Mapping[str, Any], global_config: Any) -> str:
    provider = _provider_config(global_config)
    provider_id = str(provider.get("id") or "").strip()
    capability = str(provider.get("capability") or "").strip()
    if not provider_id or capability != "wiki_search":
        raise ValueError("wiki_search is not configured for this HASHI instance")
    root, zones = _configured_roots(provider)
    operation = str(arguments.get("operation") or "search").strip().casefold()
    if operation == "search":
        return _search(
            provider_id=provider_id,
            provider=provider,
            root=root,
            zones=zones,
            arguments=arguments,
        )
    if operation == "read":
        return _read(
            provider_id=provider_id,
            root=root,
            zones=zones,
            arguments=arguments,
        )
    raise ValueError("wiki_search operation must be 'search' or 'read'")


async def execute_wiki_search(arguments: Mapping[str, Any], *, global_config: Any) -> str:
    """Search or read the configured curated Wiki without exposing its root."""

    try:
        return await asyncio.to_thread(_execute, arguments, global_config)
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"
