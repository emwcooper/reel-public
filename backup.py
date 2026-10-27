"""Reel's backup to Google Drive, through Google Drive for desktop.

Drive for desktop keeps a folder on the Mac in step with your Drive
(~/Library/CloudStorage/GoogleDrive-<you>/My Drive), so backing up is copying
into that folder: Reel needs no Google sign-in of its own, and Drive uploads in
the background, picking up where it left off after the Mac was offline. Any
other folder works the same way (an external disk, another sync service).

What goes, for each finished recording, into
<folder>/Reel backup/<library folder>/<recording>/:

  item.json, transcript.json, clean.json   title, flags, speaker names; the
                                           transcript and its clean-up
  speakers.npz                             the mic levels a relabel needs
  audio.*                                  what the player plays (the mix of a
                                           multi-mic recording)
  tracks/, originals/                      only with "each mic's own files" on:
                                           large, but what a remix, adding a mic
                                           or uncombining start from

The denoised copy is left out: it is made again from the audio. Nothing in the
backup is ever deleted: a file that changed is copied again over its backup
(Drive keeps earlier versions for 30 days), and an archived recording stays.
"""

import json
import os
import shutil
import time
from pathlib import Path

BACKUP_DIR = "Reel backup"
DRIVE_DOWNLOAD = "https://www.google.com/drive/download/"
TOP_FILES = ("item.json", "transcript.json", "clean.json", "speakers.npz")
# In the data folder: each backed-up file's size and modification time when it
# was copied, so an unchanged file isn't copied again.
MANIFEST = ".backup.json"


class BackupError(Exception):
    pass


# Drive for desktop names an account's folders in the Mac's language: "My
# Drive" is the one to back up into; the roots of shared drives and other
# computers take no folders of their own.
MY_DRIVE = {"My Drive", "Meine Ablage", "Mon Drive", "Mi unidad", "Il mio Drive", "Meu Drive", "Mijn Drive",
            "Min enhet", "Mit drev", "Min disk", "Oma Drive", "Mój dysk", "Мой диск", "マイドライブ", "내 드라이브",
            "我的云端硬盘", "我的雲端硬碟"}
NOT_MY_DRIVE = {"Shared drives", "Other computers", "Geteilte Ablagen", "Andere Computer", "Drive partagés",
                "Autres ordinateurs", "Unidades compartidas", "Otros ordenadores", "Drive condivisi", "Altri computer",
                "Drives compartilhados", "Outros computadores", "Gedeelde drives", "Andere computers", "共有ドライブ",
                "その他のパソコン", "공유 드라이브", "共享云端硬盘", "共用雲端硬碟"}


def drive_folders():
    """The "My Drive" folders of Google Drive for desktop on this Mac, one per
    signed-in account (and where its predecessors, Drive File Stream and
    Backup and Sync, kept theirs). An account whose My Drive goes by a name not
    known here offers all its folders, the known non-starters last."""
    home = Path.home()
    found = []
    try:
        accounts = sorted((home / "Library" / "CloudStorage").glob("GoogleDrive-*"))
        for acct in accounts:
            subs = [d for d in sorted(acct.iterdir()) if d.is_dir() and not d.name.startswith(".")]
            mine = [d for d in subs if d.name in MY_DRIVE]
            found += mine or sorted(subs, key=lambda d: d.name in NOT_MY_DRIVE)
    except OSError:
        pass
    for old in [*sorted(Path("/Volumes").glob("GoogleDrive*/My Drive")), home / "Google Drive"]:
        if old.is_dir():
            found.append(old)
    if os.name == "nt":
        # Drive for desktop on Windows: streamed as a drive letter (G: by default) with
        # My Drive at its top, or mirrored into a My Drive folder in the home folder.
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            try:
                found += [d for d in Path(f"{letter}:/").iterdir() if d.is_dir() and d.name in MY_DRIVE]
            except OSError:
                continue
        found += [home / n for n in sorted(MY_DRIVE) if (home / n).is_dir()]
    return [str(p) for p in found]


def recording_files(d, tracks=False):
    """(file, path inside the recording's backup) for what a recording's backup holds."""
    out = [(d / n, n) for n in TOP_FILES if (d / n).is_file()]
    out += [(p, p.name) for p in sorted(d.glob("audio.*")) if p.is_file() and not p.name.endswith(".tmp")]
    if tracks:
        for sub in ("tracks", "originals"):
            out += [(p, p.relative_to(d).as_posix()) for p in sorted((d / sub).rglob("*"))
                    if p.is_file() and not p.name.startswith(".") and not p.name.endswith(".tmp")]
    return out


def _load(data):
    try:
        manifest = json.loads((Path(data) / MANIFEST).read_text())
    except (OSError, ValueError):
        manifest = {}
    return manifest if isinstance(manifest, dict) else {}


def _save(data, manifest):
    path = Path(data) / MANIFEST
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(manifest))
    tmp.replace(path)


def last_run(data):
    """When the last backup round finished (epoch seconds), or None."""
    return _load(data).get("at")


def run(data, dest, recordings, tracks=False):
    """Copy what's new or changed of `recordings` — [(library folder name,
    recording folder)] — into dest/Reel backup/. Returns (files copied, bytes
    copied). Raises BackupError."""
    dest = Path(dest).expanduser()
    if not dest.is_dir():
        raise BackupError(f"The backup folder {dest} isn't there (is Google Drive for desktop running?)")
    root = dest / BACKUP_DIR
    manifest = _load(data)
    done = manifest.setdefault("files", {})
    copied = size = 0
    try:
        for group, d in recordings:
            for src, rel in recording_files(d, tracks):
                dst = root / group / d.name / rel
                try:
                    st = src.stat()
                except OSError:
                    continue  # gone meanwhile (archived, moved)
                stamp = [st.st_size, st.st_mtime_ns]
                if done.get(str(dst)) == stamp and dst.exists():
                    continue
                tmp = dst.with_name(f".{dst.name}.reel-tmp")  # Drive never sees half a file under the real name
                try:
                    # Folder by folder below dest, never dest itself: should Drive quit
                    # mid-round, its folder must not come back as a plain local one.
                    folder = dest
                    for part in dst.parent.relative_to(dest).parts:
                        folder = folder / part
                        folder.mkdir(exist_ok=True)
                    shutil.copy2(src, tmp)
                    os.replace(tmp, dst)
                except FileNotFoundError as err:
                    tmp.unlink(missing_ok=True)
                    if not dest.is_dir():
                        raise BackupError(f"The backup folder {dest} went away (did Google Drive for desktop quit?)")
                    if src.exists():
                        raise BackupError(f"Couldn't copy {src.name} to {dst.parent}: {err}")
                    continue  # the source went away mid-copy (archived, moved)
                except OSError as err:
                    tmp.unlink(missing_ok=True)
                    raise BackupError(f"Couldn't copy {src.name} to {dst.parent}: {err}")
                done[str(dst)] = stamp
                copied += 1
                size += st.st_size
        manifest["at"] = time.time()
    finally:
        _save(data, manifest)
    return copied, size
