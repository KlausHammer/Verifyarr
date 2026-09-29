"""Backup/quarantine — the non-destructive undo mechanism. See README's "Undoing something".
Also the one place a brand-new subtitle file (see generate.py) gets written to the media
folder — write_new_subtitle below."""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from verifyarr.settings import LANG_CODE_RE

_TS_SUFFIX_RE = re.compile(r"\.\d{8}T\d{6}Z$")
_TS_ORIG_SUFFIX_RE = re.compile(r"\.\d{8}T\d{6}Z\.orig$")


class SubtitleAlreadyExists(Exception):
    """Raised by write_new_subtitle when a file already sits at the destination -- e.g. Bazarr's
    own poll or a concurrent sweep beat generation to it. Never silently overwritten: an existing
    file might be a real (if unverified) subtitle, not a placeholder."""


def match_reference_ownership(dest: Path, reference: Path) -> None:
    """Best-effort: new file takes the reference file's owner and rw-bits. Never raises."""
    try:
        st = reference.stat()
    except OSError:
        return
    try:
        os.chown(dest, st.st_uid, st.st_gid)
    except OSError:
        pass
    try:
        # 0o666, not 0o777: videos on SMB/NTFS are often 0777, subtitles need no exec bit.
        os.chmod(dest, st.st_mode & 0o666)
    except OSError:
        pass


def write_new_subtitle(subs: "pysubs2.SSAFile", video_path: Path, lang: str) -> Path:
    """Atomically writes a freshly GENERATED subtitle next to the video, as
    '<video_stem>.<lang>.srt' -- the exact naming discovery.find_subtitles_for_video /
    parse_lang_from_filename already expect, so the very next sweep's discover_pairs() picks it
    up with no special-casing at all. Writes to a temp file in the same directory first, then
    links it into place -- a crash or a cancelled job mid-write can never leave a half-written
    .srt where discovery would find it.

    `lang` is validated, not just interpolated: it becomes part of a filename, and anything that
    isn't a plain language code produces a file this app's own discovery can never match back to
    its video (Path.with_name does reject a separator outright, so this is about correctness of
    the result rather than about escaping a directory).

    os.link, not os.replace: the destination must never be overwritten -- an existing file there
    may be a real, if unverified, subtitle. A plain exists() check followed by a write is a race
    (Bazarr's own poll downloading into that exact path in between is the realistic case, and it
    is exactly what SubtitleAlreadyExists is for), whereas link fails atomically if the name is
    taken. It needs hardlink support, which a few network filesystems lack; the exists()+replace
    path is kept as the fallback for those, with its race still narrowed to the final instant."""
    if not LANG_CODE_RE.match((lang or "").lower()):
        raise ValueError(f"not a usable subtitle language code: {lang!r}")
    lang = lang.lower()
    dest = video_path.with_name(f"{video_path.stem}.{lang}.srt")
    if dest.exists():
        raise SubtitleAlreadyExists(f"subtitle already exists: {dest}")
    tmp = dest.with_suffix(dest.suffix + ".generating.tmp")
    # format_ passed explicitly -- pysubs2 otherwise infers format from the file extension,
    # which would be ".tmp" here and fail (see pysubs2.formats.get_format_identifier).
    subs.save(str(tmp), format_="srt")
    try:
        try:
            os.link(tmp, dest)
        except FileExistsError:
            raise SubtitleAlreadyExists(f"subtitle already exists: {dest}")
        except (OSError, NotImplementedError, AttributeError):
            if dest.exists():
                raise SubtitleAlreadyExists(f"subtitle already exists: {dest}")
            os.replace(tmp, dest)
            match_reference_ownership(dest, video_path)
            return dest
    finally:
        tmp.unlink(missing_ok=True)
    match_reference_ownership(dest, video_path)
    return dest


