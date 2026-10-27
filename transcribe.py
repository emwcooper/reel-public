#!/usr/bin/env python3
"""Reel — transcribe audio with ElevenLabs Scribe v2, then click any word to jump the audio.

Usage:
  python3 transcribe.py                 # start the viewer at http://127.0.0.1:8765
  python3 transcribe.py a.mp3 b.m4a     # queue files for transcription, then start the viewer
  python3 transcribe.py --group A.WAV B.WAV   # one conversation recorded by several mics
  python3 transcribe.py --ingest        # sort out everything waiting in inbox/ right now
  python3 transcribe.py --regroup A B   # rebuild one conversation from recordings A and B (library ids)
  python3 transcribe.py --remix A       # redo alignment/mix/speaker labels of recording A, no transcription
  python3 transcribe.py --no-browser    # don't auto-open the browser

Multi-mic conversations (one DJI-style recorder per person): drop all the
recorders' files into inbox/ — files that belong to the same conversation are
matched by their audio, aligned, auto-mixed into one track, and the transcript's
speakers come from whichever mic carried each word. See README.md.

Needs only the Python standard library (multi-mic support also needs numpy).
API keys: ELEVENLABS_API_KEY (and ANTHROPIC_API_KEY for titles and the
clean-up), pasted into ⚙ SETTINGS in the app, which keeps them in a .env file
next to this script (gitignored), or set in the environment. See README.md for
the one-time privacy setup (opt out of ElevenLabs' model training).
"""

try:
    import fcntl
except ImportError:  # Windows: no fcntl; see claim_library and _full_sync
    fcntl = None
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if sys.version_info < (3, 9):
    raise SystemExit(f"Reel needs Python 3.9 or newer; {sys.executable} is {sys.version.split()[0]}")
