#!/usr/bin/env python3
"""
OneDrive Folder Memo Generator
Scans a directory tree and generates _memo.md in each folder.
Analyzes file metadata only — never reads file contents.
"""

import os
import sys
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

# ── Configuration ──────────────────────────────────────────────────────────────

MEMO_FILENAME = "_memo.md"
SKIP_DIRS = {".obsidian", ".git", "__pycache__", "System Volume Information"}
SKIP_FILES = {MEMO_FILENAME, "desktop.ini", ".DS_Store", "Thumbs.db"}

SENSITIVE_KEYWORDS = [
    "bank", "tax", "statement", "invoice", "receipt", "passport", "id",
    "license", "licence", "medicare", "tfn", "abn", "acn", "salary",
    "payslip", "super", "superfund", "insurance", "policy", "contract",
    "legal", "deed", "will", "trust", "nude", "private", "confidential",
    "password", "secret", "credit", "loan", "mortgage", "证件", "护照",
    "身份证", "税", "银行", "工资", "合同", "律师"
]

OLD_YEARS_THRESHOLD = 3  # years since last modified = "old"

EXT_CATEGORIES = {
    "📄 文档": {".pdf", ".doc", ".docx", ".odt", ".txt", ".rtf", ".pages"},
    "📊 表格": {".xls", ".xlsx", ".csv", ".numbers", ".ods"},
    "🖼️ 图片": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".heic", ".webp", ".svg"},
    "🎬 视频": {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v"},
    "🎵 音频": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
    "📦 压缩包": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "💻 代码": {".py", ".js", ".ts", ".html", ".css", ".json", ".xml", ".sh", ".bat"},
    "📧 邮件": {".eml", ".msg", ".mbox"},
    "🔗 链接": {".lnk", ".url", ".webloc"},
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def format_size(bytes_val):
    for unit in ["B", "KB", "MB", "GB"]:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"

def get_category(ext):
    ext = ext.lower()
    for cat, exts in EXT_CATEGORIES.items():
        if ext in exts:
            return cat
    return "📎 其他"

def is_sensitive(name):
    name_lower = name.lower()
    return any(kw in name_lower for kw in SENSITIVE_KEYWORDS)

def years_ago(ts):
    now = datetime.now(timezone.utc)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (now - dt).days / 365

def file_size_key(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0

# ── Core: scan one folder ──────────────────────────────────────────────────────

def scan_folder(folder_path: Path):
    files = []
    for item in folder_path.iterdir():
        if item.is_file() and item.name not in SKIP_FILES:
            try:
                stat = item.stat()
                files.append({
                    "name": item.name,
                    "ext": item.suffix.lower(),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "ctime": stat.st_ctime,
                    "sensitive": is_sensitive(item.name),
                    "old": years_ago(stat.st_mtime) >= OLD_YEARS_THRESHOLD,
                })
            except OSError:
                pass
    return files

def detect_duplicates(files):
    """Flag files with same name (case-insensitive) or same size as possible duplicates."""
    name_counts = defaultdict(list)
    size_counts = defaultdict(list)
    for f in files:
        name_counts[f["name"].lower()].append(f["name"])
        if f["size"] > 0:
            size_counts[f["size"]].append(f["name"])

    dup_names = {name for names in name_counts.values() if len(names) > 1 for name in names}
    dup_sizes = {name for names in size_counts.values() if len(names) > 1 for name in names}
    return dup_names | dup_sizes

# ── Core: generate markdown ────────────────────────────────────────────────────

def generate_memo(folder_path: Path, files, subfolders):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    rel_path = str(folder_path)

    # Stats
    total_size = sum(f["size"] for f in files)
    sensitive_files = [f for f in files if f["sensitive"]]
    old_files = [f for f in files if f["old"]]
    dup_names = detect_duplicates(files)
    dup_files = [f for f in files if f["name"] in dup_names]

    # Date range
    if files:
        oldest = min(files, key=lambda f: f["mtime"])
        newest = max(files, key=lambda f: f["mtime"])
        oldest_str = datetime.fromtimestamp(oldest["mtime"]).strftime("%Y-%m-%d")
        newest_str = datetime.fromtimestamp(newest["mtime"]).strftime("%Y-%m-%d")
        date_range = f"{oldest_str} → {newest_str}"
    else:
        date_range = "—"

    # Category breakdown
    cat_counts = defaultdict(int)
    for f in files:
        cat_counts[get_category(f["ext"])] += 1

    # Auto tags
    tags = []
    if sensitive_files:
        tags.append("⚠️ 疑似敏感")
    if old_files:
        tags.append("⏰ 含旧文件")
    if dup_files:
        tags.append("🔁 疑似重复")
    for cat, count in cat_counts.items():
        if count > 0:
            tags.append(cat)

    # ── Build markdown ─────────────────────────────────────────────────────────
    lines = []
    lines.append(f"# 📁 {folder_path.name}")
    lines.append(f"\n> 📍 `{rel_path}`")
    lines.append(f"> 🕐 生成时间：{now_str}\n")

    # Summary
    lines.append("## 概览\n")
    lines.append(f"| 项目 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 文件数 | {len(files)} |")
    lines.append(f"| 子文件夹数 | {len(subfolders)} |")
    lines.append(f"| 总大小 | {format_size(total_size)} |")
    lines.append(f"| 日期范围 | {date_range} |")
    if sensitive_files:
        lines.append(f"| ⚠️ 疑似敏感文件 | {len(sensitive_files)} |")
    if old_files:
        lines.append(f"| ⏰ 超过{OLD_YEARS_THRESHOLD}年未修改 | {len(old_files)} |")
    if dup_files:
        lines.append(f"| 🔁 疑似重复文件 | {len(dup_files)} |")
    lines.append("")

    # Tags
    if tags:
        lines.append("## 标签\n")
        lines.append(" ".join(f"`{t}`" for t in tags))
        lines.append("")

    # Category breakdown
    if cat_counts:
        lines.append("## 文件类型分布\n")
        for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {cat}：{count} 个")
        lines.append("")

    # Subfolders
    if subfolders:
        lines.append("## 子文件夹\n")
        for sf in sorted(subfolders):
            lines.append(f"- 📁 [{sf}](./{sf}/{MEMO_FILENAME})")
        lines.append("")

    # Sensitive files (highlighted)
    if sensitive_files:
        lines.append("## ⚠️ 疑似敏感文件（建议移入加密区）\n")
        for f in sorted(sensitive_files, key=lambda x: x["name"]):
            mtime = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d")
            lines.append(f"- `{f['name']}` — {format_size(f['size'])} — 修改：{mtime}")
        lines.append("")

    # Old files
    if old_files:
        lines.append(f"## ⏰ 旧文件（{OLD_YEARS_THRESHOLD}年以上未修改）\n")
        for f in sorted(old_files, key=lambda x: x["mtime"]):
            mtime = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d")
            lines.append(f"- `{f['name']}` — {format_size(f['size'])} — 最后修改：{mtime}")
        lines.append("")

    # Duplicate suspects
    if dup_files:
        lines.append("## 🔁 疑似重复文件\n")
        for f in sorted(dup_files, key=lambda x: x["name"]):
            lines.append(f"- `{f['name']}` — {format_size(f['size'])}")
        lines.append("")

    # Full file list
    lines.append("## 完整文件列表\n")
    lines.append("| 文件名 | 类型 | 大小 | 最后修改 |")
    lines.append("|--------|------|------|---------|")
    for f in sorted(files, key=lambda x: x["name"].lower()):
        mtime = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d")
        cat = get_category(f["ext"])
        flags = ""
        if f["sensitive"]: flags += "⚠️"
        if f["old"]: flags += "⏰"
        if f["name"] in dup_names: flags += "🔁"
        name_col = f"{flags} `{f['name']}`" if flags else f"`{f['name']}`"
        lines.append(f"| {name_col} | {cat} | {format_size(f['size'])} | {mtime} |")
    lines.append("")

    lines.append("---")
    lines.append(f"*由 HASHI OneDrive Memo Generator 自动生成 · {now_str}*")

    return "\n".join(lines)

# ── Walk and generate ──────────────────────────────────────────────────────────

def process_tree(root: Path, dry_run=False):
    total_folders = 0
    total_files = 0

    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden/system dirs
        dirnames[:] = [d for d in sorted(dirnames) if d not in SKIP_DIRS and not d.startswith(".")]

        folder = Path(dirpath)
        files = scan_folder(folder)
        subfolders = [d for d in dirnames]

        memo_content = generate_memo(folder, files, subfolders)
        memo_path = folder / MEMO_FILENAME

        total_folders += 1
        total_files += len(files)

        if dry_run:
            print(f"  [DRY RUN] Would write: {memo_path}")
        else:
            try:
                memo_path.write_text(memo_content, encoding="utf-8")
                flag_count = sum(1 for f in files if f["sensitive"] or f["old"])
                status = f"⚠️ {flag_count} flagged" if flag_count else "✅"
                print(f"  {status}  {folder.name}/ ({len(files)} files)")
            except OSError as e:
                print(f"  ❌ 无法写入 {memo_path}: {e}")

    return total_folders, total_files

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "/mnt/c/Users/thene/OneDrive - The University Of Newcastle/个人资料/Z. Archive"
    )

    dry_run = "--dry-run" in sys.argv

    if not target.exists():
        print(f"❌ 路径不存在: {target}")
        sys.exit(1)

    mode = "DRY RUN 模式" if dry_run else "写入模式"
    print(f"\n🔍 OneDrive Memo Generator — {mode}")
    print(f"📁 目标：{target}\n")

    folders, files = process_tree(target, dry_run=dry_run)

    print(f"\n✅ 完成！")
    print(f"   扫描文件夹：{folders}")
    print(f"   扫描文件：  {files}")
    if not dry_run:
        print(f"   已在每个文件夹生成 {MEMO_FILENAME}")
    print()