def _relative_to_root(path: Path, root: Path) -> Path:
    """Path relative to the media root, or just the bare filename when it isn't under
    the root at all (e.g. an absolute path from a different mount) — archiving must
    never fail on the location math, only mirror structure when it can."""
    try:
        return path.relative_to(root)
    except ValueError:
        return Path(path.name)


def _utc_timestamp() -> str:
    """Compact UTC timestamp used in archived filenames (also the shape _original_name
    reverses on restore). One format string for every writer, so the regexes above
    always match what the writers produce."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _store_archived(subtitle_path: Path, archive_dir: Path, media_root: Path,
                    infix: str, move: bool) -> Path:
    """Copy (backup) or move (quarantine) a subtitle into the archive tree, preserving
    its relative location and stamping the filename — the single implementation behind
    backup_subtitle and quarantine_subtitle, which differ only in infix and copy/move."""
    rel = _relative_to_root(subtitle_path, media_root)
    dest = archive_dir / rel.parent / f"{subtitle_path.stem}.{_utc_timestamp()}{infix}{subtitle_path.suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(str(subtitle_path), str(dest))
    else:
        shutil.copyfile(subtitle_path, dest)
        # Keep the original's rw-bits on the copy (not copy2: mtime is the sort key).
        try:
            os.chmod(dest, subtitle_path.stat().st_mode & 0o666)
        except OSError:
            pass
    return dest


def backup_subtitle(subtitle_path: Path, backup_dir: Path, media_root: Path) -> None:
    # Return value deliberately discarded — this has always returned None, and callers
    # (and tests) may rely on that; only quarantine_subtitle hands the dest back.
    _store_archived(subtitle_path, backup_dir, media_root, ".orig", move=False)


def quarantine_subtitle(subtitle_path: Path, quarantine_dir: Path, media_root: Path) -> Path:
    return _store_archived(subtitle_path, quarantine_dir, media_root, "", move=True)


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


def _restore_target(archive_dir: Path, rel_path: str, media_root: Path,
                     is_backup: bool, archive_label: str) -> tuple[Path, Path]:
    """Locate an archived file and compute where it restores to. Missing source and
    occupied destination are both hard errors, so a restore can never silently lose a
    newer file. Used by quarantine restore; backup restore resolves its own target
    instead because an occupied destination means "back up first", not "refuse"."""
    src = archive_dir / rel_path
    if not src.is_file():
        raise FileNotFoundError(f"not found in {archive_label}: {rel_path}")
    target = media_root / Path(rel_path).parent / _original_name(src.stem, src.suffix, is_backup)
    if target.exists():
        raise FileExistsError(f"a file already exists at the destination: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    return src, target


def restore_from_quarantine(rel_path: str, quarantine_dir: Path, media_root: Path) -> Path:
    """Moves a quarantined file back to its original relative location under media_root.
    Refuses to overwrite a file already there — remove it first, so a newer file is never
    silently lost."""
    src, target = _restore_target(quarantine_dir, rel_path, media_root,
                                  is_backup=False, archive_label="quarantine")
    shutil.move(str(src), str(target))
    return target


def restore_from_backup(rel_path: str, backup_dir: Path, media_root: Path) -> Path:
    """Copies a backup (the ORIGINAL, pre-sync version) back over the current file at its
    original location. The current file is backed up first if it exists, so undoing is
    never itself irreversible."""
    # Note: unlike quarantine restore, an occupied destination is NOT an error here — the
    # current file is backed up first (see below), which is exactly why _restore_target's
    # exists-check can't be shared and this resolves its own target instead.
    src = backup_dir / rel_path
    if not src.is_file():
        raise FileNotFoundError(f"not found in backups: {rel_path}")
    target = media_root / Path(rel_path).parent / _original_name(src.stem, src.suffix, is_backup=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup_subtitle(target, backup_dir, media_root)
    shutil.copyfile(src, target)
    # Put the archived rw-bits back so a restore keeps its old access.
    match_reference_ownership(target, src)
    return target
