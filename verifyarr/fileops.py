"""Backup/quarantine — the non-destructive undo mechanism. See README's "Undoing something"."""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

_TS_SUFFIX_RE = re.compile(r"\.\d{8}T\d{6}Z$")
_TS_ORIG_SUFFIX_RE = re.compile(r"\.\d{8}T\d{6}Z\.orig$")


def backup_subtitle(subtitle_path: Path, backup_dir: Path, media_root: Path) -> None:
    try:
        rel = subtitle_path.relative_to(media_root)
    except ValueError:
        rel = Path(subtitle_path.name)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backup_dir / rel.parent / f"{subtitle_path.stem}.{ts}.orig{subtitle_path.suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(subtitle_path, dest)


def quarantine_subtitle(subtitle_path: Path, quarantine_dir: Path, media_root: Path) -> Path:
    try:
        rel = subtitle_path.relative_to(media_root)
    except ValueError:
        rel = Path(subtitle_path.name)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = quarantine_dir / rel.parent / f"{subtitle_path.stem}.{ts}{subtitle_path.suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(subtitle_path), str(dest))
    return dest


def _original_name(stem: str, suffix: str, is_backup: bool) -> str:
    """Reconstructs the filename before backup_subtitle/quarantine_subtitle added a
    timestamp (+'.orig' for backups) — i.e. reverses that naming."""
    pattern = _TS_ORIG_SUFFIX_RE if is_backup else _TS_SUFFIX_RE
    return pattern.sub("", stem) + suffix


def list_archived(root_dir: Path, is_backup: bool) -> list[dict]:
    """Everything under backup_dir/quarantine_dir, newest first — used by the webapp's
    quarantine/backup browser."""
    items = []
    if not root_dir.exists():
        return items
    for p in root_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root_dir)
        st = p.stat()
        items.append({
            "path": str(rel),
            "original_name": _original_name(p.stem, p.suffix, is_backup),
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    items.sort(key=lambda x: -x["mtime"])
    return items


def restore_from_quarantine(rel_path: str, quarantine_dir: Path, media_root: Path) -> Path:
    """Moves a quarantined file back to its original relative location under media_root.
    Refuses to overwrite a file already there — remove it first, so a newer file is never
    silently lost."""
    src = quarantine_dir / rel_path
    if not src.is_file():
        raise FileNotFoundError(f"not found in quarantine: {rel_path}")
    target = media_root / Path(rel_path).parent / _original_name(src.stem, src.suffix, is_backup=False)
    if target.exists():
        raise FileExistsError(f"a file already exists at the destination: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(target))
    return target


def restore_from_backup(rel_path: str, backup_dir: Path, media_root: Path) -> Path:
    """Copies a backup (the ORIGINAL, pre-sync version) back over the current file at its
    original location. The current file is backed up first if it exists, so undoing is
    never itself irreversible."""
    src = backup_dir / rel_path
    if not src.is_file():
        raise FileNotFoundError(f"not found in backups: {rel_path}")
    target = media_root / Path(rel_path).parent / _original_name(src.stem, src.suffix, is_backup=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup_subtitle(target, backup_dir, media_root)
    shutil.copyfile(src, target)
    return target