if sys.platform == "win32":  # consoles and pipes there default to cp1252, and Reel prints ⚠, ⚙, …
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
ROOT = Path(__file__).resolve().parent
# Local settings and secrets: KEY=value lines in .env next to this script
# (gitignored) — ELEVENLABS_API_KEY and ANTHROPIC_API_KEY, which ⚙ SETTINGS
# writes, and any REEL_* setting. The real environment wins over the file.
ENV_FILE = ROOT / ".env"
KEY_VARS = {"elevenlabs": "ELEVENLABS_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
KEYS_FROM_ENV = {v for v in KEY_VARS.values() if os.environ.get(v)}  # set before .env was read


def _load_env_file(path=ENV_FILE):
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(name.strip(), value)


_load_env_file()
# All data lives under one folder, ~/Reel Recordings unless REEL_DATA says
# otherwise (never the code's own folder: see check_data_folder), with the
# recordings filed in folders of your choosing:
#   <DATA>/general/<recording>    where new recordings land (REEL_GROUP picks another)
#   <DATA>/<folder>/<recording>   any folder you move recordings into
#   <DATA>/inbox                  recorder files waiting to be sorted
#   <DATA>/archive                deleted recordings and inbox files
DATA = Path(os.environ.get("REEL_DATA") or Path.home() / "Reel Recordings").expanduser()
STATIC = ROOT / "static"
INBOX = Path(os.environ.get("REEL_INBOX") or DATA / "inbox").expanduser()
ARCHIVE_DIR = "archive"
ARCHIVE = Path(os.environ.get("REEL_ARCHIVE") or DATA / ARCHIVE_DIR).expanduser()
DEFAULT_GROUP = os.environ.get("REEL_GROUP", "general")   # where new recordings go
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
LIBRARY = DATA  # caches, staging and the run lock live at the top of the data folder
STARTED = time.time()
CODE_FILES = [ROOT / "transcribe.py", ROOT / "multitrack.py", ROOT / "backup.py"]


def code_version():
    """Short git commit of the code on disk, if this is a checkout."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


CODE_VERSION = code_version()


def code_changed_on_disk():
    """True when the server code files were modified after this process
    started — e.g. by `git pull` — so the running Reel is no longer what is on
    disk and needs a restart."""
    try:
        return any(f.stat().st_mtime > STARTED + 1 for f in CODE_FILES)
    except OSError:
        return False


def place_dir(group):
    """The folder holding the recordings filed under `group`."""
    return DATA / (group or DEFAULT_GROUP)


def item_dir(item):
    """A recording's folder. Accepts the item dict or its id (an unknown id is
    assumed to be in the default folder)."""
    if isinstance(item, str):
        item = ITEMS.get(item) or {"id": item}
    return place_dir(item.get("group")) / item["id"]


def archive_path(name, sub=None):
    """A free path for `name` in the archive — an earlier archive of the same
    name is never overwritten (same suffixing as inbox/skipped/)."""
    root = ARCHIVE / sub if sub else ARCHIVE
    root.mkdir(parents=True, exist_ok=True)
    dst = root / name
    stem, suffix = Path(name).stem, Path(name).suffix
    while dst.exists():
        dst = root / f"{stem}_{os.urandom(2).hex()}{suffix}"
    return dst


def archive_item(item):
    """Move a recording out of its folder and into the archive, returning
    where it went (None if its folder was already gone). The archive sits
    beside the folders (like inbox/), so groups() skips it, and nothing is
    ever deleted: empty it by hand."""
    src = item_dir(item)
    if not src.is_dir():
        return None
    dst = archive_path(item["id"])
    shutil.move(str(src), str(dst))
    return dst


def groups():
    """Names of the folders that exist, sorted."""
    names = set()
    if DATA.is_dir():
        for d in DATA.iterdir():
            if (d.is_dir() and not d.name.startswith(".")
                    and d.name not in ("inbox", ARCHIVE_DIR) and d != ARCHIVE and d != INBOX):
                names.add(d.name)
    names.add(DEFAULT_GROUP)
    return sorted(names)


def leaf_folders():
    """Every folder that holds recordings: (group, path)."""
    return [(g, place_dir(g)) for g in groups() if place_dir(g).is_dir()]

try:
    import multitrack as mt  # needs numpy; everything single-track works without it
except ImportError as _err:  # pragma: no cover
    mt = None
    MULTITRACK_ERROR = str(_err)
else:
    MULTITRACK_ERROR = None
import backup

# Inbox: files are picked up once their size/mtime has been stable this long…
INBOX_SETTLE_S = 4
# …matched against each other and the library, and an unmatched file goes
# ahead as an ordinary single-track recording after this grace period (so a
# partner file copied from a second recorder a little later still pairs up).
INBOX_SINGLE_DELAY_S = 120
INBOX_POLL_S = 3
# A matched group still waits this long after its newest file settled, in
# case more of the same recorders' files are on their way — and the full
# grace period when a recorder's last file is a complete 30-minute segment
# (DJI then has a continuation file that may not have been copied yet).
INBOX_GROUP_DELAY_S = 45
SEGMENT_FULL_S = (1790.0, 1810.0)
# DJI recorders save every take twice: <take>_orig.wav is the raw 32-bit float
# capture, <take>_edit.wav the same audio with the transmitter's own processing
# baked in (a peak limiter, plus noise cancelling / low-cut when switched on,
# which also delays it ~7 ms). Reel uses the raw one and never transcribes a
# processed twin whose raw file it has; those move to inbox/skipped/.
INBOX_TWIN_USE, INBOX_TWIN_SKIP = "_orig", "_edit"
INBOX_SKIPPED = INBOX / "skipped"
# A recorder plugged in by USB (a DJI transmitter mounts as "Mic Tx"; an SD
# card in a reader works too) has its new recordings copied into the inbox by
# itself: every file named like a DJI take that Reel hasn't seen — not in the
# inbox, the library, the archive or inbox/skipped/, and not copied before (the log in
# IMPORT_LOG, so recordings left on a mic are never imported twice). Only the
# raw _orig of a take goes to the inbox; its _edit twin, which Reel doesn't
# use, is kept in inbox/skipped/ (empty it whenever). The mic is left as it
# is, unless CLEAR THE MIC AFTER COPYING is on in ⚙ SETTINGS (clear_mic):
# then a file is deleted from it only once Reel holds a byte-identical copy of
# that very file (SHA-256 — for a file just copied, of what was read off the
# mic against the copy re-read from disk; otherwise of both files), and only
# if every copy this time succeeded. Nothing is deleted
# while a copy by hand into the inbox is still running (Finder may be reading
# the mic). While a mic is being copied or checked the inbox sorts nothing,
# and afterwards its files get the full INBOX_SINGLE_DELAY_S to find a
# partner — plug the other mics in too. REEL_AUTO_IMPORT=0 turns all of this off.
AUTO_IMPORT = os.environ.get("REEL_AUTO_IMPORT", "1") != "0"
VOLUMES = Path("/Volumes")
VOLUME_POLL_S = 5
RECORDER_FILE_RE = re.compile(r"^TX\d*_MIC\d+_\d{8}_\d{6}.*\.wav$", re.I)
IMPORT_LOG = INBOX / ".imported.json"
API = "https://api.elevenlabs.io/v1"
MODEL = "scribe_v2"
DEFAULT_PORT = 8765

# Match diarized speakers against profiles registered in your ElevenLabs
# workspace speaker library (matched speakers come back named). If your plan
# rejects the parameter, the tool automatically retries without it.
USE_SPEAKER_LIBRARY = True

# After a transcript is saved locally, delete it from ElevenLabs' servers so no
# copy of your text lingers there.
DELETE_REMOTE_TRANSCRIPT = True

AUDIO_EXTS = {
    ".mp3", ".m4a", ".m4b", ".mp4", ".mov", ".wav", ".flac", ".ogg", ".oga",
    ".opus", ".webm", ".aac", ".aiff", ".aif", ".amr", ".3gp", ".mkv", ".wma",
}
MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".wav": "audio/wav",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".webm": "audio/webm", ".aac": "audio/aac",
    ".aiff": "audio/aiff", ".aif": "audio/aiff",
}

ITEMS = {}  # id -> item dict (also persisted as library/<id>/item.json)
ITEM_MTIME = {}  # id -> mtime of the item.json we last read (a folder may also be changed by hand)
LIBRARY_RESCAN_S = 10
LOCK = threading.RLock()
def _mix_slots():
    """How many conversations may be aligned and mixed at once: each mostly uses
    one core and a few hundred MB, so up to 3 on a Mac with the cores and memory
    for it (18 cores / 64 GB → 3; 8 cores / 16 GB → 2; 8 GB → 1)."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = MemoryStatus(dwLength=ctypes.sizeof(MemoryStatus))
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                raise OSError("GlobalMemoryStatusEx failed")
            mem_gb = ms.ullTotalPhys / 2**30
        else:
            mem_gb = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                                        timeout=5).stdout) / 2**30
    except (OSError, ValueError, subprocess.SubprocessError):
        mem_gb = 8
    return max(1, min(3, (os.cpu_count() or 4) // 4, int(mem_gb // 8)))


MIX_SEM = threading.Semaphore(_mix_slots())  # conversations aligned/mixed at once (CPU + RAM)


# API keys: ELEVENLABS_API_KEY and ANTHROPIC_API_KEY, from the environment or
# .env (see ENV_FILE). Pasting one into ⚙ SETTINGS checks it and writes .env.

def api_key():
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def anthropic_key():
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def keys_state():
    """What ⚙ SETTINGS shows for each key: whether it is set, its last four
    characters, and whether it comes from the environment (which wins over .env)."""
    out = {}
    for name, var in KEY_VARS.items():
        key = os.environ.get(var, "").strip()
        out[name] = {"set": bool(key), "hint": key[-4:] if len(key) > 12 else "",
                     "from": "environment" if var in KEYS_FROM_ENV else ".env" if key else None}
    return out


def check_key(name, key):
    """Ask the service whether it takes this key: "ok", or "unchecked" when it
    couldn't say (offline, or a key limited to fewer permissions than the
    check needs). Raises ValueError for a key it turns down."""
    if name == "elevenlabs":
        req = urllib.request.Request(f"{API}/user", headers={"xi-api-key": key})
    else:
        req = urllib.request.Request(f"{ANTHROPIC_API}/models",
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            return "ok"
    except urllib.error.HTTPError as err:
        message = api_error_text(err)
        if err.code == 401 and "permission" not in message.lower():
            raise ValueError(f"{'ElevenLabs' if name == 'elevenlabs' else 'Anthropic'} turned that key down: {message}")
        return "unchecked"
    except (OSError, http.client.HTTPException):
        return "unchecked"


def save_key(name, key):
    """Store an API key in .env (readable by this user only) and use it from
    now on; an empty key removes it. Returns what check_key said."""
    var = KEY_VARS[name]
    if var in KEYS_FROM_ENV:
        raise ValueError(f"{var} is set in your shell's environment, which wins over Reel's settings: change it there")
    key = "".join((key or "").split())  # pasted keys pick up stray spaces and line breaks
    result = check_key(name, key) if key else None
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        lines = []
    lines = [l for l in lines if not l.strip().removeprefix("export ").startswith(var + "=")]
    if key:
        lines.append(f"{var}={key}")
        os.environ[var] = key
    else:
        os.environ.pop(var, None)
    tmp = ENV_FILE.with_name(".env.tmp")
    tmp.touch(mode=0o600)
    tmp.chmod(0o600)
    tmp.write_text("".join(l + "\n" for l in lines))
    tmp.replace(ENV_FILE)
    print(f"  {var} {'saved to' if key else 'removed from'} {ENV_FILE.name}")
    return result


# ---------------------------------------------------------------- persistence

def save_item(item):
    d = item_dir(item)
    if not d.exists():
        return
    try:
        tmp = d / ".item.json.tmp"
        tmp.write_text(json.dumps(item, indent=2))
        tmp.replace(d / "item.json")
        ITEM_MTIME[item["id"]] = (d / "item.json").stat().st_mtime  # our own write: nothing to reload
    except OSError:
        pass


def set_status(item, status, error=None):
    with LOCK:
        item["status"] = status
        item["error"] = error
    save_item(item)
    suffix = f": {error}" if error else ""
    print(f"  [{item['title']}] {status}{suffix}")
    if status == "ready":
        backup_soon()


def audio_path(item):
    if not item.get("ext"):
        return item_dir(item) / "audio.pending"
    return item_dir(item) / f"audio{item['ext']}"


def track_paths(item):
    """Recorder files of a multi-mic item, one list per mic (a recorder that
    saved several consecutive files contributes all its parts, in order)."""
    d = item_dir(item) / "tracks"
    return [[d / f for f in (t.get("parts") or [t["file"]])] for t in item.get("tracks") or []]


# ------------------------------------------------------------- ElevenLabs API

def http_json(method, url, payload, key, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("xi-api-key", key)
    if data is not None:
        req.add_header("content-type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def api_error_text(err):
    try:
        payload = json.loads(err.read().decode())
        detail = payload.get("detail") or payload.get("error")  # ElevenLabs | Anthropic
        if isinstance(detail, dict):
            return detail.get("message") or detail.get("status") or str(detail)
        if detail:
            return str(detail)
    except Exception:
        pass
    return f"HTTP {err.code}"


def post_multipart(url, key, fields, file_path, timeout=3600):
    """Stream a multipart/form-data POST without holding the file in memory.

    A list value becomes repeated form fields with the same name."""
    boundary = "reel" + os.urandom(12).hex()
    ctype = MIME.get(file_path.suffix.lower(), "application/octet-stream")
    tmp = file_path.parent / f".upload-{os.urandom(4).hex()}.tmp"
    try:
        with open(tmp, "wb") as out:
            for k, v in fields.items():
                for val in (v if isinstance(v, list) else [v]):
                    out.write(f'--{boundary}\r\nContent-Disposition: form-data; '
                              f'name="{k}"\r\n\r\n{val}\r\n'.encode())
            out.write(f'--{boundary}\r\nContent-Disposition: form-data; '
                      f'name="file"; filename="{file_path.name}"\r\n'
                      f'Content-Type: {ctype}\r\n\r\n'.encode())
            with open(file_path, "rb") as src:
                shutil.copyfileobj(src, out, 1 << 20)
            out.write(f"\r\n--{boundary}--\r\n".encode())
        with open(tmp, "rb") as body:
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("xi-api-key", key)
            req.add_header("content-type", f"multipart/form-data; boundary={boundary}")
            req.add_header("content-length", str(tmp.stat().st_size))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
    finally:
        tmp.unlink(missing_ok=True)


KEYTERMS_FILE = ROOT / "keyterms.txt"


def load_keyterms():
    """Lines like 'ASR (automatic speech recognition)'."""
    if not KEYTERMS_FILE.exists():
        return []
    return [line.strip() for line in KEYTERMS_FILE.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")]


def keyterms_state():
    """keyterms.txt as the app's editor shows it: the text, how many terms, how
    many of them ElevenLabs gets, and the ones it can't take."""
    text = KEYTERMS_FILE.read_text() if KEYTERMS_FILE.exists() else ""
    skipped = []
    for line in load_keyterms():
        m = re.fullmatch(r"(.+?)\s*\((.+)\)", line)
        term = (m.group(1) if m else line).strip()
        if len(term) >= 50 or len(term.split()) > 5:
            skipped.append(term)
    return {"text": text, "terms": len(load_keyterms()), "scribe": len(scribe_keyterms()),
            "skipped": skipped}


def save_keyterms(text):
    """Write keyterms.txt from the app's editor; used from the next transcription
    and clean-up on (it is read fresh each time)."""
    text = (text or "").replace("\r\n", "\n").rstrip() + "\n"
    tmp = KEYTERMS_FILE.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(KEYTERMS_FILE)


def scribe_keyterms():
    """Flatten keyterms.txt into ElevenLabs keyterms (term and gloss separately)."""
    terms = []
    for line in load_keyterms():
        m = re.fullmatch(r"(.+?)\s*\((.+)\)", line)
        for part in ([m.group(1), m.group(2)] if m else [line]):
            part = part.strip()
            if part and len(part) < 50 and len(part.split()) <= 5 and part not in terms:
                terms.append(part)
    return terms[:1000]


def to_transcript(result):
    """Map a Scribe response onto Reel's transcript schema (ms timestamps)."""
    words = []
    for w in result.get("words") or []:
        if w.get("type") == "spacing" or w.get("start") is None or w.get("end") is None:
            continue
        text = (w.get("text") or "").strip()
        if not text:
            continue
        logprob = w.get("logprob")
        confidence = (round(min(1.0, math.exp(logprob)), 3)
                      if isinstance(logprob, (int, float)) else 1.0)
        word = {
            "text": text,
            "start": int(w["start"] * 1000),
            "end": int(w["end"] * 1000),
            "confidence": confidence,
            "speaker": w.get("speaker_id"),
        }
        if w.get("type") == "audio_event":
            word["event"] = True
        words.append(word)

    # Scribe has no utterance objects; group words on speaker change / pauses.
    utterances = []
    cur = None
    for w in words:
        if (cur is None or w["speaker"] != cur["speaker"]
                or w["start"] - cur["end"] > 1500 or len(cur["texts"]) >= 200):
            cur = {"speaker": w["speaker"], "start": w["start"],
                   "end": w["end"], "texts": []}
            utterances.append(cur)
        cur["end"] = w["end"]
        cur["texts"].append(w["text"])

    duration = int((result.get("audio_duration_secs") or 0) * 1000)
    if not duration and words:
        duration = words[-1]["end"]
    return {
        "text": result.get("text") or "",
        "language": result.get("language_code"),
        "audio_duration_ms": duration,
        "words": words,
        "utterances": [{"speaker": u["speaker"], "start": u["start"],
                        "end": u["end"], "text": " ".join(u["texts"])}
                       for u in utterances],
    }


NO_KEY_MESSAGE = ("No ElevenLabs API key — add yours in ⚙ SETTINGS on the library page "
                  "(or set ELEVENLABS_API_KEY), then retry")


def scribe_transcribe(item, key, path):
    """Upload `path` to Scribe and return (raw result, Reel transcript)."""
    fields = {
        "model_id": MODEL,
        "diarize": "true",
        "tag_audio_events": "true",
        "timestamps_granularity": "word",
    }
    if USE_SPEAKER_LIBRARY:
        fields["use_speaker_library"] = "true"
    keyterms = scribe_keyterms()
    if keyterms:
        fields["keyterms"] = keyterms
    try:
        result = post_multipart(f"{API}/speech-to-text", key, fields, path)
    except urllib.error.HTTPError as err:
        message = api_error_text(err)
        low = message.lower()
        dropped = []
        if "use_speaker_library" in fields and "speaker" in low and "library" in low:
            del fields["use_speaker_library"]
            dropped.append("speaker library")
        if "keyterms" in fields and "keyterm" in low:
            del fields["keyterms"]
            dropped.append("keyterms")
        if not dropped:
            raise
        print(f"  [{item['title']}] {', '.join(dropped)} unavailable "
              f"({message}) — retrying without")
        result = post_multipart(f"{API}/speech-to-text", key, fields, path)
    return result, to_transcript(result)


def finish_transcript(item, key, result, transcript):
    """Persist a finished transcript, mark the item ready, tidy up remotely, name it."""
    item_id = item["id"]
    out = item_dir(item_id) / "transcript.json"
    if out.parent.exists():
        out.write_text(json.dumps(transcript))
    with LOCK:
        item["duration_ms"] = transcript["audio_duration_ms"]
        if is_voice_note(item):  # its start, now that its length is known
            item["recorded"] = recorded_at(item)
        item["language"] = transcript["language"]
        item["transcript_id"] = result.get("transcription_id")
    set_status(item, "ready")
    if not item.get("tracks"):  # multi-mic items start theirs right after the mix
        _after_ready_denoise(item_id)

    if DELETE_REMOTE_TRANSCRIPT and item.get("transcript_id"):
        try:
            http_json("DELETE",
                      f"{API}/speech-to-text/transcripts/{item['transcript_id']}",
                      None, key)
        except Exception:
            pass  # best effort; local copy is already saved

    if not item.get("ai_title"):
        generate_title(item_id)  # best effort; needs an Anthropic key
    if CLEAN_AUTO:
        auto_clean(item_id)


def auto_clean(item_id):
    """Start the clean-up on a newly transcribed recording (see CLEAN_AUTO)."""
    with LOCK:
        item = ITEMS.get(item_id)
        if item is None or item["status"] != "ready" or item.get("clean_status") in ("working", "ready"):
            return
    if not anthropic_key():
        return  # no key here: leave the button for whoever has one
    with LOCK:
        if item.get("clean_status") in ("working", "ready"):
            return
        item["clean_status"] = "working"
        item["clean_error"] = None
    save_item(item)
    print(f"  [{item.get('ai_title') or item['title']}] cleaning up")
    threading.Thread(target=clean_worker, args=(item_id,), daemon=True).start()


def relabel_item(item_id):
    """Redo who-said-what for a finished multi-mic recording from the stored
    level map and Scribe's original diarization — no transcription involved.
    Used when the labelling algorithm changes (LABEL_VERSION)."""
    with LOCK:
        item = ITEMS.get(item_id)
        if item is None or not item.get("tracks") or item["status"] != "ready":
            return False
        tag = f"[{item.get('ai_title') or item['title']}]"
    d = item_dir(item_id)
    tfile, npz = d / "transcript.json", d / "speakers.npz"
    if not (tfile.is_file() and npz.is_file()):
        return False
    transcript = json.loads(tfile.read_text())
    words = transcript["words"]
    for w in words:  # back to Scribe's own labels (kept in `diarized` where they differed)
        w["speaker"] = w.pop("diarized", None) or w["speaker"]
    if len(item["tracks"]) >= 2:
        labels, mic_ids, stats = mt.label_words(words, npz, log_fn=lambda m: print(f"  {tag} {m}"))
        for w, lbl in zip(words, labels):
            if w.get("speaker") != lbl:
                w["diarized"] = w.get("speaker")
            w["speaker"] = lbl
    else:  # one recorder: Scribe's diarization stands
        mic_ids, stats = [], {"confident": 0, "total": len(words), "provider_map": {}, "words_per_mic": {}}
        for t in item["tracks"]:
            t.pop("speaker", None)
    transcript["utterances"] = mt.regroup_utterances(words)
    transcript["mics"] = mic_ids
    tmp = tfile.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(transcript))
    tmp.replace(tfile)
    with LOCK:
        for i, mic in enumerate(mic_ids):
            if i < len(item["tracks"]):
                item["tracks"][i]["speaker"] = mic
        item["speaker_stats"] = stats
        item["label_version"] = mt.LABEL_VERSION
    save_item(item)
    return True


def name_unnamed():
    """Give a title to finished recordings that still lack one (naming is best
    effort and used to fail when several recordings finished at once)."""
    with LOCK:
        todo = [i["id"] for i in ITEMS.values()
                if i["status"] == "ready" and not i.get("ai_title")
                and (item_dir(i) / "transcript.json").is_file()]
    for item_id in todo:
        generate_title(item_id)


def denoise_item(item_id):
    """Make the denoised playback copy (denoised.m4a) of a finished recording
    with DeepFilterNet, if the tool is installed. Transcription is never
    affected: Scribe always gets the untouched audio."""
    if mt is None or not mt.denoise_available():
        return
    with LOCK:
        item = ITEMS.get(item_id)
        if item is None or not item.get("ext"):
            return
        item["denoise"] = "working"
        tag = f"[{item.get('ai_title') or item['title']}]"
    save_item(item)
    d = item_dir(item_id)
    src = audio_path(item)
    ok = False
    try:
        with tempfile.TemporaryDirectory(dir=d) as tmp:
            tmp = Path(tmp)
            wav = tmp / "in.wav"
            info = mt.WavData(src) if src.suffix.lower() == ".wav" else None
            # deep-filter reads 16-bit and float WAVs only: a 24-bit recorder file (DJI's
            # 24-bit mode) panics it ("TooWide"), so anything else is decoded to float first
            if info and info.rate == mt.MIX_RATE and (info.tag, info.bits) in ((1, 16), (3, 32)):
                shutil.copy2(src, wav)
            else:
                mt.decode_to_wav(src, wav)
            out = mt.denoise_wav(wav, tmp / "out.wav", log_fn=lambda m: print(f"  {tag} {m}"))
            if out is not None:
                enc = mt.encode_mix(out, tmp / "denoised.m4a")
                if enc is not None:
                    shutil.move(str(enc), d / "denoised.m4a")
                    ok = True
    except Exception as err:
        print(f"  {tag} denoising failed: {err}")
    with LOCK:
        item["denoise"] = "ready" if ok else "error"
    save_item(item)
    print(f"  {tag} " + ("denoised playback copy ready" if ok else "no denoised copy"))


def denoise_missing():
    """Denoised copies for finished recordings that don't have one yet."""
    if mt is None or not mt.denoise_available():
        return
    with LOCK:
        todo = [i["id"] for i in ITEMS.values()
                if i["status"] == "ready" and i.get("ext") and i.get("denoise") != "ready"
                and not (item_dir(i) / "denoised.m4a").is_file()]
    for item_id in todo:
        denoise_item(item_id)


def remix_item(item_id):
    """Rebuild a finished multi-mic recording's alignment and mix from its
    recorder files, then relabel its words — without transcribing again. The
    transcript's timestamps follow the longest recorder's own timeline, which
    a remix leaves alone, so they stay valid."""
    with LOCK:
        item = ITEMS.get(item_id)
        if item is None or not item.get("tracks") or item["status"] not in ("ready", "error"):
            raise SystemExit(f"{item_id}: not a finished multi-mic recording")
        tag = f"[{item.get('ai_title') or item['title']}]"
    d = item_dir(item_id)
    paths = track_paths(item)
    missing = [p.name for parts in paths for p in parts if not p.is_file()]
    if missing:
        raise SystemExit(f"{item_id}: recorder file missing: {', '.join(missing)}")
    with MIX_SEM:
        set_status(item, "aligning")
        for old in ("audio.m4a", "audio.wav", "mix.wav", "speakers.npz"):
            (d / old).unlink(missing_ok=True)
        last = [0]

        def progress(frac):
            pct = int(frac * 100)
            if pct >= last[0] + 10:
                last[0] = pct
                with LOCK:
                    item["status"] = f"mixing {pct}%"
                save_item(item)

        # keep the transcript's timeline: the same reference track as before
        ref = next((i for i, t in enumerate(item["tracks"])
                    if t.get("offset") == 0 and not t.get("drift")), None)
        info = mt.build_mix(paths, d, log_fn=lambda m: print(f"  {tag} {m}"), progress=progress,
                            ref_index=ref)
        set_status(item, "encoding")
        out = mt.encode_mix(d / "mix.wav", d / "audio.m4a")
    if out is not None:
        (d / "mix.wav").unlink(missing_ok=True)
        ext = ".m4a"
    else:
        (d / "mix.wav").replace(d / "audio.wav")
        ext = ".wav"
    by_file = {t["file"]: t for t in info["tracks"]}
    with LOCK:
        item["ext"] = ext
        item["duration_ms"] = info["duration_ms"]
        item["tracks"] = [{**t, **by_file.get(t["file"], {})} for t in item["tracks"]]
        item["mix"] = info["mix"]
    set_status(item, "ready")
    if (d / "transcript.json").is_file():
        relabel_item(item_id)
    print(f"  {tag} remixed" + (" and relabelled" if (d / "transcript.json").is_file() else ""))
    (d / "denoised.m4a").unlink(missing_ok=True)
    with LOCK:
        item["denoise"] = None
    denoise_item(item_id)


def remix_items(ids):
    for iid in ids:
        try:
            remix_item(iid)
        except SystemExit as err:
            print(f"  remix: {err}")
        except Exception as err:
            print(f"  remix {iid}: {err}")


def _after_ready_denoise(item_id):
    threading.Thread(target=denoise_item, args=(item_id,), daemon=True).start()


def relabel_outdated():
    """Relabel every finished multi-mic recording made with an older labelling."""
    if mt is None:
        return
    with LOCK:
        todo = [i["id"] for i in ITEMS.values()
                if i.get("tracks") and i["status"] == "ready"
                and (i.get("label_version") or 0) < mt.LABEL_VERSION]
    for item_id in todo:
        try:
            if relabel_item(item_id):
                print(f"  relabelled speakers of {item_id} (labelling v{mt.LABEL_VERSION})")
        except Exception as err:
            print(f"  relabel {item_id}: {err}")


def key_added(name):
    """A key was just saved in ⚙ SETTINGS: recordings that failed for want of
    an ElevenLabs key go again, and unnamed ones get a title from Claude."""
    if name == "elevenlabs":
        with LOCK:
            todo = [i for i in ITEMS.values() if i["status"] == "error" and i.get("error") == NO_KEY_MESSAGE]
            for it in todo:
                it["transcript_id"] = None
                set_status(it, "queued")
        for it in todo:
            threading.Thread(target=transcribe_worker, args=(it["id"],), daemon=True).start()
    else:
        threading.Thread(target=name_unnamed, daemon=True).start()


def transcribe_worker(item_id):
    with LOCK:
        item = ITEMS.get(item_id)
    if item is None:
        return
    if item.get("tracks"):
        return multitrack_worker(item_id)
    key = api_key()
    if not key:
        set_status(item, "error", NO_KEY_MESSAGE)
        return
    try:
        set_status(item, "transcribing")
        result, transcript = scribe_transcribe(item, key, audio_path(item))
        finish_transcript(item, key, result, transcript)
    except urllib.error.HTTPError as err:
        set_status(item, "error", api_error_text(err))
    except Exception as err:
        set_status(item, "error", str(err))


# ------------------------------------------------------ multi-mic conversations

def mix_is_built(item):
    """True when align+mix already ran for this item (retries skip straight to Scribe)."""
    d = item_dir(item)
    return (bool(item.get("ext")) and audio_path(item).is_file()
            and (d / "speakers.npz").is_file()
            and all("offset" in t for t in item.get("tracks") or []))


def multitrack_worker(item_id):
    """Align the recorders' files, auto-mix them, transcribe the mix, label words by mic."""
    with LOCK:
        item = ITEMS.get(item_id)
    if item is None:
        return
    if mt is None:
        set_status(item, "error", f"Multi-mic support needs numpy ({MULTITRACK_ERROR})")
        return
    d = item_dir(item_id)
    paths = track_paths(item)
    missing = [p.name for parts in paths for p in parts if not p.is_file()]
    if missing:
        set_status(item, "error", f"Recorder file missing: {', '.join(missing)}")
        return
    tag = f"[{item['title']}]"
    adding = bool(item.get("adding"))  # mics just added: on failure, put it back as it was

    def fail(msg):
        if adding:
            _undo_attach(item, msg)
        else:
            set_status(item, "error", msg)

    try:
        if not mix_is_built(item):
            with MIX_SEM:
                set_status(item, "aligning")
                for old in ("audio.m4a", "audio.wav", "mix.wav", "speakers.npz"):
                    (d / old).unlink(missing_ok=True)
                last = [0]

                def progress(frac):
                    pct = int(frac * 100)
                    if pct >= last[0] + 10:
                        last[0] = pct
                        with LOCK:
                            item["status"] = f"mixing {pct}%"
                        save_item(item)

                info = mt.build_mix(paths, d, log_fn=lambda m: print(f"  {tag} {m}"),
                                    progress=progress)
                set_status(item, "encoding")
                out = mt.encode_mix(d / "mix.wav", d / "audio.m4a")
                if out is not None:
                    (d / "mix.wav").unlink(missing_ok=True)
                    ext = ".m4a"
                else:
                    (d / "mix.wav").replace(d / "audio.wav")
                    ext = ".wav"
            by_file = {t["file"]: t for t in info["tracks"]}
            with LOCK:
                item["ext"] = ext
                item["duration_ms"] = info["duration_ms"]
                item["tracks"] = [{**t, **by_file.get(t["file"], {})} for t in item["tracks"]]
                item["mix"] = info["mix"]
                item["denoise"] = None
            save_item(item)
            (d / "denoised.m4a").unlink(missing_ok=True)
            if not adding:  # (else only once it has all worked: an undo must not race it)
                threading.Thread(target=denoise_item, args=(item_id,), daemon=True).start()

        key = api_key()
        if not key:
            fail(NO_KEY_MESSAGE)
            return
        set_status(item, "transcribing")
        result, transcript = scribe_transcribe(item, key, audio_path(item))

        # Who said what: the mic levels and Scribe's diarization are weighed
        # word by word (see multitrack.label_words). With a single recorder
        # there is nothing to compare, and Scribe's diarization stands.
        words = transcript["words"]
        if len(item["tracks"]) >= 2:
            labels, mic_ids, stats = mt.label_words(words, d / "speakers.npz",
                                                    log_fn=lambda m: print(f"  {tag} {m}"))
            for w, lbl in zip(words, labels):
                if w.get("speaker") != lbl:
                    w["diarized"] = w.get("speaker")  # keep Scribe's guess for reference
                w["speaker"] = lbl
            transcript["utterances"] = mt.regroup_utterances(words)
        else:
            mic_ids, stats = [], {"confident": 0, "total": len(words), "provider_map": {}, "words_per_mic": {}}
        transcript["mics"] = mic_ids
        with LOCK:
            for i, mic in enumerate(mic_ids):
                if i < len(item["tracks"]):
                    item["tracks"][i]["speaker"] = mic
            item["speaker_stats"] = stats
            item["label_version"] = mt.LABEL_VERSION
        finish_transcript(item, key, result, transcript)
        if adding:
            with LOCK:
                item.pop("adding", None)
            save_item(item)
            threading.Thread(target=denoise_item, args=(item_id,), daemon=True).start()
    except urllib.error.HTTPError as err:
        fail(api_error_text(err))
    except Exception as err:
        fail(str(err))


# ------------------------------------------------- Claude transcript clean-up

ANTHROPIC_API = "https://api.anthropic.com/v1"
CLEAN_MODEL = "claude-fable-5"
# "low" is plenty for filler-word detection on this model and keeps each pass
# fast; raise to "high"/"xhigh" if you want maximum judgment on edge cases.
CLEAN_EFFORT = "low"
# Run the clean-up by itself on every new transcript, not only from the
# button. Skipped quietly without an Anthropic key. Costs roughly $1–3 per
# 2-hour recording.
CLEAN_AUTO = True
CLEAN_CHUNK_WORDS = 800

CLEAN_SYSTEM = """Here is a verbatim speech transcript. I would like you to clean it up. Often, the transcript will be pretty garbled, so you will have to figure various things out from context: the recording's title, the speakers' names and the key terms that come with the transcript, and what is being talked about. I don't want you to change the wording of what people say, you should leave it basically verbatim, but I want you to fix obvious transcription errors where possible and clean up filler words and the like. Before you start, you should think carefully about what edits to make. Once you're done thinking, respond with two lists of edits; with indices referring to the numbered words.

"remove" — indices of pure filler words to drop:
- filler sounds: um, uh, er, erm, mm, hmm and variants
- discourse fillers (like, you know, I mean, sort of, kind of, well, so, right) only where they carry no meaning
- stutters and immediate repetitions of a word or phrase — keep the occurrence that carries the punctuation, usually the last
- false starts: abandoned sentence fragments the speaker restarted

"edit" — span rewrites, as {"s": first index, "e": last index, "text": "replacement"}. The text replaces original words s through e inclusive, and their timestamps are remapped onto it — so keep spans tight, never overlapping, and never spanning a speaker change. Use edits to:
- fix transcription errors: garbled names and terms (use the recording context and spellings used elsewhere), homophones, run-together or mis-split words
- fix punctuation the transcription got wrong: missing or stray commas, a period or question mark where the sentence actually ends, comma splices, run-on sentences that should be split — punctuation attaches to a word, so a single-word edit like {"s": 12, "e": 12, "text": "word."} is enough
- repair what removals leave behind: capitalize a new sentence start, fix orphaned punctuation or dangling connectives
- lightly smooth clearly mangled passages into readable form — but never paraphrase, summarize, or change the speaker's meaning or word choice; keep their voice, including informal words like "wanna"

Keep everything else exactly as transcribed: anything that carries meaning ("like" as a verb or preposition, "right" as an answer), bracketed audio events such as [laughs]. When unsure, leave the word unchanged. Most words should survive untouched."""

# The prompt above is built in. ✎ PROMPT in the app can layer two files on top,
# both next to keyterms.txt so they're easy to hand-edit or version:
#   clean_prompt_default.txt — the user's own baseline (what RESET TO DEFAULT restores)
#   clean_prompt.txt         — the active prompt, when it differs from that baseline
CLEAN_DEFAULT_FILE = ROOT / "clean_prompt_default.txt"
CLEAN_PROMPT_FILE = ROOT / "clean_prompt.txt"


def _read_prompt(path):
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


def _normalize_prompt(text):
    return (text or "").replace("\r\n", "\n").strip()


def load_default_prompt():
    """The baseline prompt: clean_prompt_default.txt if present, else the built-in one."""
    return _read_prompt(CLEAN_DEFAULT_FILE) or CLEAN_SYSTEM


def load_clean_prompt():
    """The prompt clean-ups actually use: clean_prompt.txt if present, else the default."""
    return _read_prompt(CLEAN_PROMPT_FILE) or load_default_prompt()


def save_clean_prompt(text):
    """Use this prompt from now on; empty text or the default itself removes the override."""
    text = _normalize_prompt(text)
    if not text or text == load_default_prompt().strip():
        CLEAN_PROMPT_FILE.unlink(missing_ok=True)
    else:
        CLEAN_PROMPT_FILE.write_text(text + "\n")


def save_default_prompt(text):
    """Make this prompt the baseline and the active prompt; built-in text removes the file."""
    text = _normalize_prompt(text)
    if not text or text == CLEAN_SYSTEM.strip():
        CLEAN_DEFAULT_FILE.unlink(missing_ok=True)
    else:
        CLEAN_DEFAULT_FILE.write_text(text + "\n")
    CLEAN_PROMPT_FILE.unlink(missing_ok=True)  # the new default is what runs next


def clean_prompt_state():
    prompt, default = load_clean_prompt(), load_default_prompt()
    return {"prompt": prompt, "default": default, "builtin": CLEAN_SYSTEM,
            "custom": prompt != default, "default_custom": default != CLEAN_SYSTEM}

CLEAN_SCHEMA = {
    "type": "object",
    "properties": {
        "remove": {"type": "array", "items": {"type": "integer"}},
        "edit": {"type": "array", "items": {
            "type": "object",
            "properties": {"s": {"type": "integer"}, "e": {"type": "integer"},
                           "text": {"type": "string"}},
            "required": ["s", "e", "text"],
            "additionalProperties": False,
        }},
    },
    "required": ["remove", "edit"],
    "additionalProperties": False,
}


def anthropic_request(key, payload, attempts=5):
    body = json.dumps(payload).encode()
    last_err = None
    for attempt in range(attempts):
        req = urllib.request.Request(f"{ANTHROPIC_API}/messages", data=body, method="POST")
        req.add_header("x-api-key", key)
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("anthropic-beta", "server-side-fallback-2026-07-01")
        req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as err:
            if err.code in (408, 429, 500, 502, 503, 529) and attempt < attempts - 1:
                last_err = err
                retry_after = err.headers.get("retry-after")
                time.sleep(int(retry_after) if retry_after and retry_after.isdigit()
                           else 3 * 2 ** attempt)
                continue
            raise RuntimeError(f"Claude API: {api_error_text(err)}") from err
        except (OSError, http.client.HTTPException) as err:
            # Connection resets / timeouts on long requests are retryable.
            if attempt < attempts - 1:
                time.sleep(3 * 2 ** attempt)
                continue
            raise RuntimeError(f"Claude API: {err}") from err
    raise RuntimeError(f"Claude API: {api_error_text(last_err)}")


def clean_chunk(key, words, start, context, system=CLEAN_SYSTEM):
    numbered = "\n".join(f"{start + i}: {w['text']}" for i, w in enumerate(words))
    prompt = (context + "\n\n" if context else "") + numbered
    base = {
        "model": CLEAN_MODEL,
        "max_tokens": 16000,
        "system": system,
        "fallbacks": "default",  # re-route the (unlikely) safety refusal server-side
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        # Preferred: structured outputs guarantee valid JSON back.
        result = anthropic_request(key, {**base, "output_config": {
            "effort": CLEAN_EFFORT,
            "format": {"type": "json_schema", "schema": CLEAN_SCHEMA},
        }}, attempts=2)
    except RuntimeError as err:
        if "Overloaded" not in str(err):
            raise
        # The structured-output path is sometimes capacity-limited; plain JSON
        # prompting works fine for this task, with lenient parsing below.
        plain = {**base, "output_config": {"effort": CLEAN_EFFORT}}
        plain["messages"] = [{"role": "user", "content": prompt +
            '\n\nReply with only a JSON object: '
            '{"remove": [indices], "edit": [{"s": first, "e": last, "text": "replacement"}]}'}]
        result = anthropic_request(key, plain)

    if result.get("stop_reason") == "refusal":
        raise RuntimeError("Claude declined to process part of this transcript")
    if result.get("stop_reason") == "max_tokens":
        raise RuntimeError("Claude's response was truncated — try a smaller CLEAN_CHUNK_WORDS")
    text = next(b["text"] for b in result["content"] if b["type"] == "text")
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise RuntimeError("Claude returned no JSON for a transcript chunk")
    data = json.loads(match.group())
    end = start + len(words)
    remove = [i for i in data.get("remove") or [] if start <= i < end]
    edits = []
    for r in data.get("edit") or []:
        try:
            s, e, fixed = int(r["s"]), int(r["e"]), str(r["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if start <= s <= e < end:
            if fixed:
                edits.append({"s": s, "e": e, "text": fixed})
            else:  # empty replacement is just a removal of the span
                remove.extend(range(s, e + 1))
    return remove, edits


TITLE_SYSTEM = """You title audio recordings from their transcripts. Reply with the title only: specific and short (3–8 words), in the language the user message names (the transcript's language), no surrounding quotes, no trailing period, and no generic openers like "Recording of" or "Conversation about". Mention speakers or topics only when they are clearly central."""
TITLE_LOCK = threading.Lock()  # one naming request at a time: parallel ones get rejected under load
LANG_NAMES = {"eng": "English", "deu": "German", "fra": "French", "spa": "Spanish", "ita": "Italian",
              "nld": "Dutch", "por": "Portuguese", "pol": "Polish", "swe": "Swedish", "dan": "Danish",
              "nor": "Norwegian", "fin": "Finnish", "rus": "Russian", "jpn": "Japanese", "zho": "Chinese",
              "cmn": "Chinese", "kor": "Korean", "hin": "Hindi", "ara": "Arabic", "tur": "Turkish"}


def generate_title(item_id):
    """Best-effort: ask Claude for a descriptive recording title. Returns it (it
    replaces a title typed by hand too), or None when none came back."""
    with LOCK:
        item = ITEMS.get(item_id)
        based_on = item and item.get("transcript_id")
    key = anthropic_key()
    if item is None or not key:
        return None
    try:
        t = json.loads((item_dir(item_id) / "transcript.json").read_text())
        words = t["words"]
        if not words:
            return None
        opening = " ".join(w["text"] for w in words[:1200])
        middle = ""
        if len(words) > 2400:
            mid = len(words) // 2
            middle = " ".join(w["text"] for w in words[mid:mid + 400])
        names = sorted({
            s.replace("-", " ").replace("_", " ").title()
            for s in {w.get("speaker") for w in words}
            if s and not re.fullmatch(r"speaker_\d+|mic_\d+|[A-Z]", s)
        })
        code = (item.get("language") or t.get("language") or "").lower()
        lang = LANG_NAMES.get(code[:3], code or "the transcript's language")
        prompt = f"Duration: {clock_min(item.get('duration_ms'))}. Transcript language: {lang} — write the title in {lang}."
        if names:
            prompt += " Speakers include: " + ", ".join(names) + "."
        prompt += f"\n\nOpening of the transcript:\n{opening}"
        if middle:
            prompt += f"\n\nSample from the middle:\n{middle}"
        with TITLE_LOCK:
            result = anthropic_request(key, {
                "model": CLEAN_MODEL,
                "max_tokens": 2000,
                "system": TITLE_SYSTEM,
                "fallbacks": "default",
                "output_config": {"effort": "low"},
                "messages": [{"role": "user", "content": prompt}],
            }, attempts=3)
        text = next(b["text"] for b in result["content"] if b["type"] == "text")
        title = text.strip().splitlines()[0].strip().strip('"“”').rstrip(".").strip()
        if 0 < len(title) <= 90:
            with LOCK:
                if item["status"] != "ready" or item.get("transcript_id") != based_on:
                    return None  # rebuilt meanwhile (a mic added): its new transcript gets its own title
                item["ai_title"] = title
                item.pop("title_manual", None)
            save_item(item)
            print(f"  [{item['title']}] named: {title}")
            return title
    except Exception as err:
        print(f"  [{item['title']}] naming failed: {err}")
    return None


def clock_min(ms):
    if not ms:
        return "unknown"
    minutes = round(ms / 60000)
    return f"{minutes} min" if minutes < 90 else f"{minutes // 60} h {minutes % 60} min"


def set_clean_status(item, status, error=None):
    with LOCK:
        item["clean_status"] = status
        item["clean_error"] = error
    save_item(item)


def clean_worker(item_id):
    with LOCK:
        item = ITEMS.get(item_id)
    if item is None:
        return
    key = anthropic_key()
    if not key:
        set_clean_status(item, "error",
                         "No Anthropic API key — add one in ⚙ SETTINGS on the library page "
                         "(or set ANTHROPIC_API_KEY)")
        return
    try:
        words = json.loads((item_dir(item_id) / "transcript.json").read_text())["words"]
        chunks = [(i, words[i:i + CLEAN_CHUNK_WORDS])
                  for i in range(0, len(words), CLEAN_CHUNK_WORDS)]

        # Recording-level context helps fix garbled names ("Eye, Anna" → "I, Ana").
        names = set(item.get("speaker_names", {}).values())
        for w in words:
            s = w.get("speaker")
            if s and not re.fullmatch(r"speaker_\d+|mic_\d+|[A-Z]", s):
                names.add(s.replace("-", " ").replace("_", " ").title())
        context = f'Recording: "{item["title"]}".'
        if names:
            context += " Speakers include: " + ", ".join(sorted(names)) + "."
        terms = load_keyterms()
        if terms:
            context += (" Technical terms likely to appear — fix mis-transcriptions"
                        " of these: " + "; ".join(terms) + ".")

        system = load_clean_prompt()  # read once so every chunk gets the same prompt
        custom = system != CLEAN_SYSTEM
        print(f"  [{item['title']}] cleaning {len(words)} words in {len(chunks)} chunks"
              f"{' with custom prompt' if custom else ''}…")

        def chunk_with_retry(c):
            for pause in (30, 60, None):  # ride out congestion and network blips
                try:
                    return clean_chunk(key, c[1], c[0], context, system)
                except RuntimeError as err:
                    if not str(err).startswith("Claude API:") or pause is None:
                        raise
                    time.sleep(pause)

        removed, edits = [], []
        with ThreadPoolExecutor(max_workers=2) as pool:
            for rem, eds in pool.map(chunk_with_retry, chunks):
                removed.extend(rem)
                edits.extend(eds)

        # Normalize: sorted, non-overlapping edits; removals never inside a span.
        edits.sort(key=lambda ed: (ed["s"], ed["e"]))
        kept_edits, cursor = [], -1
        for ed in edits:
            if ed["s"] > cursor:
                kept_edits.append(ed)
                cursor = ed["e"]
        spanned = {i for ed in kept_edits for i in range(ed["s"], ed["e"] + 1)}
        removed = sorted(set(removed) - spanned)

        out = item_dir(item_id) / "clean.json"
        if out.parent.exists():
            out.write_text(json.dumps({
                "removed": removed,
                "edits": kept_edits,
                "model": CLEAN_MODEL,
                "custom_prompt": custom,
                "total_words": len(words),
            }))
        set_clean_status(item, "ready")
        print(f"  [{item['title']}] cleaned: {len(removed)} of {len(words)} words hidden, "
              f"{len(kept_edits)} span edits")
    except Exception as err:
        set_clean_status(item, "error", str(err))


# -------------------------------------------------------------------- library

def add_file(src, title=None, move=False, group=None):
    notes = starting_flags(None)
    ext = src.suffix.lower()
    stem = Path(title).stem if title else src.stem
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:40] or "recording"
    item_id = f"{slug}-{os.urandom(3).hex()}"
    d = place_dir(group) / item_id
    d.mkdir(parents=True)
    try:
        src_mtime = src.stat().st_mtime
    except OSError:
        src_mtime = None
    if move:
        shutil.move(str(src), d / f"audio{ext}")
    else:
        shutil.copy2(src, d / f"audio{ext}")
    item = {
        "id": item_id,
        "title": title or src.name,
        "ext": ext,
        "added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "queued",
        "error": None,
        "duration_ms": None,
        "language": None,
        "transcript_id": None,
        "speaker_names": {},
        "ai_title": None,
        "clean_status": None,
        "clean_error": None,
        "group": group or DEFAULT_GROUP,
        **NOTE_DEFAULTS,
    }
    item.update(notes)
    tidy_notes(item)
    item["recorded"] = recorded_at(item)
    if item["recorded"] == item["added"] and src_mtime:
        item["recorded"] = datetime.fromtimestamp(src_mtime).astimezone().isoformat(timespec="seconds")
    with LOCK:
        ITEMS[item_id] = item
    save_item(item)
    threading.Thread(target=transcribe_worker, args=(item_id,), daemon=True).start()
    return item


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "track"


# Signal names a saved attachment by when its message was sent, which for a
# voice note is when the recording *ended*: signal-2026-10-14-09-31-05-250.aac
SIGNAL_NAME_RE = re.compile(r"^signal-(\d{4})-(\d{2})-(\d{2})-(\d{2})-?(\d{2})-?(\d{2})(?:-(\d{3}))?\b", re.I)


def signal_sent(name):
    """When a Signal attachment's message was sent (epoch seconds), or None."""
    m = SIGNAL_NAME_RE.match(Path(name or "").name)
    if not m:
        return None
    try:
        return datetime.strptime("".join(m.groups()[:6]), "%Y%m%d%H%M%S").timestamp() + int(m.group(7) or 0) / 1000
    except ValueError:
        return None


def is_voice_note(item):
    return not item.get("tracks") and signal_sent(item.get("title")) is not None


def recorded_at(item, src=None):
    """When the audio was recorded, as an ISO timestamp: the recorder files'
    own timestamps (earliest part) when the names carry one, or a Signal voice
    note's send time less its length, else the source file's modification
    time, else when the item was added."""
    names = []
    for t in item.get("tracks") or []:
        names += t.get("parts") or [t.get("file")]
        names.append(t.get("original"))
    names.append(item.get("title"))
    times = []
    for n in names:
        if n and mt is not None:
            t = mt.parse_name(Path(n).name)[2]
            if t:
                times.append(t)
    if is_voice_note(item):
        times.append(signal_sent(item["title"]) - (item.get("duration_ms") or 0) / 1000)
    if times:
        return datetime.fromtimestamp(min(times)).astimezone().isoformat(timespec="seconds")
    if src is not None:
        try:
            return datetime.fromtimestamp(Path(src).stat().st_mtime).astimezone().isoformat(timespec="seconds")
        except OSError:
            pass
    return item.get("added")


def group_title(paths):
    """A title for a multi-mic conversation, until Claude names it from the transcript.

    Recorder files usually carry a timestamp (DJI: TX01_MIC002_20250527_202904_orig.wav);
    otherwise use the files' common stem, else the first file's name."""
    stems = [Path(p).stem for p in paths]
    for stem in stems:
        m = re.search(r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})\d{2}", stem)
        if m:
            y, mo, d, h, mi = m.groups()
            return f"Conversation {y}-{mo}-{d} {h}.{mi} ({len(paths)} mic{'s' if len(paths) != 1 else ''})"
    prefix = os.path.commonprefix(stems).rstrip("_- ")
    base = prefix if len(prefix) >= 6 else stems[0]
    return f"{base} ({len(paths)} mic{'s' if len(paths) != 1 else ''})"


def move_parts(parts, d):
    """Move one recorder's file(s) into tracks/; returns the track record."""
    names = []
    for p in parts:
        p = Path(p)
        name = safe_name(p.name)
        while (d / name).exists():
            name = f"{Path(name).stem}_{os.urandom(2).hex()}{Path(name).suffix}"
        shutil.move(str(p), d / name)
        names.append(name)
    rec = {"file": names[0], "original": Path(parts[0]).name}
    if len(names) > 1:
        rec["parts"] = names
        rec["original"] += f" (+{len(names) - 1} more)"
    return rec


def add_group(paths, title=None, group=None, notes=None, combined=None):
    """One conversation captured by several recorders: files move into tracks/.

    Each entry of `paths` is one mic: a file, or a list of that recorder's
    consecutive files. `notes` as starting_flags takes them; `combined` as
    combine_items records it."""
    notes = starting_flags(notes)
    paths = [[Path(p) for p in ps] if isinstance(ps, (list, tuple)) else [Path(ps)] for ps in paths]
    title = title or group_title([ps[0] for ps in paths])
    slug = re.sub(r"[^a-z0-9]+", "-", Path(title).stem.lower()).strip("-")[:40] or "conversation"
    item_id = f"{slug}-{os.urandom(3).hex()}"
    d = place_dir(group) / item_id / "tracks"
    d.mkdir(parents=True)
    tracks = [move_parts(ps, d) for ps in paths]
    item = {
        "id": item_id,
        "title": title,
        "ext": None,
        "added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "queued",
        "error": None,
        "duration_ms": None,
        "language": None,
        "transcript_id": None,
        "speaker_names": {},
        "ai_title": None,
        "clean_status": None,
        "clean_error": None,
        "tracks": tracks,
        "group": group or DEFAULT_GROUP,
        **NOTE_DEFAULTS,
    }
    item.update(notes)
    tidy_notes(item)
    if combined:
        item["combined"] = combined
    item["recorded"] = recorded_at(item)
    with LOCK:
        ITEMS[item_id] = item
    save_item(item)
    nfiles = sum(len(t.get("parts") or [t["file"]]) for t in tracks)
    print(f"  [{title}] {len(tracks)} mics ({nfiles} files) grouped")
    threading.Thread(target=multitrack_worker, args=(item_id,), daemon=True).start()
    return item


# A recording's notes on how it may be used, kept in item.json (and backed up
# with the transcript): no_training keeps it out of training data, for whoever
# assembles some from the library; training_ask names who could still OK it.
NOTE_DEFAULTS = {"no_training": False, "training_ask": ""}


def tidy_notes(item):
    """Drop a note that no longer applies: whom to ask about a recording that's
    allowed for training anyway."""
    if not item.get("no_training"):
        item["training_ask"] = ""


# This Mac's ⚙ SETTINGS: how brand-new recordings start, whether a plugged-in
# recorder is cleared once its recordings are copied (import_volume), and the
# Google Drive backup (backup.py). Next to the code but gitignored, like .env.
SETTINGS_FILE = ROOT / "settings.json"
SETTINGS_DEFAULTS = {"no_training": False, "clear_mic": False,
                     "backup": False, "backup_dir": "", "backup_tracks": False}
DEFAULT_NOTE = "<default>"  # a ticked-by-the-settings box's note, until someone writes the real one


def load_settings():
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    return {k: saved[k] if type(saved.get(k)) is type(v) else v for k, v in SETTINGS_DEFAULTS.items()}


def save_settings(changes):
    settings = {**load_settings(), **changes}
    tmp = SETTINGS_FILE.with_name(".settings.json.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(SETTINGS_FILE)
    return settings


def starting_flags(notes):
    """How a recording starts out: one rebuilt from others (combine, regroup)
    keeps the notes they had; a brand-new one (notes None) gets this Mac's
    settings, its note reading DEFAULT_NOTE until someone writes the reason."""
    if notes is not None:
        return notes
    notes = {}
    if load_settings()["no_training"]:
        notes.update(no_training=True, training_ask=DEFAULT_NOTE)
    return notes


def set_place(item_id, group):
    """Move a recording to another folder. Only when nothing is working on it."""
    with LOCK:
        item = ITEMS.get(item_id)
        if item is None:
            raise ValueError("No such recording")
        if item["status"] not in ("ready", "error") or item.get("clean_status") == "working":
            raise ValueError("Wait until the recording is done processing")
        group = (group or item.get("group") or DEFAULT_GROUP).strip().lower()
        if not GROUP_RE.match(group) or group in ("inbox", ARCHIVE_DIR):
            raise ValueError(f"Folder names: lowercase letters, digits and dashes (e.g. interviews) "
                             f"— and not inbox or {ARCHIVE_DIR}")
        if group == item.get("group"):
            return item
        src = item_dir(item)
        item["group"] = group
        dst = item_dir(item)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    save_item(item)
    print(f"  [{item.get('ai_title') or item['title']}] moved to {dst.parent.relative_to(DATA)}/")
    return item


# Cutting and splitting a finished recording (✂ CUT / SPLIT on its page).
# Both keep the transcript: its words are divided or dropped and their times
# moved, so nothing is transcribed again, and the clean-up and speaker names
# carry over. The recording as it was goes to the archive, from where an edit
# can be undone by hand.

EDITING = ("cutting", "splitting")  # in memory only, while an edit is being written
EDIT_EDGE_MS = 50  # a cut this close to the start or end runs to it: the recording is trimmed
EDIT_PAD_S = 3.0  # silence this much wider in a recorder file placed by the whole seconds in
                  # its name, after a restart (part_gaps; alignment slides it by up to 3 s)


def _clone(src, dst):
    """Copy a file, as an APFS clone where possible: instant, and no extra
    disk until one of the two changes."""
    if sys.platform == "darwin" and subprocess.run(["cp", "-c", str(src), str(dst)],
                                                   capture_output=True).returncode == 0:
        return
    shutil.copy2(src, dst)


def _edit_audio(src, dst_dir, stem, t0, t1, silence):
    """Seconds [t0, t1) of audio file `src` (t1 None: to its end), each (a, b)
    in `silence` (seconds on src's own timeline) silenced, written to dst_dir
    as `stem` plus src's suffix when src is a WAV (its format kept), else as
    `stem`.m4a.
    Returns the path written."""
    src = Path(src)
    with tempfile.TemporaryDirectory(dir=LIBRARY) as tmp:
        native = src.suffix.lower() == ".wav"
        if native:
            try:
                mt.wav_layout(src)
            except ValueError:
                native = False
        wav = src if native else mt.decode_to_wav(src, Path(tmp) / "decoded.wav")
        frames, rate = mt.wav_frames(wav)
        f0 = min(frames, round(t0 * rate))
        f1 = frames if t1 is None else min(frames, round(t1 * rate))
        out = Path(dst_dir) / f"{stem}{src.suffix}" if native else Path(tmp) / "edited.wav"
        if f0 == 0 and f1 == frames:
            _clone(wav, out)
        else:
            mt.write_wav_slice(wav, out, f0, f1)
        quiet = [(round(a * rate) - f0, round(b * rate) - f0) for a, b in silence]
        quiet = [(a, b) for a, b in quiet if b > 0 and a < f1 - f0]
        if quiet:
            mt.silence_wav(out, quiet)
        if native:
            return out
        enc = mt.encode_mix(out, Path(tmp) / "edited.m4a")
        if enc is None:
            raise RuntimeError(f"Couldn't encode the edited {src.name} (afconvert)")
        final = Path(dst_dir) / f"{stem}.m4a"
        shutil.move(str(enc), final)
        return final


def _shifted_name(name, shift_s, taken=()):
    """A recorder file's name for its piece that starts `shift_s` seconds in:
    the timestamp in it moved on (TX01_MIC004_20261013_141104_orig.wav, 15 min
    in → TX01_MIC004_20261013_142604_orig.wav), else "_from-15m00s" added."""
    stem, suffix = Path(name).stem, Path(name).suffix
    new = None
    m = mt.NAME_RE.match(stem)
    if m:
        ts = m.group("ts")
        sep = ts[8] if len(ts) == 15 else ""
        try:
            t = datetime.strptime(re.sub(r"[_-]", "", ts), "%Y%m%d%H%M%S") + timedelta(seconds=int(shift_s))
            new = stem[:m.start("ts")] + t.strftime(f"%Y%m%d{sep}%H%M%S") + stem[m.end("ts"):] + suffix
        except ValueError:
            pass
    if new is None:
        new = f"{stem}_from-{int(shift_s // 60)}m{int(shift_s % 60):02d}s{suffix}"
    while new in taken:
        new = f"{Path(new).stem}_{os.urandom(2).hex()}{suffix}"
    return new


def _edit_tracks(item, src_dir, dst_dir, t0, t1, silence):
    """The recorder files for seconds [t0, t1) of a multi-mic recording (t1
    None: to its end), each `silence` range (seconds) silenced, into
    dst_dir/tracks. A file wholly inside is cloned; one an edge falls in is cut,
    the piece after t0 named and time-referenced from where it starts so that
    it still chains; one outside is left out, and so is a mic with nothing
    inside. Returns (track records on the new timeline, indices of the mics kept)."""
    out_dir = Path(dst_dir) / "tracks"
    out_dir.mkdir()
    records, kept, taken = [], [], set()
    for k, rec in enumerate(item["tracks"]):
        paths = [Path(src_dir) / "tracks" / n for n in (rec.get("parts") or [rec["file"]])]
        durs = []
        for p in paths:
            try:
                frames, rate = mt.wav_frames(p)
                durs.append(frames / rate)
            except ValueError:
                durs.append(mt.file_duration(p))
        gaps, exact = mt.part_gaps([str(p) for p in paths], durs, with_exact=True)
        starts = [0.0]
        for dur, gap in zip(durs, gaps):
            starts.append(starts[-1] + dur + gap)
        # this mic's file time (seconds into its chain of parts) at time t of the recording
        o, d = rec.get("offset") or 0.0, rec.get("drift") or 0.0
        chain = lambda t: o + t * (1 + d)
        c0 = chain(t0)
        c1 = math.inf if t1 is None else chain(t1)
        names, first = [], None
        for j, p in enumerate(paths):
            s, e = starts[j], starts[j] + durs[j]
            lo, hi = max(c0, s), min(c1, e)
            if hi - lo < 0.001:
                continue
            pad = 0.0 if all(exact[:j]) else EDIT_PAD_S
            quiet = [(chain(a) - pad - s, chain(b) + pad - s) for a, b in silence]
            quiet = [(a, b) for a, b in quiet if b > lo - s and a < hi - s]
            to_end = hi > e - 0.001
            name = p.name if lo - s < 0.001 else _shifted_name(p.name, lo - s, taken)
            if lo - s < 0.001 and to_end and not quiet:
                _clone(p, out_dir / name)
            else:
                name = _edit_audio(p, out_dir, Path(name).stem, lo - s, None if to_end else hi - s, quiet).name
            taken.add(name)
            names.append(name)
            first = lo if first is None else first
            last_end = hi
        if not names:
            continue
        offset = c0 - first  # the new chain starts `first` seconds into the old one
        new = {**rec, "file": names[0],
               "offset": round(offset, 6), "start": round(-offset / (1 + d), 3),
               "end": round((last_end - first - offset) / (1 + d), 3)}
        original = (rec.get("original") or names[0]) if names[0] == rec["file"] else names[0]
        new["original"] = original.split(" (+")[0] + (f" (+{len(names) - 1} more)" if len(names) > 1 else "")
        if len(names) > 1:
            new["parts"] = names
        else:
            new.pop("parts", None)
        records.append(new)
        kept.append(k)
    return records, kept


def _edit_transcript(transcript, clean, t0, t1, silence):
    """The transcript and clean-up of milliseconds [t0, t1) of a recording with
    the `silence` ranges taken out: their words dropped, the rest moved so that
    t0 is 0, and the clean-up renumbered (an edit is kept only if all its words are)."""
    def kept(w):
        mid = (w["start"] + w["end"]) / 2
        return t0 <= mid < t1 and not any(a <= mid < b for a, b in silence)

    old = transcript["words"]
    words, renum = [], {}
    for i, w in enumerate(old):
        if kept(w):
            renum[i] = len(words)
            words.append({**w, "start": max(0, w["start"] - t0), "end": max(0, min(t1, w["end"]) - t0)})
    transcript = {**transcript, "words": words, "utterances": mt.regroup_utterances(words),
                  "text": " ".join(w["text"] for w in words), "audio_duration_ms": t1 - t0}
    if clean is None:
        return transcript, None

    def span(ed):
        s, e = ed["s"], min(ed["e"], len(old) - 1)
        return s <= e and all(i in renum for i in range(s, e + 1))

    clean = {**clean, "total_words": len(words),
             "removed": [renum[i] for i in clean.get("removed") or [] if i in renum],
             "edits": [{**ed, "s": renum[ed["s"]], "e": renum[min(ed["e"], len(old) - 1)]}
                       for ed in clean.get("edits") or [] if span(ed)]}
    if clean.get("replacements"):  # the older format: {word number: text}
        clean["replacements"] = {str(renum[int(i)]): text for i, text in clean["replacements"].items()
                                 if int(i) in renum}
    return transcript, clean


def _merge_cuts(ranges, length):
    """Cut ranges (ms) clipped to [0, length), sorted, overlaps merged."""
    out = []
    for a, b in sorted((max(0, a), min(length, b)) for a, b in ranges):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _build_version(item, src, dst, t0, t1, silence):
    """Write into the empty folder `dst` the version of recording `item` (its
    files in `src`) that keeps milliseconds [t0, t1) of it with each `silence`
    range (ms) silenced. Returns the item fields that change."""
    dst.chmod(0o755)  # it becomes the recording's folder (mkdtemp makes it private)
    dur = item["duration_ms"]
    s0, s1 = t0 / 1000, None if t1 >= dur else t1 / 1000
    quiet = [(a / 1000, b / 1000) for a, b in silence]
    audio = _edit_audio(src / f"audio{item['ext']}", dst, "audio", s0, s1, quiet)
    changes = {"ext": audio.suffix, "duration_ms": t1 - t0, "denoise": item.get("denoise"),
               "cuts": _merge_cuts([(a - t0, b - t0) for a, b in (item.get("cuts") or []) + list(silence)], t1 - t0)}
    if (src / "denoised.m4a").is_file():
        _edit_audio(src / "denoised.m4a", dst, "denoised", s0, s1, quiet)
        changes["denoise"] = "ready"
    clean = None
    if item.get("clean_status") == "ready" and (src / "clean.json").is_file():
        clean = json.loads((src / "clean.json").read_text())
    transcript, clean = _edit_transcript(json.loads((src / "transcript.json").read_text()), clean, t0, t1, silence)
    if item.get("tracks"):
        tracks, kept = _edit_tracks(item, src, dst, s0, s1, quiet)
        if not tracks:
            raise ValueError("None of the mics recorded anything in that part")
        changes["tracks"] = tracks
        if (src / "speakers.npz").is_file():
            mt.slice_levels(src / "speakers.npz", dst / "speakers.npz",
                            round(t0 / mt.FRAME_MS), round(t1 / mt.FRAME_MS), kept)
        if transcript.get("mics"):
            transcript["mics"] = [m for i, m in enumerate(transcript["mics"]) if i in kept]
    (dst / "transcript.json").write_text(json.dumps(transcript))
    if clean is not None:
        (dst / "clean.json").write_text(json.dumps(clean))
    return changes


def _clock(ms):
    s = int(ms // 1000)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _later(iso, seconds):
    """An ISO time `seconds` later (None stays None)."""
    if not iso:
        return iso
    return (datetime.fromisoformat(iso) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _begin_edit(item_id, status):
    """The recording to cut or split, marked busy; raises ValueError if it can't be now."""
    if mt is None:
        raise ValueError(f"Cutting and splitting need numpy ({MULTITRACK_ERROR})")
    with LOCK:
        item = ITEMS.get(item_id)
        if item is None:
            raise ValueError("No such recording")
        if (item["status"] != "ready" or item.get("clean_status") == "working"
                or item.get("denoise") == "working" or not item.get("duration_ms")):
            raise ValueError("Wait until the recording is done processing")
        d = item_dir(item)
        files = [audio_path(item), d / "transcript.json"] + [p for parts in track_paths(item) for p in parts]
        missing = [p.name for p in files if not p.is_file()]
        if missing:
            raise ValueError(f"Some of its files are missing: {', '.join(missing)}")
        item["status"] = status  # busy: the rescan, the late matcher and other edits leave it alone
    return item


def _finish_edit(item, versions):
    """Put the new versions [(staging folder, changed fields)] of `item` in its
    place, the folder, flags and notes as they are now; the old one goes to the
    archive. Returns (new items, where the old one went)."""
    out = []
    with LOCK:
        archived = archive_item(item)
        ITEMS.pop(item["id"], None)
        ITEM_MTIME.pop(item["id"], None)
        for staging, changes in versions:
            new = {k: v for k, v in item.items() if k != "combined"}
            new.update(changes, status="ready", error=None)
            dst = item_dir(new)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging), str(dst))
            ITEMS[new["id"]] = new
            out.append(new)
    for new in out:
        save_item(new)
    return out, archived


def cut_item(item_id, start_ms, end_ms):
    """Cut milliseconds [start_ms, end_ms) out of a finished recording. A cut
    from the start or to the end trims the recording. One in the middle
    silences that stretch in every file — the playback audio, its denoised
    copy and each recorder's own file — so the recording's timeline and the
    recorders' timing stay as they were (a remix still lines up), and the
    player skips it (item["cuts"]). The words said in it leave the
    transcript. Raises ValueError."""
    item = _begin_edit(item_id, "cutting")
    staging = None
    try:
        dur = item["duration_ms"]
        a, b = max(0, int(start_ms)), min(dur, int(end_ms))
        if b - a < 50:
            raise ValueError("Pick a stretch to cut")
        if a <= EDIT_EDGE_MS and b >= dur - EDIT_EDGE_MS:
            raise ValueError("That's all of it — archive the recording instead (✕ in the library)")
        if a <= EDIT_EDGE_MS:
            t0, t1, silence = b, dur, []
        elif b >= dur - EDIT_EDGE_MS:
            t0, t1, silence = 0, a, []
        else:
            t0, t1, silence = 0, dur, [(a, b)]
        staging = Path(tempfile.mkdtemp(prefix=".edit-", dir=LIBRARY))
        changes = _build_version(item, item_dir(item), staging, t0, t1, silence)
        if t0:
            changes["recorded"] = _later(item.get("recorded"), t0 / 1000)
            if not item.get("tracks") and mt.parse_name(item["title"])[2] is not None:
                changes["title"] = _shifted_name(item["title"], t0 / 1000)  # it's the recorder file
    except BaseException:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        with LOCK:
            item["status"] = "ready"
        raise
    (new,), archived = _finish_edit(item, [(staging, changes)])
    what = "trimmed off the start" if t0 else "trimmed off the end" if t1 < dur else "silenced and skipped"
    print(f"  [{new.get('ai_title') or new['title']}] cut {_clock(a)}–{_clock(b)} ({what}); "
          f"the uncut recording is archived at {archived}")
    return new


def split_item(item_id, at_ms):
    """Split a finished recording in two at `at_ms`, e.g. where a conversation
    moved on to something else: each half gets its share of the audio, of
    every recorder's file (cut where needed) and of the transcript, keeps the
    folder, flags, notes and speaker names, and is titled afresh by Claude
    (a title you typed is kept, numbered). Raises ValueError."""
    item = _begin_edit(item_id, "splitting")
    stagings = []
    try:
        dur, at = item["duration_ms"], int(at_ms)
        if not 1000 <= at <= dur - 1000:
            raise ValueError("Split somewhere inside the recording")
        versions = []
        for n, (t0, t1) in enumerate(((0, at), (at, dur)), 1):
            stagings.append(Path(tempfile.mkdtemp(prefix=".edit-", dir=LIBRARY)))
            changes = _build_version(item, item_dir(item), stagings[-1], t0, t1, [])
            title = item["title"]
            if changes.get("tracks"):
                title = group_title([t["file"] for t in changes["tracks"]])
            elif mt.parse_name(title)[2] is not None:
                title = _shifted_name(title, t0 / 1000)  # the recorder file it is
            else:
                title = f"{Path(title).stem} (part {n}){Path(title).suffix}"
            slug = re.sub(r"[^a-z0-9]+", "-", Path(title).stem.lower()).strip("-")[:40] or "recording"
            changes.update(id=f"{slug}-{os.urandom(3).hex()}", title=title,
                           added=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           recorded=_later(item.get("recorded"), t0 / 1000),
                           ai_title=f"{item['ai_title']} (part {n})" if item.get("title_manual") else None)
            versions.append((stagings[-1], changes))
    except BaseException:
        for s in stagings:
            shutil.rmtree(s, ignore_errors=True)
        with LOCK:
            item["status"] = "ready"
        raise
    new, archived = _finish_edit(item, versions)
    print(f"  split “{item.get('ai_title') or item['title']}” at {_clock(at)} into two; "
          f"the original is archived at {archived}")
    for it in new:
        if not it.get("ai_title"):
            threading.Thread(target=generate_title, args=(it["id"],), daemon=True).start()
    return new


COMBINED_DIR = "originals"  # inside a combined recording: the ones it was made from, for UNCOMBINE


def merged_notes(items):
    """The notes of several recordings that become one: kept out of training if
    any was, their free-text notes joined."""
    return {"no_training": any(it.get("no_training") for it in items),
            "training_ask": join_notes(*(it.get("training_ask") for it in items))}


def _combine_name(it, path, when=None):
    """A single-file recording's audio as a recorder file: its title when that
    carries a recorder timestamp (a DJI file added on its own), else
    reel_<when it was recorded> (or `when`, epoch seconds), so recordings made
    one after another chain as one recorder and ones made at the same time
    stay separate mics."""
    title = safe_name(it["title"])
    if when is None and mt.parse_name(title)[2] is not None and Path(title).suffix.lower() == path.suffix.lower():
        return title
    when = (datetime.fromtimestamp(when) if when is not None
            else datetime.fromisoformat(it.get("recorded") or it["added"])).astimezone()
    return f"reel_{when:%Y%m%d_%H%M%S}{path.suffix.lower()}"


def _back_to_back(items):
    """Voice notes are joined end to end, a second or two apart, in the order
    they were sent: the time between them was spent listening, not talking.
    {id: start, epoch seconds} when every one of `items` is a voice note, else {}."""
    if not all(is_voice_note(it) and it.get("duration_ms") for it in items):
        return {}
    starts, t = {}, None
    for it in sorted(items, key=lambda it: signal_sent(it["title"])):
        own = math.floor(signal_sent(it["title"]) - it["duration_ms"] / 1000)
        t = own if t is None else t
        starts[it["id"]] = t
        t = math.ceil(t + it["duration_ms"] / 1000) + 1
    return starts


def combine_items(ids):
    """Make one recording of several picked in the library — mics of the same
    conversation, or recordings made one after another — from their recorder
    files, as --regroup does: chained by recorder, aligned, mixed and
    transcribed afresh (costs a transcription). Unlike --regroup it can be
    undone: each original, minus its recorder files, moves inside the new one
    (originals/<id>/) and uncombine_item puts them back as they were. All must
    be in one folder. Raises ValueError with a message for the UI."""
    if mt is None:
        raise ValueError(f"Combining needs numpy ({MULTITRACK_ERROR})")
    ids = list(dict.fromkeys(ids))
    if len(ids) < 2:
        raise ValueError("Pick at least two recordings to combine")
    with LOCK:
        items = [ITEMS.get(i) for i in ids]
        if None in items:
            raise ValueError("One of those recordings is gone (archived?)")
        for it in items:
            if (it["status"] not in ("ready", "error") or it.get("clean_status") == "working"
                    or it.get("denoise") == "working"):
                raise ValueError(f"“{it.get('ai_title') or it['title']}” is still being processed "
                                 f"— try again once it's done")
        if len({it.get("group") for it in items}) > 1:
            raise ValueError("Those recordings are in different folders: move them into one folder first")
        before = {it["id"]: it["status"] for it in items}
        for it in items:
            it["status"] = "combining"  # busy: the rescan, the late matcher and edits leave it alone
    try:
        # (current path, name in the combined recording, original id, path inside the original)
        sources = []
        mics = []  # indices into sources: each recording's mics, kept whole
        joined = _back_to_back(items)
        for it in items:
            if it.get("tracks"):
                for parts in track_paths(it):
                    mics.append(list(range(len(sources), len(sources) + len(parts))))
                    sources += [(p, p.name, it["id"], f"tracks/{p.name}") for p in parts]
            else:
                p = audio_path(it)
                mics.append([len(sources)])
                sources.append((p, _combine_name(it, p, joined.get(it["id"])), it["id"], p.name))
        missing = [p.name for p, _, _, _ in sources if not p.is_file()]
        if missing:
            raise ValueError(f"Some of their recorder files are missing: {', '.join(missing)}")
        taken = set()
        for k, (p, name, oid, rel) in enumerate(sources):
            while name in taken:
                name = f"{Path(name).stem}_{os.urandom(2).hex()}{Path(name).suffix}"
            taken.add(name)
            sources[k] = (p, name, oid, rel)
        if not mt._tool("ffmpeg") and any(Path(n).suffix.lower() in (".webm", ".mkv") for _, n, _, _ in sources):
            raise ValueError("Combining a browser recording (WebM) needs ffmpeg on this Mac: brew install ffmpeg")
        staging = LIBRARY / f".combine-{os.urandom(4).hex()}"
        durations = {}
        for p, name, _, _ in sources:
            try:
                durations[staging / name] = mt.file_duration(p)
            except Exception as err:
                raise ValueError(f"Couldn't read {p.name}: {err}")
        # Each recording's mics stay whole (they were sorted when it was made);
        # they're only joined end to end — a recorder restarted, or recordings
        # made one after another (however far apart: these were picked
        # together) — never taken apart and re-chained, where two same-named
        # transmitters restarted together could swap files.
        here = {staging / name: p for p, name, _, _ in sources}  # not moved into staging yet
        chains = mt.fold_recorders([[staging / sources[k][1] for k in mic] for mic in mics], durations,
                                   fingerprint=lambda ch: mt.file_fingerprint([here[n] for n in ch],
                                                                              LIBRARY / ".fpcache"),
                                   max_gap_s=float("inf"))
    except BaseException:
        with LOCK:
            for it in items:
                it["status"] = before[it["id"]]
        raise
    staging.mkdir()
    for p, name, _, _ in sources:
        shutil.move(str(p), staging / name)
    combined = [{"id": it["id"], "title": it.get("ai_title") or it["title"],
                 "files": {name: rel for _, name, oid, rel in sources if oid == it["id"]}} for it in items]
    item = add_group(chains, group=items[0].get("group"), notes=merged_notes(items), combined=combined)
    shutil.rmtree(staging, ignore_errors=True)
    root = item_dir(item) / COMBINED_DIR
    root.mkdir()
    for it in items:
        shutil.move(str(item_dir(it)), str(root / it["id"]))
    with LOCK:
        for it in items:
            ITEMS.pop(it["id"], None)
            ITEM_MTIME.pop(it["id"], None)
    print(f"  combined {len(items)} recordings into “{item['title']}” "
          f"({len(chains)} mic{'s' if len(chains) != 1 else ''}, {len(sources)} files) — transcribing")
    return item


def uncombine_item(item_id):
    """Undo combine_items: the recordings it was made from come back as they
    were (titles, transcripts, clean-ups, speaker names) with their recorder
    files, into the combined one's folder. The combined recording goes to the
    archive. Raises ValueError."""
    with LOCK:
        item = ITEMS.get(item_id)
        if item is None:
            raise ValueError("No such recording")
        if not item.get("combined"):
            raise ValueError("This recording wasn't combined in Reel, so there is nothing to uncombine")
        # A denoised copy still in the making is moot: denoise_item just fails once the folder is archived.
        if item["status"] not in ("ready", "error") or item.get("clean_status") == "working":
            raise ValueError("Wait until the recording is done processing")
        base = item_dir(item)
        tracks, root = base / "tracks", base / COMBINED_DIR
        plan = []
        for o in item["combined"]:
            dst = place_dir(item.get("group")) / o["id"]
            if not (root / o["id"] / "item.json").is_file() or not all((tracks / n).is_file() for n in o["files"]):
                raise ValueError("Some of the files it was made from are missing")
            if dst.exists():
                raise ValueError(f"{dst.relative_to(DATA)} is already there")
            plan.append((o, dst))
        item["status"] = "uncombining"
    restored = []
    try:
        for o, dst in plan:
            src = root / o["id"]
            for name, rel in o["files"].items():
                (src / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(tracks / name), str(src / rel))
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            it = _read_item(dst, item.get("group"))
            with LOCK:
                ITEMS[o["id"]] = it
            save_item(it)
            restored.append(it)
    except BaseException as err:
        set_status(item, "error", f"Uncombining stopped partway ({err}); {len(restored)} of "
                                  f"{len(plan)} recordings are back")
        raise
    archive_item(item)
    with LOCK:
        ITEMS.pop(item_id, None)
        ITEM_MTIME.pop(item_id, None)
    print(f"  uncombined “{item.get('ai_title') or item['title']}” back into "
          + ", ".join(f"“{it.get('ai_title') or it['title']}”" for it in restored))
    return restored


def join_notes(*notes):
    """Several recordings' free-text notes (who to ask) as one, without repeats."""
    out = []
    for n in notes:
        n = " ".join((n or "").split())
        if n and n not in out:
            out.append(n)
    return "; ".join(out)


def regroup_items(ids):
    """Rebuild one conversation from the recorder files of several library
    items — e.g. two single-mic recordings the inbox failed to pair, or a
    conversation that got a recorder's restart as an extra mic. The files are
    chained by recorder afresh and grouped; the old items (and their
    transcripts, titles and clean-ups) are removed. Costs one transcription."""
    if mt is None:
        raise SystemExit(f"Multi-mic support needs numpy ({MULTITRACK_ERROR})")
    items = []
    for iid in ids:
        it = ITEMS.get(iid)
        if it is None:
            raise SystemExit(f"No such recording: {iid}")
        if it["status"] not in ("ready", "error"):
            raise SystemExit(f"{iid} is still being processed — try again later")
        items.append(it)
    for it in items:
        missing = [p.name for parts in track_paths(it) for p in parts if not p.is_file()]
        if missing:
            raise SystemExit(f"{it['id']}: recorder file missing: {', '.join(missing)}")
    staging = LIBRARY / f".regroup-{os.urandom(4).hex()}"
    staging.mkdir()
    files = []
    for it in items:
        if it.get("tracks"):
            for parts in track_paths(it):
                for src in parts:
                    dst = staging / src.name  # recorder names carry the timestamps
                    shutil.move(str(src), dst)
                    files.append(dst)
        else:
            dst = staging / safe_name(it["title"])
            shutil.move(str(audio_path(it)), dst)
            files.append(dst)
    durations = [mt.file_duration(p) for p in files]
    names = [str(p) for p in files]
    chains = mt.chain_parts(names, durations)
    chains = mt.fold_recorders(chains, dict(zip(names, durations)),
                               fingerprint=lambda ch: mt.file_fingerprint(ch, LIBRARY / ".fpcache"))
    notes = merged_notes(items)
    group = items[0].get("group") or DEFAULT_GROUP
    for it in items:
        with LOCK:
            ITEMS.pop(it["id"], None)
        shutil.rmtree(item_dir(it), ignore_errors=True)
    item = add_group([[Path(c) for c in ch] for ch in chains], group=group, notes=notes)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"Regrouped {len(ids)} recordings into “{item['title']}” "
          f"({len(chains)} mic{'s' if len(chains) != 1 else ''}, {sum(len(c) for c in chains)} files) — transcribing")
    return item


def rechain_tracks(tracks, tracks_dir):
    """Re-sort a recording's recorder files into mics after late files were
    added: a late file may continue a recorder that's already there (its last
    30-minute segments copied in after the rest), and then it's that mic's
    next part — not another person. Each mic, the ones there and the ones
    just added, stays whole (its files were sorted when it came in): mics
    are only joined end to end (fold_recorders), never taken apart, where two
    same-named transmitters restarted together could swap files."""
    if mt is None:
        return tracks
    chains = [[str(tracks_dir / f) for f in (t.get("parts") or [t["file"]])] for t in tracks]
    names = [n for ch in chains for n in ch]
    try:
        durations = [mt.file_duration(n) for n in names]
    except Exception:
        return tracks  # unreadable file: keep the tracks as they are
    chains = mt.fold_recorders(chains, dict(zip(names, durations)),
                               fingerprint=lambda ch: mt.file_fingerprint(ch, LIBRARY / ".fpcache"))
    if len(chains) == len(tracks):
        return tracks
    originals = {f: t.get("original") for t in tracks for f in (t.get("parts") or [t["file"]])}
    out = []
    for ch in chains:
        files = [Path(n).name for n in ch]
        rec = {"file": files[0], "original": (originals.get(files[0]) or files[0]).split(" (+")[0]}
        if len(files) > 1:
            rec["parts"] = files
            rec["original"] += f" (+{len(files) - 1} more)"
        out.append(rec)
    print(f"  late files continue a recorder already here: {len(tracks)} tracks → {len(out)} mics")
    return out


def attach_track(item_id, src):
    """A late-arriving recorder file (or list of consecutive parts) for an
    existing recording: add it as a new mic and redo everything."""
    parts = [Path(p) for p in src] if isinstance(src, (list, tuple)) else [Path(src)]
    return attach_tracks(item_id, [parts])


def attach_tracks(item_id, mics):
    """Several late mics at once (each a list of one recorder's parts)."""
    mics = [[Path(p) for p in parts] for parts in mics]
    src = mics[0][0]
    with LOCK:
        item = ITEMS.get(item_id)
        if item is None:
            return None
        if item["status"] not in ("ready", "error") or item.get("clean_status") == "working":
            return None
        if item.get("tracks") and not all(p.is_file() for parts in track_paths(item) for p in parts):
            return None  # its own recorder files are missing: a rebuild would fail
        d = item_dir(item_id)
        tracks_dir = d / "tracks"
        tracks_dir.mkdir(exist_ok=True)
        previous = _keep_previous(item)
        promoted = None
        if not item.get("tracks"):
            # Promote a single-track recording: its audio becomes mic 1.
            first = tracks_dir / safe_name(item["title"] if Path(item["title"]).suffix else f"audio{item['ext']}")
            shutil.move(str(audio_path(item)), first)
            item["tracks"] = [{"file": first.name, "original": item["title"]}]
            item["single_title"] = item["title"]
            promoted = first.name
        added = []
        for parts in mics:
            rec = move_parts(parts, tracks_dir)
            item["tracks"].append(rec)
            added.append(rec.get("parts") or [rec["file"]])
        item["tracks"] = rechain_tracks(item["tracks"], tracks_dir)
        # What _undo_attach needs to put it all back if the rebuild fails.
        item["adding"] = {"previous": previous, "mics": added, "promoted": promoted}
        item.pop("mics_failed", None)
        for t in item["tracks"]:
            t.pop("offset", None)  # forces a fresh align + mix
        for old in ("audio.m4a", "audio.wav", "speakers.npz", "transcript.json", "clean.json", "denoised.m4a"):
            (d / old).unlink(missing_ok=True)
        item["denoise"] = None
        item["ext"] = None
        item["clean_status"] = None
        item["clean_error"] = None
        item["transcript_id"] = None
        item["title"] = group_title([t["original"] for t in item["tracks"]])
        if not item.get("title_manual"):  # a title typed by hand survives a rebuild
            item["ai_title"] = None
        item["recorded"] = recorded_at(item)
        set_status(item, "queued")
    more = sum(len(m) for m in mics) - 1
    print(f"  [{item['title']}] recorder file {src.name}{f' (+{more} more)' if more else ''} attached — re-aligning")
    threading.Thread(target=multitrack_worker, args=(item_id,), daemon=True).start()
    return item


# A rebuild after adding mics throws away the mix, transcript and clean-up it
# replaces. So Reel first keeps them (APFS clones: no extra disk), and if the
# rebuild fails, puts the recording back exactly as it was and sets the new
# mics' files aside in held/, for the page to say why and offer to try again.
HELD_DIR = "held"
REBUILT_KEYS = ("tracks", "ext", "duration_ms", "title", "ai_title", "single_title", "recorded", "transcript_id",
                "language", "clean_status", "clean_error", "denoise", "mix", "speaker_stats", "label_version",
                "status", "error")


def _keep_previous(item):
    """Clone a recording's own files (not its recorder files, which a rebuild
    keeps) and its item.json into archive/before-rebuild/; returns that
    folder, relative to the archive."""
    d = item_dir(item)
    dst = archive_path(item["id"], sub="before-rebuild")
    dst.mkdir()
    for f in d.iterdir():
        if f.is_file() and not f.name.startswith(".") and f.name != "item.json":
            _clone(f, dst / f.name)
    (dst / "item.json").write_text(json.dumps(item, indent=2))
    return str(dst.relative_to(ARCHIVE))


def _undo_attach(item, error):
    """The rebuild after adding mics failed: back to the recording as it was."""
    add = item.get("adding") or {}
    prev = ARCHIVE / (add.get("previous") or "-")
    try:
        old = json.loads((prev / "item.json").read_text())
    except (OSError, ValueError):
        set_status(item, "error", error)  # nothing kept to go back to
        return
    d = item_dir(item)
    tracks_dir, held = d / "tracks", d / HELD_DIR
    held.mkdir(exist_ok=True)
    for mic in add.get("mics") or []:
        for n in mic:
            if (tracks_dir / n).is_file():
                shutil.move(str(tracks_dir / n), held / n)
    for f in d.iterdir():  # what the failed attempt got as far as writing
        if f.is_file() and not f.name.startswith(".") and f.name != "item.json":
            f.unlink()
    for f in prev.iterdir():
        if f.is_file() and f.name != "item.json":
            _clone(f, d / f.name)
    if add.get("promoted") and old.get("ext") and (d / f"audio{old['ext']}").is_file():
        (tracks_dir / add["promoted"]).unlink(missing_ok=True)  # the single recording's audio, back in place
        try:
            tracks_dir.rmdir()
        except OSError:
            pass
    with LOCK:
        for k in REBUILT_KEYS:
            if k in old:
                item[k] = old[k]
            else:
                item.pop(k, None)
        item.pop("adding", None)
        item["mics_failed"] = {"mics": add.get("mics") or [], "error": error}
    save_item(item)
    n = len(add.get("mics") or [])
    print(f"  [{item.get('ai_title') or item['title']}] adding {n} mic{'s' if n != 1 else ''} failed, "
          f"so it's back as it was (their files are in {HELD_DIR}/): {error}")


def held_mics(item_id, action):
    """The mics a failed rebuild set aside: "retry" adds them again, "drop"
    moves their files to the archive and forgets them."""
    with LOCK:
        item = ITEMS.get(item_id)
        if item is None or not item.get("mics_failed"):
            raise ValueError("No mics waiting to be added to this recording")
        if item["status"] not in ("ready", "error"):
            raise ValueError("Wait until it has finished processing")
        held = item_dir(item) / HELD_DIR
        mics = [[held / n for n in mic] for mic in item["mics_failed"]["mics"]]
    mics = [[p for p in mic if p.is_file()] for mic in mics]
    mics = [mic for mic in mics if mic]
    if action == "retry":
        if not mics:
            raise ValueError(f"Their files aren't in {HELD_DIR}/ any more")
        if attach_tracks(item_id, mics) is None:
            raise ValueError("It can't take them right now (still busy, or its own recorder files are missing)")
    elif action == "drop":
        dst = archive_path(f"{item_id}-{HELD_DIR}")
        if held.is_dir():
            shutil.move(str(held), dst)
        with LOCK:
            item.pop("mics_failed", None)
        save_item(item)
        print(f"  [{item.get('ai_title') or item['title']}] left the held mics out (files in {ARCHIVE_DIR}/{dst.name})")
    else:
        raise ValueError(f"Unknown action {action!r}")
    with LOCK:
        return dict(ITEMS[item_id])


# ------------------------------------------------------------- late matches
# One conversation can still end up as two recordings: two mics' files copied
# into the inbox together become a recording of their own even when a third
# mic of the same conversation was transcribed earlier. When two finished
# recordings overlap in time and their audio matches, the newer is merged into
# the older: its mics are attached (re-align, re-mix, re-transcribe) and its
# separate copy archived.
LATE_CHECKED = set()  # (donor id, keeper id) pairs already compared
LATE_LOCK = threading.Lock()


def _keeper_of(a, b):
    """Of two recordings of one conversation, the one that stays: the older."""
    return min(a, b, key=lambda i: (i.get("added") or "", i["id"]))


def _recorder_files(it):
    names = set()
    for t in it.get("tracks") or []:
        names.update(t.get("parts") or [t.get("file")])
    if not it.get("tracks"):
        names.add(safe_name(it.get("title") or ""))
    names.discard(None)
    return names


def _files_here(it):
    """True when a recording's audio and every recorder file of it are on disk."""
    return audio_path(it).is_file() and all(p.is_file() for parts in track_paths(it) for p in parts)


def late_match_pass():
    """Look for one pair of recordings to merge (see above)."""
    if mt is None or not LATE_LOCK.acquire(blocking=False):
        return
    try:
        with INBOX_LOCK:
            if any(s["status"] not in ("skipped",) for s in INBOX_STATE.values()):
                return  # files being sorted may be about to attach; wait for them
        with LOCK:  # (both of a pair need all their files: a rebuild starts from them)
            ready = [dict(i) for i in ITEMS.values()
                     if i["status"] == "ready" and i.get("clean_status") != "working"
                     and _from_recorder(i) and _item_span(i) and _files_here(i)]
        for donor in ready:
            for keeper in ready:
                if keeper["id"] == donor["id"] or _keeper_of(keeper, donor) is not keeper:
                    continue
                if _overlap_s(_item_span(donor), _item_span(keeper)) < mt.MATCH_MIN_OVERLAP_S:
                    continue
                if _recorder_files(donor) & _recorder_files(keeper):
                    continue  # the same recorder files ingested twice: not two mics
                pair = (donor["id"], keeper["id"])
                if pair in LATE_CHECKED:
                    continue
                LATE_CHECKED.add(pair)
                try:
                    fp = mt.file_fingerprint(audio_path(donor), LIBRARY / ".fpcache")
                except Exception:
                    continue
                t0 = _item_span(donor)[0]
                score = _match_score(keeper, fp, t0)
                if score is None:
                    continue
                print(f"  late match: “{donor.get('ai_title') or donor['title']}” is the same conversation as "
                      f"“{keeper.get('ai_title') or keeper['title']}” (score {score:.0f}) — merging")
                if merge_into(keeper["id"], donor["id"]) is None:
                    LATE_CHECKED.discard(pair)  # one of them was busy: try again on a later pass
                    continue
                return  # one merge per pass; the library has changed
    finally:
        LATE_LOCK.release()


def merge_into(keeper_id, donor_id):
    """Attach the donor's recorder files to the keeper as more mics, then
    archive what's left of the donor (its transcript, mix, …). None when
    either can't be touched right now."""
    with LOCK:
        keeper, donor = ITEMS.get(keeper_id), ITEMS.get(donor_id)
        if (keeper is None or donor is None or donor["status"] not in ("ready", "error")
                or keeper["status"] not in ("ready", "error") or keeper.get("clean_status") == "working"
                or not _files_here(keeper)):
            return None
        staging = None
        if donor.get("tracks"):
            mics = track_paths(donor)
        else:  # a single recording's audio is its recorder file: give it back its name
            staging = LIBRARY / f".merge-{os.urandom(4).hex()}"
            staging.mkdir()
            mics = [[staging / safe_name(donor["title"])]]
            shutil.move(str(audio_path(donor)), mics[0][0])
        if attach_tracks(keeper_id, mics) is None:
            if staging:
                shutil.move(str(mics[0][0]), audio_path(donor))
                shutil.rmtree(staging, ignore_errors=True)
            return None
        keeper.setdefault("merged", []).append({"id": donor_id, "title": donor.get("ai_title") or donor["title"]})
        if donor.get("no_training"):
            keeper["no_training"] = True  # audio kept out of training stays out once merged
        keeper["training_ask"] = join_notes(keeper.get("training_ask"), donor.get("training_ask"))
        tidy_notes(keeper)
        donor["status"] = "merging"  # in memory only: busy, so the rescan leaves its folder alone meanwhile
    save_item(keeper)
    try:
        dst = archive_item(donor)
        print(f"  late match: separate copy archived to {dst.name if dst else '(already gone)'}")
    except OSError as err:
        print(f"  late match: couldn't archive {donor_id}: {err}")
    with LOCK:
        ITEMS.pop(donor_id, None)
        ITEM_MTIME.pop(donor_id, None)
    if staging:
        shutil.rmtree(staging, ignore_errors=True)
    return keeper_id


def _read_item(d, group):
    """Load one recording's item.json (None if absent/unreadable), filling defaults."""
    meta = d / "item.json"
    if not meta.is_file():
        return None
    try:
        mtime = meta.stat().st_mtime
        item = json.loads(meta.read_text())
    except (OSError, ValueError):
        return None
    item["group"] = group
    item.setdefault("speaker_names", {})
    for k, v in NOTE_DEFAULTS.items():
        item.setdefault(k, v)
    item.setdefault("ai_title", None)
    item.setdefault("clean_status", None)
    item.setdefault("clean_error", None)
    if not item.get("recorded") or is_voice_note(item):  # earlier Reels took a voice note's upload time
        item["recorded"] = recorded_at(item)
    ITEM_MTIME[item["id"]] = mtime
    return item


def rescan_library():
    """Recordings that appear, change or vanish on disk while Reel runs — a
    folder moved in or out by hand, an item.json edited — are picked up here.
    A recording being worked on is left alone; others are reloaded when their
    item.json changed."""
    seen = set()
    for group, leaf in leaf_folders():
        for d in sorted(leaf.iterdir()):
            meta = d / "item.json"
            if not d.is_dir() or not meta.is_file():
                continue
            seen.add(d.name)
            try:
                mtime = meta.stat().st_mtime
            except OSError:
                continue
            with LOCK:
                cur = ITEMS.get(d.name)
                busy = cur is not None and (cur["status"] not in ("ready", "error") or cur.get("clean_status") == "working")
            if busy or (cur is not None and mtime <= ITEM_MTIME.get(d.name, 0) and cur.get("group") == group):
                continue
            item = _read_item(d, group)
            if item is None:
                continue
            with LOCK:
                ITEMS[item["id"]] = item
            if cur is None:
                print(f"  library: “{item.get('ai_title') or item['title']}” arrived in {leaf.relative_to(DATA)}/")
    with LOCK:
        gone = [i for i, it in ITEMS.items() if i not in seen and it["status"] in ("ready", "error")]
        for i in gone:
            ITEMS.pop(i, None)
            ITEM_MTIME.pop(i, None)
    for i in gone:
        print(f"  library: {i} is gone from disk (deleted or moved elsewhere)")


def library_watcher():
    while True:
        time.sleep(LIBRARY_RESCAN_S)
        try:
            rescan_library()
        except Exception as err:
            print(f"  library rescan: {err}")
        try:
            late_match_pass()
        except Exception as err:
            print(f"  late match: {err}")


def load_library():
    dirs = [(d, g) for g, leaf in leaf_folders() for d in sorted(leaf.iterdir())]
    for d, group in dirs:
        item = _read_item(d, group)
        if item is None:
            continue
        item.setdefault("speaker_names", {})
        item.setdefault("ai_title", None)
        item.setdefault("clean_status", None)
        item.setdefault("clean_error", None)
        if not item.get("recorded"):
            item["recorded"] = recorded_at(item)
        if item["status"] in EDITING:  # saved mid-edit (a flag set meanwhile): the edit never landed
            item["status"] = "ready"
        if item["clean_status"] == "working":
            item["clean_status"] = "error"
            item["clean_error"] = "Interrupted — run clean-up again"
        with LOCK:
            ITEMS[item["id"]] = item
        # The Scribe call is synchronous, so anything caught mid-flight by a
        # shutdown simply restarts from the local audio file (or recorder files).
        if item["status"] not in ("ready", "error"):
            item["transcript_id"] = None
            set_status(item, "queued")
            threading.Thread(target=transcribe_worker, args=(item["id"],), daemon=True).start()


# ----------------------------------------------------------------------- inbox

INBOX_LOCK = threading.Lock()
INBOX_STATE = {}  # file name -> dict(size, mtime, changed, settled, status, error, …)


def inbox_files():
    if not INBOX.is_dir():
        return []
    return sorted(p for p in INBOX.iterdir()
                  if p.is_file() and not p.name.startswith(".")
                  and p.suffix.lower() in AUDIO_EXTS)


def inbox_snapshot():
    """What the UI shows: every file in the inbox with its state."""
    now = time.time()
    out = []
    with INBOX_LOCK:
        for name, s in sorted(INBOX_STATE.items()):
            single_in = hold_in = None
            if s["settled"] and s["status"] == "waiting":
                single_in = max(0, int(s["settled"] + INBOX_SINGLE_DELAY_S - now))
            elif s["settled"] and s["status"] == "holding":
                hold_in = max(0, int(s["settled"] + INBOX_SINGLE_DELAY_S - now))
            out.append({"name": name, "size": s["size"], "status": s["status"],
                        "error": s.get("error"), "single_in": single_in,
                        "hold_in": hold_in, "twin": s.get("twin"),
                        "match": s.get("match"), "waiting_for": s.get("waiting_for"),
                        "waiting_why": s.get("waiting_why")})
        importing = [dict(v, volume=k) for k, v in IMPORTING.items()]
        cleared = [dict(v, volume=k) for k, v in CLEARED.items()]
    return {"folder": str(INBOX), "files": out, "single_delay": INBOX_SINGLE_DELAY_S,
            "available": mt is not None, "error": MULTITRACK_ERROR, "importing": importing,
            "cleared": cleared}


def twin_of(name):
    """For a DJI recorder's processed twin (…_edit.wav): the name of its raw
    twin (…_orig.wav). None for anything else."""
    p = Path(name)
    if p.stem.endswith(INBOX_TWIN_SKIP):
        return p.stem[:-len(INBOX_TWIN_SKIP)] + INBOX_TWIN_USE + p.suffix
    return None


def library_file_names():
    """File names of everything already in the library — single recordings and
    recorder tracks — so a twin whose raw file was taken (even before a
    restart) is still recognised."""
    names = set()
    with LOCK:
        for it in ITEMS.values():
            names.add(it.get("title"))
            names.add(it.get("single_title"))
            for t in it.get("tracks") or []:
                names.add(t.get("original"))
                names.update(t.get("parts") or [t.get("file")])
    names.discard(None)
    return names


def _retire_twin(p, twin):
    """A processed DJI twin whose raw file has been taken: out of the way it goes."""
    INBOX_SKIPPED.mkdir(exist_ok=True)
    dst = INBOX_SKIPPED / p.name
    if dst.exists():
        dst = INBOX_SKIPPED / f"{p.stem}_{os.urandom(2).hex()}{p.suffix}"
    try:
        shutil.move(str(p), dst)
    except OSError as err:
        print(f"  inbox: couldn't move {p.name} to {INBOX_SKIPPED.name}/: {err}")
        return
    print(f"  inbox: {p.name} is the processed twin of {twin}, already in the library "
          f"— moved to {INBOX_SKIPPED.name}/")


def _inbox_refresh_states(force=False):
    """Track size/mtime per file; a file is settled once unchanged for INBOX_SETTLE_S.

    Returns the names ready to be sorted. A DJI recorder's processed twin
    (…_edit.wav) is never among them while its raw twin (…_orig.wav) is in the
    inbox ("skipped"), and is moved to inbox/skipped/ once the raw file has
    been taken into the library. A processed twin whose raw file hasn't shown
    up is "holding" for INBOX_SINGLE_DELAY_S (it is usually still copying),
    then goes ahead as it is; `force` ends the hold."""
    now = time.time()
    seen = set()
    in_library = library_file_names()
    with INBOX_LOCK:
        files = inbox_files()
        present = {p.name for p in files}
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            seen.add(p.name)
            s = INBOX_STATE.get(p.name)
            if s is None or s["size"] != st.st_size or s["mtime"] != st.st_mtime:
                INBOX_STATE[p.name] = s = {"size": st.st_size, "mtime": st.st_mtime, "changed": now,
                                           "settled": None, "status": "copying", "error": None,
                                           "lib_checked": False}
            elif s["settled"] is None and now - s["changed"] >= INBOX_SETTLE_S and st.st_size > 0:
                # (never an empty file: Finder creates every file of a copy up
                # front, empty, and fills them one after another)
                s["settled"] = now
                s["status"] = "waiting"
            twin = twin_of(p.name)
            if twin is None or s["status"] not in ("waiting", "holding", "skipped"):
                continue
            if twin in present:
                s["status"], s["twin"] = "skipped", twin
            elif twin in in_library or safe_name(twin) in in_library:
                _retire_twin(p, twin)
                seen.discard(p.name)
            elif force or now - s["settled"] >= INBOX_SINGLE_DELAY_S:
                s["status"] = "waiting"
                s.pop("twin", None)
            else:
                s["status"], s["twin"] = "holding", twin
        for name in list(INBOX_STATE):
            if name not in seen:
                del INBOX_STATE[name]
        return [n for n, s in INBOX_STATE.items() if s["status"] == "waiting"]


def _set_inbox(name, **fields):
    with INBOX_LOCK:
        s = INBOX_STATE.get(name)
        if s is not None:
            s.update(fields)


def _item_span(it):
    """(start, end) of a recording in epoch seconds from its recorder timestamp
    and duration, or None when either is unknown."""
    try:
        t0 = datetime.fromisoformat(it["recorded"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None
    if not it.get("duration_ms"):
        return None
    return t0, t0 + it["duration_ms"] / 1000


def _from_recorder(it):
    """True when a recording's time comes from recorder file names (DJI), so its
    span can be trusted for matching — browser recordings and uploads can't."""
    if mt is None:
        return False
    names = [it.get("title")] + [t.get("original") for t in it.get("tracks") or []]
    return any(n and mt.parse_name(Path(n.split(" (+")[0]).name)[2] for n in names)


def _overlap_s(a, b):
    return min(a[1], b[1]) - max(a[0], b[0])


def _match_score(it, fp, file_t):
    """How well fingerprint `fp` (a recorder starting at `file_t`, or None)
    matches library recording `it`'s audio: the score if it's the same
    conversation, else None."""
    try:
        other = mt.file_fingerprint(audio_path(it), LIBRARY / ".fpcache")
    except Exception:
        return None  # e.g. a browser .webm afconvert can't read — not a recorder file anyway
    lag, score, overlap = mt.pair_match(other, fp)
    if score >= mt.MATCH_MIN_SCORE and overlap >= mt.MATCH_MIN_OVERLAP_S:
        return score
    if file_t is None or not it.get("recorded"):
        return None
    # The recorder's timestamp says where this file sits in the recording:
    # a weaker peak at that offset is enough (lav mics barely hear each other).
    try:
        rec_t = datetime.fromisoformat(it["recorded"]).timestamp()
    except ValueError:
        return None
    expect = file_t - rec_t
    timed_overlap = min(len(other) / mt.ENV_RATE - expect, len(fp) / mt.ENV_RATE + expect)
    if timed_overlap < mt.MATCH_MIN_OVERLAP_S:
        return None
    lag2, score2, overlap2 = mt.pair_match(other, fp, expect_s=expect)
    if score2 >= mt.MATCH_MIN_SCORE_TIMED and overlap2 >= mt.MATCH_MIN_OVERLAP_S:
        return score2
    return None


def library_match(name, fp):
    """A library recording this file is another mic of, if any (by audio correlation,
    helped by the recorder's timestamp when the file name carries one). Returns
    LIBRARY_BUSY when a recording it may belong to can't be compared yet."""
    if mt is None:
        return None
    dur = len(fp) / mt.ENV_RATE
    file_t = mt.parse_name(Path(name.split(" + ")[0]).name)[2]
    span = (file_t, file_t + dur) if file_t is not None else None
    wait = False
    WAITING_FOR.pop(name, None)
    with LOCK:
        items = [dict(i) for i in ITEMS.values()]
    for it in items:
        it_span = _item_span(it) if _from_recorder(it) else None
        # Timestamps overlapping is enough to be compared, however different the
        # lengths (someone who joined for ten minutes of an hour); without them,
        # only recordings of a similar length are worth correlating.
        timed = span is not None and it_span is not None and _overlap_s(span, it_span) >= mt.MATCH_MIN_OVERLAP_S
        if it["status"] not in ("ready", "error"):
            if timed or (file_t is not None and _busy_overlap(file_t, dur, [it])):
                wait = True
                WAITING_FOR.setdefault(name, (it.get("ai_title") or it["title"], "processing"))
            continue
        if not it.get("duration_ms"):
            continue
        if not timed and not 0.4 < (it["duration_ms"] / 1000) / max(dur, 1) < 2.5:
            continue
        if not audio_path(it).is_file():
            continue
        score = _match_score(it, fp, file_t)
        if score is not None and it.get("clean_status") == "working":
            wait = True  # attaching now would wipe a clean-up that is still being written
            WAITING_FOR.setdefault(name, (it.get("ai_title") or it["title"], "processing"))
            continue
        if score is not None:
            print(f"  inbox: {name} matches “{it.get('ai_title') or it['title']}” (score {score:.0f})")
            return it["id"]
    return LIBRARY_BUSY if wait else None


# library_match's answer when a recording that may be this file's partner is
# still being processed: it can't be compared (or attached to) yet, so the
# file waits and asks again — for up to LIBRARY_BUSY_MAX_S, then goes ahead
# alone (the late match still pairs it up once the partner is ready). Without
# this, a recorder copied in a little after its partner had gone ahead alone
# never paired.
LIBRARY_BUSY = "busy"
WAITING_FOR = {}  # inbox name (or " + "-joined chain) -> (recording title, "processing")
LIBRARY_BUSY_WINDOW_S = 4 * 3600  # how far before the file a busy recording may have started
LIBRARY_BUSY_MAX_S = 30 * 60


def _busy_overlap(file_t, dur, items=None):
    """A library recording still being processed whose recorder timestamp puts
    it within reach of a file starting at `file_t` and lasting `dur` seconds."""
    if items is None:
        with LOCK:
            items = list(ITEMS.values())
    for it in items:
        if it["status"] in ("ready", "error") or not it.get("recorded"):
            continue
        try:
            rec_t = datetime.fromisoformat(it["recorded"]).timestamp()
        except ValueError:
            continue
        if file_t - LIBRARY_BUSY_WINDOW_S <= rec_t <= file_t + dur:
            return True
    return False


def inbox_process(force=False, only=None, group=None):
    """Sort the settled inbox files into conversations.

    force: don't wait for partners — unmatched files go single right away.
    only: process just this file (as a single).  group: these files together."""
    if mt is None or not INBOX.is_dir():
        return
    waiting = _inbox_refresh_states(force=force)
    if group:
        names = [n for n in group if n in waiting]
        if len(names) >= 2:
            for n in names:
                _set_inbox(n, status="grouping")
            add_group([INBOX / n for n in names])
            with INBOX_LOCK:
                for n in names:
                    INBOX_STATE.pop(n, None)
        return
    if only:
        with INBOX_LOCK:
            s = INBOX_STATE.get(only)
            ok = s is not None and s["status"] in ("waiting", "holding")
        if ok:
            _set_inbox(only, status="transcribing")
            add_file(INBOX / only, move=True)
            with INBOX_LOCK:
                INBOX_STATE.pop(only, None)
        return
    if not waiting or (_inbox_held() and not force):
        return  # (a mic's recordings still being copied: its other files are on their way)
    # One recorder may have saved its take as several consecutive files
    # (DJI: a new file every 30 min) — chain those first, then match chains.
    durations = []
    for name in waiting:
        try:
            durations.append(mt.file_duration(INBOX / name))
        except Exception:
            durations.append(0.0)
    chains = mt.chain_parts(waiting, durations)
    fps = []
    for chain in chains:
        key = tuple(chain)
        try:
            fps.append((key, mt.file_fingerprint([INBOX / n for n in chain], INBOX / ".cache")))
        except Exception as err:
            for n in chain:
                _set_inbox(n, status="error", error=f"Couldn't read audio: {err}")
    if not fps:
        return
    groups, _ = mt.group_files(fps)
    now = time.time()

    def forget(names):
        with INBOX_LOCK:
            for n in names:
                INBOX_STATE.pop(n, None)

    dur_by_name = dict(zip(waiting, durations))
    for g in groups:
        keys = [k for k, _, _ in g]
        if len(keys) >= 2 and not force:
            # Don't seal the conversation while its recorders may still have
            # files arriving: wait a little after the newest settled, and the
            # full grace period when a recorder's last file is a full segment.
            with INBOX_LOCK:
                newest = max((INBOX_STATE[n]["settled"] or now) for k in keys for n in k if n in INBOX_STATE)
            last_full = any(SEGMENT_FULL_S[0] <= dur_by_name.get(k[-1], 0) <= SEGMENT_FULL_S[1] for k in keys)
            need = INBOX_SINGLE_DELAY_S if last_full else INBOX_GROUP_DELAY_S
            if now - newest < need:
                continue
        if len(keys) >= 2:
            # A recorder stopped and restarted within the conversation: one person.
            mics = mt.fold_recorders([list(k) for k in keys], dur_by_name, fingerprint=lambda ch: mt.file_fingerprint(
                [INBOX / n for n in ch], INBOX / ".cache"))
            flat = [n for k in mics for n in k]
            for n in flat:
                _set_inbox(n, status="grouping")
            print(f"  inbox: {len(mics)} mics belong together: "
                  + "; ".join(" + ".join(k) for k in mics))
            if len(mics) == 1:
                print(f"  inbox: {' + '.join(mics[0])} is one recorder — transcribing as one recording")
            add_group([[INBOX / n for n in k] for k in mics])
            forget(flat)
            continue
        chain = list(keys[0])
        name = chain[0]
        with INBOX_LOCK:
            s = INBOX_STATE.get(name)
            if s is None:
                continue
            checked = s["lib_checked"]
            settled = max((INBOX_STATE[n]["settled"] or now) for n in chain if n in INBOX_STATE)
        if not checked:
            fp = dict(fps)[keys[0]]
            target = library_match(" + ".join(chain), fp)
            if target == LIBRARY_BUSY:
                with INBOX_LOCK:
                    since = INBOX_STATE[name].setdefault("busy_since", now)
                if not force and now - since < LIBRARY_BUSY_MAX_S:
                    why = WAITING_FOR.get(" + ".join(chain))
                    for n in chain:
                        _set_inbox(n, waiting_for=why[0] if why else None, waiting_why=why[1] if why else None)
                    continue  # ask again on the next pass, once that recording is ready
                target = None  # SORT NOW, or waited long enough: go ahead alone
            for n in chain:
                _set_inbox(n, lib_checked=True)
            if target:
                for n in chain:
                    _set_inbox(n, status="attaching", match=target)
                if attach_tracks(target, [[INBOX / n for n in chain]]) is not None:
                    forget(chain)
                    continue
                for n in chain:
                    _set_inbox(n, status="waiting", match=None)
        if force or now - settled >= INBOX_SINGLE_DELAY_S:
            for n in chain:
                _set_inbox(n, status="transcribing")
            if len(chain) > 1:
                # Consecutive parts of one recorder, nobody else's mic: still one
                # recording — align/mix with a single track just concatenates.
                print(f"  inbox: {' + '.join(chain)} has no partner — transcribing as one recording")
                add_group([[INBOX / n for n in chain]])
            else:
                print(f"  inbox: {name} has no partner — transcribing on its own")
                add_file(INBOX / name, move=True)
            forget(chain)


def prune_caches(max_age_days=14):
    """Fingerprints are keyed by file identity, so old ones are just dead weight."""
    cutoff = time.time() - max_age_days * 86400
    for cache in (INBOX / ".cache", LIBRARY / ".fpcache"):
        if not cache.is_dir():
            continue
        for f in cache.glob("*.npy"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


def inbox_watcher():
    prune_caches()
    while True:
        try:
            inbox_process()
        except Exception as err:
            print(f"  inbox: {err}")
        time.sleep(INBOX_POLL_S)


IMPORTING = {}  # volume -> {"phase", "file", "done", "total", "hold"} while a mic is copied or checked (under INBOX_LOCK)
_COPIED_SINCE_IDLE = [False]  # recordings copied since the inbox was last held (under INBOX_LOCK)


def _inbox_held():
    """The inbox sorts nothing while a mic's recordings are being copied (a
    conversation's files should all be there when it is matched) or checked —
    but not while its _edit twins are, which play no part in matching."""
    with INBOX_LOCK:
        return any(v.get("hold") for v in IMPORTING.values())


def _set_import(key, entry):
    """Update (or with None, end) a mic's import status. When no mic holds the
    inbox any more and recordings came in, every waiting file gets the full
    INBOX_SINGLE_DELAY_S from now to find a partner."""
    with INBOX_LOCK:
        was = any(v.get("hold") for v in IMPORTING.values())
        if entry is None:
            IMPORTING.pop(key, None)
        else:
            IMPORTING[key] = entry
        if was and not any(v.get("hold") for v in IMPORTING.values()) and _COPIED_SINCE_IDLE[0]:
            _COPIED_SINCE_IDLE[0] = False
            now = time.time()
            for st in INBOX_STATE.values():
                if st.get("settled"):
                    st["settled"] = max(st["settled"], now)
CLEARED = {}  # volume -> {"copied", "cleared", "kept"} after it was done, while it stays plugged in


def _mounted_volumes():
    """Where removable drives are mounted: the folders in /Volumes on a Mac, the
    drive roots (D:\\, E:\\, …) on Windows."""
    if sys.platform == "win32":
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return [Path(f"{chr(65 + i)}:\\") for i in range(26) if mask >> i & 1 and chr(65 + i) not in "AB"]
    return list(VOLUMES.iterdir()) if VOLUMES.is_dir() else []


def _volume_name(path):
    """A volume's own name, for messages: its folder in /Volumes, its drive ("D:") on Windows."""
    return path.name or path.drive


def _external_volume_windows(path):
    """_external_volume on Windows: the disk's model name ("Wireless Mic Tx")
    of a USB or SD drive, else None. Only removable drives are asked about."""
    import ctypes
    if ctypes.windll.kernel32.GetDriveTypeW(str(path)) != 2:  # DRIVE_REMOVABLE
        return None
    ps = ("$d = Get-Partition -DriveLetter %s -ErrorAction Stop | Get-Disk; "
          "@{FriendlyName=$d.FriendlyName; BusType=[string]$d.BusType} | ConvertTo-Json") % path.drive[0]
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        info = json.loads(out)
    except Exception:
        return None
    if info.get("BusType") not in ("USB", "SD", "MMC"):
        return None
    return " ".join((info.get("FriendlyName") or "").split()) or _volume_name(path)


def _external_volume(path):
    """The media name ("Mic Tx") of a local external/removable volume, else None
    (the startup disk, network shares, disk images)."""
    if sys.platform == "win32":
        return _external_volume_windows(path)
    import plistlib
    try:
        out = subprocess.run(["diskutil", "info", "-plist", str(path)], capture_output=True, timeout=20).stdout
        info = plistlib.loads(out)
    except Exception:
        return None
    if info.get("Internal", True) and not info.get("RemovableMedia") and not info.get("Removable"):
        return None
    if info.get("BusProtocol") in (None, "Disk Image", "Virtual Interface"):
        return None
    return info.get("MediaName") or info.get("VolumeName") or path.name


def _volume_recorder_files(root):
    """Every file named like a recorder take on a volume: its top level and one
    folder down (DJI keeps them in TX_MIC…/)."""
    found = []
    try:
        dirs = [root] + [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")
                         and d.name not in ("System Volume Information", "$RECYCLE.BIN")]  # Windows' own
        for d in dirs:
            found += [f for f in d.iterdir() if f.is_file() and not f.name.startswith(".")
                      and RECORDER_FILE_RE.match(f.name)]
    except OSError:
        return []
    return found


def _take(f):
    return re.sub(r"_(orig|edit)$", "", f.stem, flags=re.I)


def _recorder_takes(root, files=None):
    """One file per take on a volume — the raw _orig when there is one — oldest first."""
    by_take = {}
    for f in (files if files is not None else _volume_recorder_files(root)):
        if _take(f) not in by_take or f.stem.endswith(INBOX_TWIN_USE):
            by_take[_take(f)] = f
    return sorted(by_take.values(), key=lambda f: (mt.parse_name(f.name)[2] or 0, f.name))


def _seen_recorder_names():
    """Every recorder file name Reel already has, or had: inbox, library, archive."""
    names = library_file_names()
    names.update(p.name for p in inbox_files())
    for d in (INBOX_SKIPPED, ARCHIVE / "inbox"):
        if d.is_dir():
            names.update(p.name for p in d.iterdir())
    if ARCHIVE.is_dir():
        for f in ARCHIVE.glob("*/item.json"):
            try:
                it = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            names.add(it.get("title"))
            for t in it.get("tracks") or []:
                names.update(t.get("parts") or [t.get("file")])
    return names


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _reel_copies(name):
    """Where Reel may hold a file of this name byte for byte: the inbox (and
    its skipped/), a recording's tracks/ or — a recording of that one file —
    its audio, in the library or the archive."""
    out = [INBOX / name, INBOX_SKIPPED / name, ARCHIVE / "inbox" / name]
    with LOCK:
        items = [dict(i) for i in ITEMS.values()]
    dirs = [(item_dir(i), i) for i in items]
    if ARCHIVE.is_dir():
        for f in ARCHIVE.glob("*/item.json"):
            try:
                dirs.append((f.parent, json.loads(f.read_text())))
            except (OSError, ValueError):
                continue
    for d, it in dirs:
        if any(name in (t.get("parts") or [t.get("file")]) for t in it.get("tracks") or []):
            out.append(d / "tracks" / name)
        if not it.get("tracks") and it.get("title") == name:
            out.append(d / f"audio{it.get('ext') or Path(name).suffix}")
    return [p for p in out if p.is_file()]


def _full_sync(path):
    """Make a file's data — or, for a folder, the names in it — durable on disk:
    on macOS plain fsync stops at the drive's cache, F_FULLFSYNC goes through.
    Windows can't open a folder this way, nor needs to: NTFS journals the names
    in it, and fsync (FlushFileBuffers) already goes through the drive's cache."""
    if sys.platform == "win32" and os.path.isdir(path):
        return
    fd = os.open(path, os.O_RDWR if sys.platform == "win32" else os.O_RDONLY)  # Windows flushes only writable handles
    try:
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        except (AttributeError, OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def _verified(f, log):
    """Whether Reel holds a byte-identical copy of recorder file `f` (on
    another disk than `f` itself)."""
    try:
        st = f.stat()
        entry = log.get(f.name) or {}
        trusted = (entry.get("sha256") and entry.get("size") == st.st_size
                   and entry.get("mtime") == st.st_mtime)
        want = None
        for c in _reel_copies(f.name):
            try:
                cst = c.stat()
                if cst.st_dev == st.st_dev or cst.st_size != st.st_size:
                    continue
                if want is None:
                    # read off this very file when it was copied (the mic never rewrites one)
                    want = entry["sha256"] if trusted else _sha256(f)
                if _sha256(c) == want:
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


IMPORT_LOG_LOCK = threading.Lock()  # several mics may be copying at once


def _load_import_log():
    try:
        return json.loads(IMPORT_LOG.read_text())
    except (OSError, ValueError):
        return {}


def _update_import_log(entries):
    """Merge {file name: fields} into IMPORT_LOG — read, merge and write under a
    lock, so mics copying side by side never drop each other's entries."""
    with IMPORT_LOG_LOCK:
        log = _load_import_log()
        for name, fields in entries.items():
            log.setdefault(name, {}).update(fields)
        tmp = IMPORT_LOG.with_suffix(".tmp")
        tmp.write_text(json.dumps(log, indent=1))
        tmp.replace(IMPORT_LOG)


def _copy_in(src, dst_dir, log, media):
    """Copy one file off a recorder into dst_dir, verified: the SHA-256 of what
    was read off the mic must match the copy re-read from disk, which is flushed
    through to the drive. Logs it in IMPORT_LOG. Returns False on any failure
    (nothing half-copied is left behind)."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    tmp, dst = dst_dir / f".importing-{src.name}", dst_dir / src.name
    try:
        st = src.stat()
        h = hashlib.sha256()
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            for block in iter(lambda: fi.read(8 << 20), b""):
                h.update(block)
                fo.write(block)
            fo.flush()
        _full_sync(tmp)
        if tmp.stat().st_size != st.st_size or _sha256(tmp) != h.hexdigest():
            raise OSError("the copy doesn't match what was read off the mic")
        shutil.copystat(src, tmp)  # the recorder's own timestamps
        os.replace(tmp, dst)
    except OSError as err:
        tmp.unlink(missing_ok=True)
        print(f"  import: {src.name} from {media}: {err} — nothing deleted; trying again while it's plugged in")
        return False
    log[src.name] = {"size": st.st_size, "mtime": st.st_mtime, "sha256": h.hexdigest(),
                     "at": time.time(), "from": media}
    _update_import_log({src.name: log[src.name]})
    return True


def import_volume(root, media):
    """Copy the takes on a plugged-in recorder that Reel hasn't seen into the
    inbox, then — with clear_mic on in ⚙ SETTINGS — clear the mic of every
    file Reel verifiably holds. Returns True when the volume is done (False:
    try again in a moment)."""
    clear = load_settings()["clear_mic"]
    log = _load_import_log()
    files = _volume_recorder_files(root)
    takes = _recorder_takes(root, files)
    twins = [f for f in files if f not in takes]  # _edit files whose _orig is on the mic too
    new = [f for f in takes + twins if f.name not in log]
    if new:
        seen = _seen_recorder_names()
        new = [f for f in new if f.name not in seen and safe_name(f.name) not in seen]
    new_takes = [f for f in takes if f in new]
    if not new and not clear:
        return True
    key = f"{media} ({_volume_name(root)})"

    def phase(name, k, total, file="", hold=True):
        _set_import(key, {"phase": name, "file": file, "done": k, "total": total, "hold": hold})

    try:
        if new:
            print(f"  import: {media} plugged in — copying {len(new_takes)} new recording{'s' if len(new_takes) != 1 else ''}"
                  f"{f' (+{len(new) - len(new_takes)} _edit twins, to inbox/skipped/)' if len(new) > len(new_takes) else ''}, "
                  f"{sum(f.stat().st_size for f in new) / 1e9:.1f} GB")
        # recordings first (the inbox waits for them), then their _edit twins (it doesn't)
        for k, src in enumerate(new_takes + [f for f in new if f not in new_takes]):
            is_take = src in new_takes
            phase("copying", k, len(new), src.name, hold=is_take)
            if not _copy_in(src, INBOX if is_take else INBOX_SKIPPED, log, media):
                return False
            if is_take:
                with INBOX_LOCK:
                    _COPIED_SINCE_IDLE[0] = True
        if not clear:
            with INBOX_LOCK:
                CLEARED[key] = {"copied": len(new_takes), "cleared": None, "kept": 0}
            print(f"  import: {media} done — {len(new_takes)} copied, left on it; you can unplug it")
            return True
        with INBOX_LOCK:
            copying = [n for n, st in INBOX_STATE.items() if st["status"] == "copying" and n not in log]
        by_hand = [n for n in copying if n not in _load_import_log()]  # (not another mic's copier)
        if by_hand:
            return False  # a copy by hand into the inbox is still running: clear the mic after it
        if new:  # the copies' names, too, are on disk before anything goes
            for d in (INBOX, INBOX_SKIPPED):
                if d.is_dir():
                    _full_sync(d)
        keep, doomed = [], []
        for k, f in enumerate(takes + twins):
            phase("checking", k, len(files), f.name)
            (doomed if _verified(f, log) else keep).append(f)
        clear = [f for f in takes if f in doomed]
        phase("clearing", 0, len(doomed))
        cleared = 0
        for f in doomed:
            try:
                f.unlink()
                cleared += 1
            except OSError as err:
                print(f"  import: couldn't delete {f.name} from {media}: {err}")
        if hasattr(os, "sync"):  # not on Windows, where removable drives aren't write-cached by default
            os.sync()
        now = time.time()
        _update_import_log({f.name: {"cleared": now} for f in doomed if f.name in log})
        kept_takes = [f for f in takes if f in keep]
        with INBOX_LOCK:
            CLEARED[key] = {"copied": len(new_takes), "cleared": len(clear), "kept": len(kept_takes)}
        msg = f"  import: {media} done — {len(new_takes)} copied, {len(clear)} recording{'s' if len(clear) != 1 else ''} " \
              f"({cleared} files) cleared from it after checking Reel has identical copies"
        if keep:
            msg += f"; {len(keep)} file{'s' if len(keep) != 1 else ''} kept on it (no identical copy in Reel: " \
                   f"{', '.join(f.name for f in keep[:3])}{'…' if len(keep) > 3 else ''})"
        print(msg + " — you can unplug it")
        return True
    finally:
        _set_import(key, None)


def volume_watcher():
    """Watch /Volumes for recorders being plugged in (see AUTO_IMPORT). Each one
    is copied in a thread of its own, so several mics copy side by side: a DJI
    transmitter reads at ~13 MB/s, its own limit, far below the Mac's."""
    done = {}      # (mount path, device) -> media name, or None for volumes that aren't recorders
    running = {}   # (mount path, device) -> True while its copier runs
    retry_at = {}  # (mount path, device) -> when to look again after "not yet"

    def copier(key, v, media):
        try:
            ok = import_volume(v, media)
        except Exception as err:
            print(f"  import: {media}: {err}")
            ok = False
        if ok:
            done[key] = media
        else:
            retry_at[key] = time.time() + VOLUME_POLL_S
        running.pop(key, None)

    while True:
        try:
            mounted = {}
            for v in _mounted_volumes():
                if v.is_symlink() or v.name.startswith("."):
                    continue
                try:
                    mounted[(str(v), v.stat().st_dev)] = v
                except OSError:
                    continue
            for key in list(done):
                if key not in mounted:
                    media = done.pop(key)  # unplugged: next time it's plugged in, look again
                    with INBOX_LOCK:
                        CLEARED.pop(f"{media} ({_volume_name(Path(key[0]))})", None)
            for key in list(retry_at):
                if key not in mounted:
                    retry_at.pop(key, None)
            for key, v in mounted.items():
                if key in done or key in running or retry_at.get(key, 0) > time.time():
                    continue
                media = _external_volume(v)
                if media is None or not _recorder_takes(v):
                    done[key] = None
                    continue
                running[key] = True
                threading.Thread(target=copier, args=(key, v, media), daemon=True).start()
        except Exception as err:
            print(f"  import: {err}")
        time.sleep(VOLUME_POLL_S)


# --------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # transcription progress is logged explicitly; HTTP chatter is noise

    def send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send(code, json.dumps(obj))

    def fail(self, code, message):
        self.send_json({"error": message}, code)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def refused(self, changes):
        """Turn away what a web page elsewhere asks of this server: a Host that
        isn't this server (a site whose name was pointed at 127.0.0.1, to read
        the transcripts) or, for a request that changes something, an Origin
        that isn't this page (a site posting a key or settings of its own).
        Reel's own page, and the command line, pass. True when refused."""
        port = self.server.server_address[1]
        ours = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        if (self.headers.get("Host") or "").lower() in ours and \
                not (changes and origin is not None and origin.lower() not in {"http://" + h for h in ours}):
            return False
        self.close_connection = True  # (its body is left unread)
        self.fail(403, "Reel only answers its own page")
        return True

    # ---- routes

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if self.refused(changes=False):
            return
        try:
            if path in ("/", "/index.html"):
                page = STATIC / "index.html"
                if not page.is_file():
                    return self.fail(500, "static/index.html is missing")
                return self.send(200, page.read_bytes(), "text/html; charset=utf-8")

            if path == "/api/clean-prompt":
                return self.send_json(clean_prompt_state())

            if path == "/api/keyterms":
                return self.send_json(keyterms_state())

            if path == "/api/settings":
                return self.send_json(settings_state())

            if path == "/api/inbox":
                return self.send_json(inbox_snapshot())

            if path == "/api/library":
                with LOCK:
                    items = sorted(ITEMS.values(), key=lambda i: i.get("recorded") or i["added"], reverse=True)
                    out = [dict(i) for i in items]
                    folder_names = groups()
                return self.send_json({"items": out, "groups": folder_names,
                                       "keys": {k: v["set"] for k, v in keys_state().items()},
                                       "code": {"running": CODE_VERSION, "on_disk": code_version(), "stale": code_changed_on_disk()}})

            m = re.fullmatch(r"/api/item/([a-z0-9-]+)", path)
            if m:
                with LOCK:
                    item = ITEMS.get(m.group(1))
                    item = dict(item) if item else None
                if item is None:
                    return self.fail(404, "No such recording")
                transcript = None
                tfile = item_dir(item) / "transcript.json"
                if item["status"] == "ready" and tfile.is_file():
                    transcript = json.loads(tfile.read_text())
                clean = None
                cfile = item_dir(item) / "clean.json"
                if item.get("clean_status") == "ready" and cfile.is_file():
                    clean = json.loads(cfile.read_text())
                return self.send_json({"item": item, "transcript": transcript,
                                       "clean": clean})

            m = re.fullmatch(r"/audio/([a-z0-9-]+)", path)
            if m:
                with LOCK:
                    item = ITEMS.get(m.group(1))
                    item = dict(item) if item else None
                if item is None:
                    return self.fail(404, "No such recording")
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if query.get("src") == ["denoised"]:
                    f = item_dir(item) / "denoised.m4a"
                    if not f.is_file():
                        return self.fail(404, "No denoised copy")
                    return self.serve_file(f, "audio/mp4")
                track = query.get("track")
                if track:  # a single recorder's raw file (first part, if it saved several)
                    try:
                        t = track_paths(item)[int(track[0])][0]
                    except (ValueError, IndexError):
                        return self.fail(404, "No such track")
                    return self.serve_file(t, MIME.get(t.suffix.lower(), "application/octet-stream"))
                return self.serve_file(audio_path(item), MIME.get(item.get("ext"), "application/octet-stream"))

            return self.fail(404, "Not found")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as err:
            try:
                self.fail(500, str(err))
            except Exception:
                self.close_connection = True

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if self.refused(changes=True):
            return
        try:
            if parsed.path == "/api/upload":
                return self.handle_upload(parsed)
            if parsed.path == "/api/clean-prompt":
                return self.handle_clean_prompt()
            if parsed.path == "/api/keyterms":
                text = json.loads(self.read_body() or b"{}").get("text")
                if not isinstance(text, str):
                    return self.fail(400, 'Expected {"text": "..."}')
                save_keyterms(text)
                return self.send_json(keyterms_state())
            if parsed.path == "/api/settings":
                body = json.loads(self.read_body() or b"{}")
                changes = {k: body[k] for k in SETTINGS_DEFAULTS if k in body}
                if not changes or any(type(v) is not type(SETTINGS_DEFAULTS[k]) for k, v in changes.items()):
                    return self.fail(400, 'Expected any of {"no_training": true, "clear_mic": false, '
                                          '"backup": true, "backup_dir": "…", "backup_tracks": false}')
                try:
                    change_settings(changes)
                except ValueError as err:
                    return self.fail(409, str(err))
                return self.send_json(settings_state())
            if parsed.path == "/api/keys":
                body = json.loads(self.read_body() or b"{}")
                if body.get("service") not in KEY_VARS or not isinstance(body.get("key"), str):
                    return self.fail(400, 'Expected {"service": "elevenlabs" | "anthropic", "key": "…"}')
                try:
                    check = save_key(body["service"], body["key"])
                except ValueError as err:
                    return self.fail(409, str(err))
                if check:
                    key_added(body["service"])
                return self.send_json({**settings_state(), "check": check})
            if parsed.path == "/api/backup":
                if not load_settings()["backup"]:
                    return self.fail(409, "Turn the backup on first")
                backup_soon(now=True)
                return self.send_json(settings_state(), 202)
            if parsed.path == "/api/inbox/scan":
                return self.handle_inbox(force=True)
            if parsed.path == "/api/inbox/single":
                return self.handle_inbox(only=json.loads(self.read_body() or b"{}").get("name"))
            if parsed.path == "/api/inbox/group":
                return self.handle_inbox(group=json.loads(self.read_body() or b"{}").get("names"))
            m = re.fullmatch(r"/api/item/([a-z0-9-]+)/place", parsed.path)
            if m:
                body = json.loads(self.read_body() or b"{}")
                try:
                    item = set_place(m.group(1), body.get("group"))
                except ValueError as err:
                    return self.fail(409, str(err))
                return self.send_json({"item": item, "groups": groups()})
            m = re.fullmatch(r"/api/item/([a-z0-9-]+)/speakers", parsed.path)
            if m:
                return self.handle_speakers(m.group(1))
            if parsed.path == "/api/combine":
                ids = json.loads(self.read_body() or b"{}").get("ids")
                if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                    return self.fail(400, 'Expected {"ids": ["…", "…"]}')
                try:
                    return self.send_json({"item": combine_items(ids)}, 201)
                except ValueError as err:
                    return self.fail(409, str(err))
            m = re.fullmatch(r"/api/item/([a-z0-9-]+)/uncombine", parsed.path)
            if m:
                try:
                    return self.send_json({"items": uncombine_item(m.group(1))})
                except ValueError as err:
                    return self.fail(409, str(err))
            m = re.fullmatch(r"/api/item/([a-z0-9-]+)/(cut|split)", parsed.path)
            if m:
                return self.handle_edit(m.group(1), m.group(2))
            m = re.fullmatch(r"/api/item/([a-z0-9-]+)/flags", parsed.path)
            if m:
                return self.handle_flags(m.group(1))
            m = re.fullmatch(r"/api/item/([a-z0-9-]+)/clean", parsed.path)
            if m:
                return self.handle_clean(m.group(1))
            m = re.fullmatch(r"/api/item/([a-z0-9-]+)/name", parsed.path)
            if m:
                return self.handle_name(m.group(1))
            m = re.fullmatch(r"/api/item/([a-z0-9-]+)/retry", parsed.path)
            if m:
                return self.handle_retry(m.group(1))
            m = re.fullmatch(r"/api/item/([a-z0-9-]+)/held", parsed.path)
            if m:
                return self.handle_held(m.group(1))
            m = re.fullmatch(r"/api/item/([a-z0-9-]+)/retranscribe", parsed.path)
            if m:
                return self.handle_retry(m.group(1), force=True)
            return self.fail(404, "Not found")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as err:
            try:
                self.fail(500, str(err))
            except Exception:
                self.close_connection = True

    def handle_upload(self, parsed):
        """?name=<file name>[&mtime=<epoch seconds>]: the file as the body. The
        mtime — the original file's, which the browser and the command line
        know — dates the recording when its name doesn't."""
        query = urllib.parse.parse_qs(parsed.query)
        name = query.get("name", [""])[0]
        try:
            mtime = float(query["mtime"][0])
        except (KeyError, ValueError):
            mtime = None
        ext = Path(name).suffix.lower()
        if ext not in AUDIO_EXTS:
            return self.fail(400, f"Unsupported file type: {ext or '(none)'}")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self.fail(411, "Empty upload")
        tmp = LIBRARY / f".incoming-{os.urandom(4).hex()}{ext}"
        try:
            with open(tmp, "wb") as out:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        raise IOError("Upload ended early")
                    out.write(chunk)
                    remaining -= len(chunk)
            if mtime and 0 < mtime < time.time() + 86400:
                os.utime(tmp, (mtime, mtime))
            item = add_file(tmp, title=Path(name).name, move=True)
        finally:
            tmp.unlink(missing_ok=True)
        return self.send_json({"item": item}, 201)

    def handle_inbox(self, **kw):
        """Process the inbox now: everything (force), one file alone, or a chosen group."""
        if mt is None:
            return self.fail(501, f"Multi-mic support needs numpy ({MULTITRACK_ERROR})")
        inbox_process(**kw)
        return self.send_json(inbox_snapshot(), 202)

    def handle_speakers(self, item_id):
        """Store local display names for a recording's speakers."""
        names = json.loads(self.read_body() or b"{}").get("names")
        if not isinstance(names, dict):
            return self.fail(400, "Expected {\"names\": {\"speaker_0\": \"Alice\"}}")
        with LOCK:
            item = ITEMS.get(item_id)
            if item is None:
                return self.fail(404, "No such recording")
            clean = {str(k): str(v).strip() for k, v in names.items() if str(v).strip()}
            item["speaker_names"] = clean
            snapshot = dict(item)
        save_item(snapshot)
        return self.send_json({"item": snapshot})

    def handle_edit(self, item_id, what):
        """{"start_ms": …, "end_ms": …} to /cut, {"at_ms": …} to /split."""
        body = json.loads(self.read_body() or b"{}")
        keys = ("start_ms", "end_ms") if what == "cut" else ("at_ms",)
        if not all(isinstance(body.get(k), (int, float)) for k in keys):
            return self.fail(400, "Expected {" + ", ".join(f'"{k}": …' for k in keys) + "}")
        try:
            if what == "cut":
                return self.send_json({"item": cut_item(item_id, body["start_ms"], body["end_ms"])})
            return self.send_json({"items": split_item(item_id, body["at_ms"])})
        except ValueError as err:
            return self.fail(409, str(err))

    def handle_flags(self, item_id):
        """Set any of a recording's notes (see NOTE_DEFAULTS), e.g.
        {"no_training": true, "training_ask": "Alice"}. A note that no longer
        applies is cleared (see tidy_notes)."""
        body = json.loads(self.read_body() or b"{}")
        given = {k: body[k] for k in NOTE_DEFAULTS if k in body}
        if not given or any(type(v) is not type(NOTE_DEFAULTS[k]) for k, v in given.items()):
            return self.fail(400, 'Expected any of {"no_training": true, "training_ask": "who"}')
        with LOCK:
            item = ITEMS.get(item_id)
            if item is None:
                return self.fail(404, "No such recording")
            for k, v in given.items():
                item[k] = v if isinstance(v, bool) else " ".join(v.split())[:500]
            tidy_notes(item)
            snapshot = dict(item)
        save_item(snapshot)
        print(f"  [{snapshot.get('ai_title') or snapshot['title']}] "
              f"{'not for training' if snapshot['no_training'] else 'for training'}"
              f"{'; ask first: ' + snapshot['training_ask'] if snapshot['training_ask'] else ''}")
        return self.send_json({"item": snapshot})

    def handle_clean_prompt(self):
        """Save the prompt edited in the app; as_default also makes it the baseline."""
        body = json.loads(self.read_body() or b"{}")
        text = body.get("prompt", "")
        if not isinstance(text, str):
            return self.fail(400, 'Expected {"prompt": "...", "as_default": false}')
        (save_default_prompt if body.get("as_default") else save_clean_prompt)(text)
        return self.send_json(clean_prompt_state())

    def handle_clean(self, item_id):
        """Kick off an AI clean-up pass over a finished transcript."""
        with LOCK:
            item = ITEMS.get(item_id)
            if item is None:
                return self.fail(404, "No such recording")
            if item["status"] != "ready":
                return self.fail(400, "Transcript isn't ready yet")
            if item.get("clean_status") == "working":
                return self.fail(409, "Clean-up is already running")
            item["clean_status"] = "working"
            item["clean_error"] = None
            snapshot = dict(item)
        save_item(snapshot)
        threading.Thread(target=clean_worker, args=(item_id,), daemon=True).start()
        return self.send_json({"item": snapshot}, 202)

    def handle_retry(self, item_id, force=False):
        """Re-run the transcription from the saved audio: a failed one, or with
        `force` a finished one too (e.g. after enrolling a speaker in the
        library). A multi-mic recording keeps its mix; only Scribe runs again."""
        with LOCK:
            item = ITEMS.get(item_id)
            if item is None:
                return self.fail(404, "No such recording")
            if item["status"] != "error" and not (force and item["status"] == "ready"):
                return self.fail(409, "Only a failed transcription can be retried")
            if item.get("tracks"):
                missing = [p.name for parts in track_paths(item) for p in parts if not p.is_file()]
                if missing:
                    return self.fail(410, f"Recorder file{'s' if len(missing) > 1 else ''} missing from its "
                                          f"tracks/ folder: {', '.join(missing)}")
            elif not audio_path(item).is_file():
                return self.fail(410, "Audio file is missing — delete this recording and add it again")
            if force:  # only once we know it can run: a refused retry leaves the clean-up alone
                item["clean_status"] = None
                item["clean_error"] = None
                (item_dir(item) / "clean.json").unlink(missing_ok=True)
            item["transcript_id"] = None
            set_status(item, "queued")  # RLock: re-entrant; also saves item.json
            snapshot = dict(item)
        threading.Thread(target=transcribe_worker, args=(item_id,), daemon=True).start()
        return self.send_json({"item": snapshot}, 202)

    def handle_held(self, item_id):
        """{"action": "retry" | "drop"} for the mics a failed rebuild set aside."""
        body = json.loads(self.read_body() or b"{}")
        try:
            snapshot = held_mics(item_id, body.get("action"))
        except ValueError as err:
            return self.fail(409, str(err))
        except KeyError:
            return self.fail(404, "No such recording")
        return self.send_json({"item": snapshot}, 202)

    def handle_name(self, item_id):
        """{"title": "…"} sets the title by hand (kept from then on, through
        rebuilds too); no title, or an empty one, has Claude write a new one."""
        body = json.loads(self.read_body() or b"{}")
        title = str(body.get("title") or "").strip()
        with LOCK:
            item = ITEMS.get(item_id)
            if item is None:
                return self.fail(404, "No such recording")
            if title:
                item["ai_title"] = " ".join(title.split())[:200]
                item["title_manual"] = True
                snapshot = dict(item)
        if title:
            save_item(snapshot)
            print(f"  [{item['title']}] titled by hand: {snapshot['ai_title']}")
            return self.send_json({"item": snapshot})
        if item["status"] != "ready":
            return self.fail(400, "Transcript isn't ready yet")
        if generate_title(item_id) is None:
            return self.fail(502, "Claude didn't come back with a title. Is there an Anthropic key in "
                                  "⚙ SETTINGS? Try again in a moment.")
        with LOCK:
            item = ITEMS.get(item_id)
            snapshot = dict(item) if item else None
        if snapshot is None:
            return self.fail(404, "No such recording")
        return self.send_json({"item": snapshot})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if self.refused(changes=True):
            return
        m = re.fullmatch(r"/api/inbox/(.+)", path)
        if m:
            name = urllib.parse.unquote(m.group(1))
            target = INBOX / name
            if "/" in name or not target.is_file():
                return self.fail(404, "No such inbox file")
            try:
                dst = archive_path(name, "inbox")
                shutil.move(str(target), str(dst))
            except OSError as err:
                return self.fail(500, f"Couldn't archive {name}: {err}")
            with INBOX_LOCK:
                INBOX_STATE.pop(name, None)
            print(f"  archived inbox file {name} -> {dst.parent.name}/{dst.name}")
            return self.send_json({"ok": True, "archived": dst.name})
        m = re.fullmatch(r"/api/item/([a-z0-9-]+)", path)
        if not m:
            return self.fail(404, "Not found")
        item_id = m.group(1)
        with LOCK:
            item = ITEMS.pop(item_id, None)
            ITEM_MTIME.pop(item_id, None)
        if item is None:
            return self.fail(404, "No such recording")
        try:
            dst = archive_item(item)   # the item, not item_id: see item_dir()
        except OSError as err:
            with LOCK:                 # nothing moved — keep it in the library
                ITEMS[item_id] = item
            return self.fail(500, f"Couldn't archive {item_id}: {err}")
        print(f"  archived “{item.get('ai_title') or item.get('title')}”"
              + (f" -> {ARCHIVE_DIR}/{dst.name}" if dst else " (its folder was already gone)"))
        return self.send_json({"ok": True, "archived": dst.name if dst else None})

    # ---- audio with Range support (required for seeking in Safari)

    def serve_file(self, path, ctype):
        if not path.is_file():
            return self.fail(404, "Audio file is missing")
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)\s*$", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:  # suffix range: last N bytes
                    start = max(0, size - int(m.group(2)))
                if start >= size or start > end:
                    return self.send(416, b"", ctype,
                                     {"Content-Range": f"bytes */{size}"})
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1 << 18, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port another one is
    # listening on (and TIME_WAIT never blocks a bind there anyway): leave it off.
    allow_reuse_address = sys.platform != "win32"


def find_port(start):
    for port in range(start, start + 50):
        with socket.socket() as s:
            if sys.platform == "win32":  # fail on a port anyone holds (see Server)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                # Match HTTPServer's allow_reuse_address so a just-closed server's
                # TIME_WAIT socket doesn't push us onto a different port.
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("No free port found")


def _within(a, b):
    """Whether folder a is folder b or inside it — compared as the folders
    themselves, since macOS usually ignores case (~/reel is ~/Reel)."""
    for p in (a, *a.parents):
        try:
            if os.path.samefile(p, b):
                return True
        except OSError:
            continue
    return False


def check_data_folder():
    """~/Reel Recordings (or REEL_DATA) holds everything. The default is made
    on first start; a REEL_DATA that isn't there is refused rather than made —
    it may be a disk that isn't plugged in, and an empty library in its place
    would quietly take the new recordings. Neither may be, or be inside, the
    code's folder (or hold it): the code's folders would pass for recording
    folders, and git would see the recordings."""
    if DATA.exists() and not DATA.is_dir():
        raise SystemExit(f"Data folder {DATA} is not a directory")
    if not DATA.exists():
        if DATA.is_symlink():
            raise SystemExit(f"{DATA} is a link to {os.readlink(DATA)}, which isn't there — a disk not plugged in?")
        if os.environ.get("REEL_DATA"):
            raise SystemExit(f"No data folder at {DATA} (REEL_DATA) — create it, or plug in the disk it's on")
        DATA.mkdir(parents=True)
        print(f"  made {DATA} for your recordings (REEL_DATA in .env keeps them elsewhere)")
    if _within(DATA, ROOT) or _within(ROOT, DATA):
        raise SystemExit(f"The data folder {DATA} and Reel's code folder {ROOT} overlap. Keep them apart: "
                         f"move the code elsewhere, or set REEL_DATA in {ENV_FILE} to another folder.")


def _hand_over(url, files):
    """Give audio files named on the command line to the Reel already running
    at `url`. Returns whether every one of them went."""
    ok = True
    for src in files:
        if not src.is_file() or src.suffix.lower() not in AUDIO_EXTS:
            print(f"Skipping (not found, or not an audio file): {src}")
            ok = False
            continue
        try:
            with open(src, "rb") as f:
                query = urllib.parse.urlencode({"name": src.name, "mtime": src.stat().st_mtime})
                req = urllib.request.Request(f"{url}/api/upload?{query}", data=f,
                                             method="POST", headers={"Content-Length": str(src.stat().st_size)})
                with urllib.request.urlopen(req, timeout=3600) as resp:
                    print(f"Queued in the running Reel: {json.loads(resp.read())['item']['title']}")
        except (OSError, ValueError, KeyError) as err:
            print(f"Couldn't hand {src.name} to the running Reel: {err}")
            ok = False
    return ok


def claim_library():
    """One Reel at a time on a data folder: two would sort the same inbox and
    work on the same recordings side by side. The lock is held by the open
    file, so it goes when Reel does, however it stops. Returns the lock file
    (main writes the page's address into it). A second Reel hands any audio
    files it was given to the first, opens its page and exits."""
    lock = open(DATA / ".reel.lock", "a+")
    try:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            lock.seek(2**30)  # lock a byte past the end, so the address stays readable
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except PermissionError:
                raise BlockingIOError
    except BlockingIOError:  # held: another Reel runs here
        lock.seek(0)
        url = lock.read().strip()
        args = sys.argv[1:]
        files = [Path(a).expanduser() for a in args if not a.startswith("-")]
        if url.startswith("http") and files and all(a == "--no-browser" or not a.startswith("-") for a in args):
            ok = _hand_over(url, files)
            if "--no-browser" not in args:
                webbrowser.open(url)
            raise SystemExit(0 if ok else 1)
        if files or any(a.startswith("-") and a != "--no-browser" for a in args):
            print("Stop the running Reel first (Ctrl-C in its Terminal) to run that command.")
        if url.startswith("http") and "--no-browser" not in args:
            webbrowser.open(url)
        raise SystemExit(f"Reel is already running on {DATA}" + (f" at {url}" if url else ""))
    except OSError as err:  # a folder that can't be locked (some network drives): go ahead without
        print(f"  note: couldn't lock {DATA} ({err.strerror}) — don't start a second Reel on it")
    return lock


# --------------------------------------------------------------------- backup
# ⚙ SETTINGS can turn on a backup to Google Drive (backup.py): a round every
# BACKUP_EVERY_S while Reel runs, one at start-up, and one shortly after a
# recording is ready.

BACKUP_EVERY_S = 15 * 60
BACKUP_SETTLE_S = 90  # after a recording is ready: its title and clean-up land meanwhile
BACKUP = {"running": False, "error": None, "copied": None, "bytes": None, "finished": None}
_BACKUP_SOON = threading.Event()
_BACKUP_NOW = threading.Event()


def backup_soon(now=False):
    """Have the backup run in BACKUP_SETTLE_S — `now`: right away — if it is on."""
    (_BACKUP_NOW if now else _BACKUP_SOON).set()


def backup_round():
    settings = load_settings()
    if not settings["backup"] or not settings["backup_dir"]:
        return
    with LOCK:  # finished recordings only: nothing is half-written in them
        todo = [(it.get("group") or DEFAULT_GROUP, item_dir(it)) for it in ITEMS.values()
                if it["status"] in ("ready", "error") and it.get("clean_status") != "working"]
    BACKUP.update(running=True, error=None)
    try:
        copied, size = backup.run(DATA, settings["backup_dir"], todo, tracks=settings["backup_tracks"])
        BACKUP.update(copied=copied, bytes=size, finished=time.time())
        if copied:
            print(f"  backup: {copied} file{'s' if copied != 1 else ''} ({size / 1e6:.1f} MB) copied to "
                  f"{Path(settings['backup_dir']) / backup.BACKUP_DIR}")
    except (backup.BackupError, OSError) as err:
        BACKUP["error"] = str(err)
        print(f"  backup: {err}")
    finally:
        BACKUP["running"] = False


def backup_worker():
    soon_at, last = None, time.time()
    while True:
        _BACKUP_NOW.wait(5)
        if _BACKUP_SOON.is_set():
            _BACKUP_SOON.clear()
            soon_at = soon_at or time.time() + BACKUP_SETTLE_S
        if not (_BACKUP_NOW.is_set() or (soon_at and time.time() >= soon_at)
                or time.time() - last >= BACKUP_EVERY_S):
            continue
        _BACKUP_NOW.clear()
        soon_at, last = None, time.time()
        try:
            backup_round()
        except Exception as err:
            BACKUP.update(running=False, error=str(err))
            print(f"  backup: {err}")


def settings_state():
    """What ⚙ SETTINGS shows: the settings, the keys, and the backup — how it
    went, and the Google Drive folders it could go to."""
    return {**load_settings(), "keys": keys_state(), "data": str(DATA),
            "drive_folders": backup.drive_folders(), "drive_download": backup.DRIVE_DOWNLOAD,
            "backup_status": {**BACKUP, "last": backup.last_run(DATA)}}


def change_settings(changes):
    """Save changes made in ⚙ SETTINGS. Turning the backup on without a
    folder picks the first Google Drive found; a round starts right away.
    Raises ValueError."""
    if "backup_dir" in changes:
        d = changes["backup_dir"].strip()
        if d:
            p = Path(d).expanduser()
            if not p.is_dir():
                raise ValueError(f"There's no folder at {d}")
            d = str(p)
        changes["backup_dir"] = d
    new = {**load_settings(), **changes}
    if new["backup"] and not new["backup_dir"]:
        found = backup.drive_folders()
        if not found:
            raise ValueError("Google Drive for desktop isn't on this Mac, or isn't signed in. Install it "
                             f"({backup.DRIVE_DOWNLOAD}) and sign in, or pick another folder to back up to.")
        changes["backup_dir"] = found[0]
    saved = save_settings(changes)
    if saved["backup"] and {"backup", "backup_dir", "backup_tracks"} & set(changes):
        backup_soon(now=True)
    return saved


def main():
    check_data_folder()
    lock = claim_library()
    load_library()

    # --regroup A B [--regroup C D …]: each group becomes one conversation
    regroups, regroup_ids = [], set()
    for k, arg in enumerate(sys.argv):
        if arg == "--regroup":
            ids = []
            for a in sys.argv[k + 1:]:
                if a.startswith("-"):
                    break
                ids.append(a)
            if not ids:
                raise SystemExit("--regroup needs the ids of the recordings to rebuild (their folder names)")
            regroups.append(ids)
            regroup_ids.update(ids)
    for ids in regroups:
        regroup_items(ids)
    # --split ID 25:40: two recordings, divided at that point
    split_args = []
    if "--split" in sys.argv:
        split_args = sys.argv[sys.argv.index("--split") + 1:][:2]
        try:
            at = 0.0
            for part in split_args[1].split(":"):
                at = at * 60 + float(part)
        except (IndexError, ValueError):
            raise SystemExit("--split needs a recording's id and a time into it, e.g. --split <id> 25:40")
        try:
            split_item(split_args[0], at * 1000)
        except ValueError as err:
            raise SystemExit(f"--split: {err}")
    # --remix ID [ID …]: redo alignment + mix (+ labels) of finished recordings, no transcription
    remix_ids = []
    if "--remix" in sys.argv:
        for a in sys.argv[sys.argv.index("--remix") + 1:]:
            if a.startswith("-"):
                break
            remix_ids.append(a)
        if not remix_ids:
            raise SystemExit("--remix needs the ids of the recordings to rebuild (their folder names)")
        if mt is None:
            raise SystemExit(f"Multi-mic support needs numpy ({MULTITRACK_ERROR})")
        for iid in remix_ids:
            if iid not in ITEMS:
                raise SystemExit(f"No such recording: {iid}")
        # in the background once the server is up, so the UI shows the progress
        threading.Thread(target=remix_items, args=(remix_ids,), daemon=True).start()
    files = [a for a in sys.argv[1:]
             if not a.startswith("-") and a not in regroup_ids and a not in remix_ids and a not in split_args]
    good = []
    for arg in files:
        src = Path(arg).expanduser()
        if not src.is_file():
            print(f"Skipping (not found): {arg}")
        elif src.suffix.lower() not in AUDIO_EXTS:
            print(f"Skipping (unsupported type): {arg}")
        else:
            good.append(src)
    if "--group" in sys.argv:
        if len(good) < 2:
            raise SystemExit("--group needs at least two recorder files")
        if mt is None:
            raise SystemExit(f"Multi-mic support needs numpy ({MULTITRACK_ERROR})")
        # add_group moves files, so work on copies (same names) and leave the originals alone
        staging = LIBRARY / f".incoming-{os.urandom(4).hex()}"
        staging.mkdir()
        copies = []
        for src in good:
            shutil.copy2(src, staging / src.name)
            copies.append(staging / src.name)
        item = add_group(copies)
        shutil.rmtree(staging, ignore_errors=True)
        print(f"Queued conversation: {item['title']}")
    else:
        for src in good:
            item = add_file(src)
            print(f"Queued: {item['title']}")

    threading.Thread(target=name_unnamed, daemon=True).start()
    threading.Thread(target=denoise_missing, daemon=True).start()
    threading.Thread(target=library_watcher, daemon=True).start()
    threading.Thread(target=backup_worker, daemon=True).start()
    backup_soon()
    INBOX.mkdir(exist_ok=True)
    if mt is None:
        print(f"⚠  Multi-mic recordings, cutting and splitting are off: {MULTITRACK_ERROR}. "
              f"Run bin/setup.sh to install numpy, then start Reel again (it's needed for {sys.executable}).")
    else:
        threading.Thread(target=relabel_outdated, daemon=True).start()
        if "--ingest" in sys.argv:
            _inbox_refresh_states()
            with INBOX_LOCK:
                for s in INBOX_STATE.values():
                    s["settled"] = s["settled"] or time.time()
                    s["status"] = "waiting"
            inbox_process(force=True)
        threading.Thread(target=inbox_watcher, daemon=True).start()
        if AUTO_IMPORT and sys.platform in ("darwin", "win32"):
            threading.Thread(target=volume_watcher, daemon=True).start()

    if not api_key():
        print("\n⚠  No ElevenLabs API key yet: add it in ⚙ SETTINGS on the page that opens (see README.md).\n")

    port = find_port(DEFAULT_PORT)
    server = Server(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    lock.seek(0)
    lock.truncate()
    lock.write(url)  # for a second Reel started on this folder: it opens this one instead
    lock.flush()
    print(f"Reel is running at {url}   (Ctrl-C to stop)" + (f"   code {CODE_VERSION}" if CODE_VERSION else ""))
    print(f"Your recordings are in {DATA}."
          + (f" Drop recorder files into {INBOX} — they're matched and transcribed automatically."
             if mt is not None else ""))
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
