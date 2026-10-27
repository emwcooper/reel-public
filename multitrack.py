"""Multi-mic support for Reel: several recorders, one conversation.

Each DJI-style recorder sits on one participant, so its track carries that
person loud and everyone else quiet. Given N such tracks this module

  * fingerprints tracks and groups those that belong to the same conversation
    (cross-correlation of loudness envelopes),
  * aligns them — start offset plus clock drift, since separate recorders run
    on separate crystals (tens of ppm → hundreds of ms over an hour),
  * mixes them into one "best mic wins" track with Dugan-style gain sharing
    (each mic's gain follows its share of the total level, so the near mic
    carries a speaker while the far mics fall 20–40 dB), and
  * decides who is speaking at any moment from the per-track level gap, then
    labels transcript words with the mic (= person) that carried them.

Only numpy is needed. Non-WAV inputs are decoded with macOS afconvert (or
ffmpeg when present), and the mix is encoded to AAC the same way.
"""

import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

import numpy as np

# Analysis frame: 10 ms hop → envelope at 100 Hz; every stage below works on
# these frames, then gains are interpolated back to sample rate for mixing.
FRAME_MS = 10
ENV_RATE = 1000 // FRAME_MS
MIX_RATE = 48000  # everything is resampled to this before analysis / mixing

# Grouping: normalized envelope-correlation peak above this ⇒ same conversation.
MATCH_MIN_SCORE = 12.0
# Two tracks must overlap at least this long to be considered a pair.
MATCH_MIN_OVERLAP_S = 60.0
# Recorder files whose names carry timestamps (DJI) that say the two overlap:
# the correlation peak only has to reach this within ±MATCH_TIMED_WINDOW_S of
# the offset the timestamps predict. Lav mics on two people pick up little of
# each other, so real pairs often score 4–10 where synthetic ones scored 20+.
MATCH_MIN_SCORE_TIMED = 3.0
MATCH_TIMED_WINDOW_S = 5.0
# DJI transmitters take their clock from the phone app, so their timestamps
# agree to seconds: a match by sound that puts two DJI files more than this far
# from where their timestamps do is a false one (other recorders' clocks may be
# anything, so they're trusted to the sound alone).
MATCH_DJI_CLOCK_S = 300.0
# When the clocks say two long recordings overlap but the whole-length peak is
# too flat (hours of conversation with one noisy stretch — a food court —
# flatten it), they are compared MATCH_WINDOW_S at a time: a pair whose
# windows mostly agree on the lag (to half a second) is one conversation.
MATCH_WINDOW_S = 1200.0
DJI_NAME_RE = re.compile(r"^TX\d*_MIC\d+_", re.I)
# Consecutive files of one recorder chain into one track when the next starts
# within this long after the previous ended (DJI splits every 30 min with no
# gap; a stop and restart leaves one — it is filled with silence).
CHAIN_MAX_GAP_S = 600.0
# After a restart, a stretch of at least this long is aligned on its own to
# find the true gap (see refine_restart_gaps), if that alignment reaches this
# quality (whole tracks align at 0.5–0.9).
RESTART_ALIGN_MIN_S = 120.0
# The reference track must be at least this share of the longest one's length.
REF_MIN_SHARE = 0.8
RESTART_ALIGN_MIN_Q = 0.3

# Mixing: gain_i = level_i^Q / Σ level_j^Q. Q=2 is the classic Dugan power
# share (10 dB gap → far mic at −21 dB); 3 rejects the far mic harder (−30 dB).
MIX_SHARPNESS = 3.0
MIX_ATTACK_MS = 15
MIX_RELEASE_MS = 250
# Below this level above its own noise floor a mic is treated as "not speaking".
SPEECH_FLOOR_DB = 6.0

# Speaker decisions: a word goes to a mic when that mic leads the runner-up by
# at least this much (dB) over the word's frames; smaller gaps are settled with
# Scribe's diarization when it agrees with a known mic, else by context.
CONFIDENT_GAP_DB = 3.0
# Short words are judged on a handful of frames and their timestamps are only
# approximate, so the lead they need grows as they get shorter: a word with
# fewer voiced frames than this needs CONFIDENT_GAP_DB * (this / frames).
CONFIDENT_MIN_FRAMES = 15
# Within a word, only frames within this many dB of its loudest frame count —
# a word before a pause often has an end time deep in the silence, where the
# levels say nothing about who spoke.
WORD_VOICED_RANGE_DB = 12.0
# Final labels come from a Viterbi pass that weighs, per word, the mic levels
# against Scribe's voice-based diarization and a cost for changing speaker:
# SCRIBE_VOTE_DB is what a fully consistent Scribe label is worth in dB of
# level lead (scaled down for labels that agreed less with the mics);
# SWITCH_COST_DB is the price of a speaker change with no pause at all,
# falling linearly to zero at a pause of SWITCH_FREE_MS; LEVEL_CAP_DB bounds
# how much one word's level lead can weigh.
SCRIBE_VOTE_DB = 6.0
SWITCH_COST_DB = 6.0
SWITCH_FREE_MS = 800
LEVEL_CAP_DB = 20.0
# A mic's noise floor moves (a hand on the transmitter, fabric, a fan): each
# mic's levels are judged against its floor over the surrounding
# FLOOR_WINDOW_S, and any excess of that local floor over the mic's global
# floor is taken off — a mic that is merely hot is not carrying a voice.
FLOOR_WINDOW_S = 30
FLOOR_EXCESS_MAX_DB = 30.0  # more than this is not a hot mic but a broken floor estimate
# A mic that wasn't recording during a word can still be the speaker's. With
# a single live mic, every absent mic scores the mirror of that mic's level
# less ABSENT_MARGIN_DB — so it wins only when the live mic clearly carries
# someone else's voice, and Scribe says whose. With two or more live mics the
# levels settle it among them, and an absent mic is only in the running when
# Scribe's label for the word maps to it.
ABSENT_MARGIN_DB = 6.0
# Someone without a mic (a guest, a video playing) reaches every mic from a
# distance, so no mic leads: the gap between the two loudest mics is what
# gives them away (a wearer's own mic leads the next by ~10 dB on a typical
# word; an unmic'd voice by ~1 dB). A Scribe label becomes a speaker of its
# own, "without a mic", when at least OFFMIC_MIN_SHARE of its decided words —
# those with a gap under OFFMIC_LOOK_DB over OFFMIC_MIN_FRAMES+ voiced frames,
# against those confidently on one mic — and OFFMIC_MIN_WORDS of them, look
# like that (unmic'd voices measured 0.55–0.73, wearers mostly under 0.16,
# none above 0.46). Its words then keep Scribe's label, and in the per-word
# weighing that speaker scores as a mic sitting OFFMIC_GAP_DB above the
# runner-up: it beats the loudest mic when that leads by less. Its Scribe
# vote reaches full weight at a share of OFFMIC_FULL_SHARE.
OFFMIC_LOOK_DB = 2.0
OFFMIC_MIN_FRAMES = 10
OFFMIC_MIN_WORDS = 20
OFFMIC_MIN_SHARE = 0.5
OFFMIC_FULL_SHARE = 0.7
OFFMIC_GAP_DB = 3.0
# A mic's level for its wearer and for everyone else drifts during a recording
# — clipped on, then on the table, then under a jumper: on 10/4 one mic's
# readings dropped ~10 dB for six minutes and the other wearer "spoke" through
# its owner's sentences. So each mic's dividing line between its wearer's
# speech and everyone else's is measured locally, within ±LOCAL_LEVEL_S of each
# word, as the midpoint of its median level on words Scribe gives its wearer
# and on words Scribe gives anyone else (labels mapped to mics; words with
# LOCAL_MIN_FRAMES+ voiced frames; LOCAL_MIN_WORDS+ of each), and taken off.
# A mic that stops telling them apart then cancels out and the others decide.
LOCAL_LEVEL_S = 120.0
LOCAL_MIN_FRAMES = 8
LOCAL_MIN_WORDS = 8
# Bump when the labelling changes so existing recordings are relabelled at
# start-up (cheap: no transcription involved).
LABEL_VERSION = 5

# Mix output level. The mix is scaled so that its "speech peak" — the
# NORM_PERCENTILE-th percentile of 1 ms block peaks — lands at LIMIT_CEILING_DB;
# whatever still pokes above the ceiling (a knock on a mic can hit +15 dBFS in
# a 32-bit float recording) is caught by a lookahead peak limiter instead of
# dragging the whole recording down to make room for it.
LIMIT_CEILING_DB = -1.0
NORM_PERCENTILE = 99.9
LIMIT_BLOCK_MS = 1
LIMIT_ATTACK_MS = 5     # lookahead = length of the gain ramp into a peak
LIMIT_RELEASE_MS = 80   # time constant of the recovery after a peak

AUDIO_EXTS = {
    ".wav", ".mp3", ".m4a", ".m4b", ".mp4", ".mov", ".flac", ".aac", ".aiff",
    ".aif", ".caf", ".ogg", ".oga", ".opus", ".webm", ".mkv", ".amr", ".3gp", ".wma",
}


def log(msg):
    print(f"  {msg}", flush=True)


# ------------------------------------------------------------------ decoding

class WavData:
    """Random access to a PCM/float WAV file through a memory map."""

    def __init__(self, path):
        self.path = Path(path)
        with open(self.path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
                raise ValueError(f"{self.path.name}: not a WAV file")
            rf64_data_size = None
            fmt = None
            data_off = data_len = None
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
                start = f.tell()
                if cid == b"ds64":
                    body = f.read(size)
                    rf64_data_size = struct.unpack_from("<Q", body, 8)[0]
                elif cid == b"fmt ":
                    body = f.read(size)
                    tag, ch, rate, _, block, bits = struct.unpack_from("<HHIIHH", body, 0)
                    if tag == 0xFFFE and size >= 40:  # WAVE_FORMAT_EXTENSIBLE
                        tag = struct.unpack_from("<H", body, 24)[0]
                    fmt = (tag, ch, rate, block, bits)
                elif cid == b"data":
                    data_off = start
                    data_len = size if size != 0xFFFFFFFF else rf64_data_size
                    if data_len is None:
                        data_len = self.path.stat().st_size - start
                    break
                f.seek(start + size + (size & 1))
        if fmt is None or data_off is None:
            raise ValueError(f"{self.path.name}: missing fmt/data chunk")
        tag, ch, rate, block, bits = fmt
        data_len = min(data_len, self.path.stat().st_size - data_off)
        self.channels, self.rate, self.bits = ch, rate, bits
        self.tag = tag
        bps = block // ch if ch else bits // 8
        if tag == 1 and bits == 16:
            self.dtype, self.scale = "<i2", 1 / 32768
        elif tag == 1 and bits == 24:
            self.dtype, self.scale = None, 1 / 8388608  # 3-byte samples, handled below
        elif tag == 1 and bits == 32:
            self.dtype, self.scale = "<i4", 1 / 2147483648
        elif tag == 3 and bits == 32:
            self.dtype, self.scale = "<f4", 1.0
        elif tag == 3 and bits == 64:
            self.dtype, self.scale = "<f8", 1.0
        elif tag == 1 and bits == 8:
            self.dtype, self.scale = "u1", 1 / 128
        else:
            raise ValueError(f"{self.path.name}: unsupported WAV format tag={tag} bits={bits}")
        self.frames = data_len // block
        self.block = block
        raw = np.memmap(self.path, dtype=np.uint8, mode="r", offset=data_off,
                        shape=(self.frames * block,))
        self.raw = raw

    @property
    def duration(self):
        return self.frames / self.rate

    def read(self, start, count):
        """Mono float32 samples [start, start+count), zero-padded outside the file."""
        start, count = int(start), int(count)
        out = np.zeros(count, dtype=np.float32)
        s = max(0, start)
        e = min(self.frames, start + count)
        if e <= s:
            return out
        chunk = self.raw[s * self.block:e * self.block]
        if self.dtype is None:  # 24-bit
            b = np.asarray(chunk).reshape(-1, 3).astype(np.int32)
            v = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
            v = np.where(v >= 1 << 23, v - (1 << 24), v).astype(np.float32) * self.scale
            v = v.reshape(-1, self.channels)
        elif self.dtype == "u1":
            v = (np.frombuffer(chunk, dtype=np.uint8).astype(np.float32) - 128) * self.scale
            v = v.reshape(-1, self.channels)
        else:
            v = np.frombuffer(chunk, dtype=self.dtype).reshape(-1, self.channels)
            v = v.astype(np.float32) * self.scale
        mono = v.mean(axis=1) if self.channels > 1 else v[:, 0]
        out[s - start:e - start] = mono
        return out


def _tool(name):
    """A helper program: on PATH, or dropped into the project's bin/ folder."""
    local = Path(__file__).resolve().parent / "bin" / name
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    local = local.with_name(name + ".exe")  # Windows
    if os.name == "nt" and local.is_file():
        return str(local)
    return shutil.which(name)


def decode_to_wav(src, dst, rate=MIX_RATE):
    """Decode any audio file to a mono float32 WAV at `rate` (afconvert, else ffmpeg)."""
    src, dst = Path(src), Path(dst)
    if _tool("afconvert"):
        cmd = ["afconvert", "-f", "WAVE", "-d", f"LEF32@{rate}", "-c", "1",
               str(src), str(dst)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and dst.exists():
            return dst
        err = proc.stderr.strip()
    else:
        err = "afconvert not found"
    if _tool("ffmpeg"):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1",
               "-ar", str(rate), "-c:a", "pcm_f32le", str(dst)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and dst.exists():
            return dst
        err = proc.stderr.strip() or err
    if not _tool("ffmpeg"):
        err += (" — afconvert (CoreAudio) cannot read WebM/Matroska at all, so ffmpeg is the "
                "only way to decode this one (brew install ffmpeg)"
                if src.suffix.lower() in (".webm", ".mkv") else
                " — and ffmpeg, which reads more formats, is not installed (brew install ffmpeg)")
    raise RuntimeError(f"Could not decode {src.name}: {err}")


class ConcatWav:
    """Several consecutive files of one recorder (DJI saves a new file every
    30 min) read as a single track, with silence filling any gap between
    them. `gaps[i]` is the silence (in frames) before part i+1. Parts must
    share the sample rate."""

    def __init__(self, parts, gaps=None):
        self.parts = parts
        self.rate = parts[0].rate
        if any(p.rate != self.rate for p in parts):
            raise ValueError("Parts of one recording have different sample rates")
        self.channels = 1
        gaps = list(gaps or [0] * (len(parts) - 1))
        self.gaps = [int(g) for g in gaps]
        self.gap_exact = [True] * len(self.gaps)  # False: from rounded file-name seconds
        starts = [0]
        for p, g in zip(parts, self.gaps + [0]):
            starts.append(starts[-1] + p.frames + g)
        self.offsets = np.array(starts)
        self.frames = int(self.offsets[-1])
        self.path = parts[0].path
        self.names = None  # set by open_track: the parts' file names

    @property
    def duration(self):
        return self.frames / self.rate

    def read(self, start, count):
        start, count = int(start), int(count)
        out = np.zeros(count, dtype=np.float32)
        for i, p in enumerate(self.parts):
            p0 = int(self.offsets[i])
            p1 = p0 + p.frames
            s, e = max(start, p0), min(start + count, p1)
            if e > s:
                out[s - start:e - start] = p.read(s - p0, e - s)
        return out


def wav_time_reference(path):
    """(time reference in samples, sample rate, frames) from a WAV's bext
    chunk, or None. DJI counts samples from the start of a recording session,
    so a file that continues the previous one (the 30-minute split) starts
    exactly where that one ended, and a restart starts again at 0."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if len(head) < 12 or head[:4] != b"RIFF":
                return None
            rate = frames = ref = None
            while True:
                h = f.read(8)
                if len(h) < 8:
                    break
                cid, size = h[:4], struct.unpack("<I", h[4:])[0]
                if cid == b"fmt ":
                    body = f.read(size)
                    _, ch, rate, _, block, _ = struct.unpack_from("<HHIIHH", body, 0)
                    frames_block = block
                elif cid == b"bext" and size >= 354:
                    body = f.read(size)
                    lo, hi = struct.unpack_from("<II", body, 338)
                    ref = lo | (hi << 32)
                elif cid == b"data":
                    frames = size // frames_block if rate else None
                    break
                else:
                    f.seek(size + (size & 1), 1)
            if ref is None or rate is None or frames is None:
                return None
            return ref, rate, frames
    except (OSError, struct.error, UnboundLocalError):
        return None


def wav_layout(path):
    """(chunks, fmt) of a plain RIFF WAV: chunks as (id, offset of its header,
    body size) in file order, fmt as (format tag, channels, rate, block align,
    bits). Raises ValueError for anything else (RF64 included)."""
    path = Path(path)
    size = path.stat().st_size
    chunks, fmt = [], None
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError(f"{path.name}: not a plain RIFF WAV")
        pos = 12
        while pos + 8 <= size:
            f.seek(pos)
            cid, n = struct.unpack("<4sI", f.read(8))
            if cid == b"data":
                n = min(n, size - pos - 8)  # a recorder that stopped mid-write
            elif cid == b"fmt ":
                tag, ch, rate, _, block, bits = struct.unpack("<HHIIHH", f.read(16))
                fmt = (tag, ch, rate, block, bits)
            chunks.append((cid, pos, n))
            pos += 8 + n + (n & 1)
    if fmt is None or not any(c[0] == b"data" for c in chunks):
        raise ValueError(f"{path.name}: missing fmt/data chunk")
    return chunks, fmt


def wav_frames(path):
    """(frames, sample rate) of a plain RIFF WAV, from its header."""
    chunks, (_, _, rate, block, _) = wav_layout(path)
    return next(n for cid, _, n in chunks if cid == b"data") // block, rate


def write_wav_slice(src, dst, f0, f1):
    """Copy frames [f0, f1) of WAV `src` into a new WAV `dst`, keeping its
    format and every other chunk. A bext time reference moves on by f0, so the
    piece still says where it starts (see wav_time_reference)."""
    chunks, (_, _, _, block, _) = wav_layout(src)
    total = next(n for cid, _, n in chunks if cid == b"data") // block
    f0 = min(max(0, int(f0)), total)
    f1 = min(max(f0, int(f1)), total)
    n = (f1 - f0) * block
    with open(src, "rb") as f, open(dst, "wb") as g:
        g.write(b"RIFF\0\0\0\0WAVE")
        for cid, pos, size in chunks:
            if cid == b"data":
                g.write(b"data" + struct.pack("<I", n))
                f.seek(pos + 8 + f0 * block)
                left = n
                while left:
                    buf = f.read(min(left, 1 << 23))
                    if not buf:
                        raise ValueError(f"{Path(src).name}: shorter than its header says")
                    g.write(buf)
                    left -= len(buf)
                if n & 1:
                    g.write(b"\0")
                continue
            f.seek(pos + 8)
            body = bytearray(f.read(size))
            if cid == b"bext" and size >= 354:
                struct.pack_into("<Q", body, 338, struct.unpack_from("<Q", body, 338)[0] + f0)
            elif cid == b"fact" and size >= 4:
                struct.pack_into("<I", body, 0, f1 - f0)
            g.write(cid + struct.pack("<I", size) + bytes(body) + (b"\0" if size & 1 else b""))
        riff = g.tell() - 8
        g.seek(4)
        g.write(struct.pack("<I", riff))


def silence_wav(path, ranges):
    """Overwrite frames [a, b) of WAV `path` with silence, in place, for each
    (a, b) in `ranges`: the file keeps its length, so its timing is unchanged."""
    chunks, (_, _, _, block, bits) = wav_layout(path)
    _, pos, size = next(c for c in chunks if c[0] == b"data")
    total = size // block
    fill = b"\x80" if bits == 8 else b"\0"  # 8-bit PCM is unsigned
    with open(path, "r+b") as f:
        for a, b in ranges:
            a, b = min(max(0, int(a)), total), min(max(0, int(b)), total)
            f.seek(pos + 8 + a * block)
            left = max(0, b - a) * block
            while left:
                k = min(left, 1 << 23)
                f.write(fill * k)
                left -= k


def slice_levels(src, dst, f0, f1, rows=None):
    """speakers.npz for part of a recording: frames [f0, f1) of the mics in
    `rows` (all when None)."""
    with np.load(src) as z:
        data = {k: z[k] for k in z.files}
    rows = slice(None) if rows is None else list(rows)
    np.savez_compressed(dst, **{k: v[rows, f0:f1] if v.ndim == 2 else v for k, v in data.items()})


def part_gaps(names, durations, with_exact=False):
    """Seconds of silence between consecutive parts of one recorder.

    Sample-accurate from the WAVs' bext time references when the second part
    continues the first (DJI's 30-minute split: gap exactly 0); after a
    restart (reference back at 0) only the names' whole-second timestamps
    remain, so the gap is known to about ±1 s — those are refined against
    the other mics during alignment. 0 where nothing is known. With
    `with_exact`, returns (gaps, exact flags)."""
    gaps, exact = [], []
    for a, b, da in zip(names, names[1:], durations):
        gap = None
        ra, rb = wav_time_reference(a), wav_time_reference(b)
        if ra and rb and ra[1] == rb[1]:
            cont = (rb[0] - (ra[0] + ra[2])) / ra[1]
            if -0.05 <= cont < 5.0:
                gap = max(0.0, cont)
        if gap is None:
            ta, tb = parse_name(Path(a).name)[2], parse_name(Path(b).name)[2]
            gap = max(0.0, tb - (ta + da)) if ta is not None and tb is not None else 0.0
            exact.append(ta is None or tb is None)  # nothing known ⇒ nothing to refine
        else:
            exact.append(True)
        gaps.append(gap)
    return (gaps, exact) if with_exact else gaps


def open_track(path, workdir):
    """A WavData for `path` (or a ConcatWav for a list of consecutive parts,
    gaps between them filled with silence), decoding/resampling into
    `workdir` when needed."""
    if isinstance(path, (list, tuple)):
        parts = [open_track(p, workdir) for p in path]
        if len(parts) == 1:
            return parts[0]
        gaps, exact = part_gaps(list(path), [p.duration for p in parts], with_exact=True)
        w = ConcatWav(parts, [round(g * parts[0].rate) for g in gaps])
        w.gap_exact = exact
        w.names = [str(p) for p in path]
        return w
    path = Path(path)
    if path.suffix.lower() == ".wav":
        try:
            w = WavData(path)
            if w.rate == MIX_RATE:
                return w
        except ValueError:
            pass
    dst = Path(workdir) / (re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem) + f".{MIX_RATE}.wav")
    if not dst.exists():
        decode_to_wav(path, dst)
    return WavData(dst)


def encode_mix(wav_path, out_path, bitrate=96000):
    """Encode the mix WAV to AAC .m4a (afconvert/ffmpeg). Returns the output path or None."""
    wav_path, out_path = Path(wav_path), Path(out_path)
    if _tool("afconvert"):
        cmd = ["afconvert", "-f", "m4af", "-d", "aac", "-b", str(bitrate),
               "-q", "127", "-s", "3", str(wav_path), str(out_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and out_path.exists():
            return out_path
        log(f"afconvert AAC encode failed: {proc.stderr.strip()[:200]}")
    if _tool("ffmpeg"):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
               "-c:a", "aac", "-b:a", str(bitrate), str(out_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and out_path.exists():
            return out_path
    return None


# Optional noise reduction of the *playback* copy with DeepFilterNet's
# `deep-filter` CLI (drop the binary and DeepFilterNet3_onnx.tar.gz into bin/).
# Transcription always gets the untouched mix: denoisers make audio nicer for
# people and worse for recognizers. The attenuation limit keeps it natural.
DENOISE_TOOL = "deep-filter"
DENOISE_MODEL = "DeepFilterNet3_onnx.tar.gz"
DENOISE_ATTEN_LIM_DB = 25


def denoise_available():
    return _tool(DENOISE_TOOL) is not None


def denoise_wav(src, dst, log_fn=log):
    """Run DeepFilterNet on a 48 kHz WAV; writes `dst` (16-bit WAV, same
    timeline: the model's delay is compensated). Returns dst or None."""
    tool = _tool(DENOISE_TOOL)
    if tool is None:
        return None
    src, dst = Path(src), Path(dst)
    model = Path(tool).resolve().parent / DENOISE_MODEL
    with tempfile.TemporaryDirectory(dir=dst.parent) as tmp:
        cmd = [tool, "-D", "--atten-lim-db", str(DENOISE_ATTEN_LIM_DB), "-o", tmp]
        if model.is_file():
            cmd += ["-m", str(model)]
        cmd.append(str(src))
        proc = subprocess.run(cmd, capture_output=True, text=True)
        out = Path(tmp) / src.name
        if proc.returncode != 0 or not out.is_file():
            log_fn(f"deep-filter failed: {(proc.stderr or proc.stdout).strip()[:200]}")
            return None
        shutil.move(str(out), dst)
    return dst


def write_wav16(path, samples, rate=MIX_RATE):
    x = np.clip(samples, -1.0, 1.0)
    data = np.round(x * 32767).astype("<i2").tobytes()
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
        f.write(b"data" + struct.pack("<I", len(data)) + data)


# ---------------------------------------------------------------- envelopes

def envelope(w, chunk_s=600):
    """RMS level per 10 ms frame, in dB, for the whole track (streamed in chunks).
    A chain of parts is built from its parts' envelopes with the gaps at the
    recorder's floor (see chain_envelope)."""
    if isinstance(w, ConcatWav):
        return chain_envelope(w)
    hop = w.rate * FRAME_MS // 1000
    n_frames = int(math.ceil(w.frames / hop))
    out = np.empty(n_frames, dtype=np.float32)
    frames_per_chunk = int(chunk_s * ENV_RATE)
    for f0 in range(0, n_frames, frames_per_chunk):
        f1 = min(n_frames, f0 + frames_per_chunk)
        x = w.read(f0 * hop, (f1 - f0) * hop)
        x = x.reshape(f1 - f0, hop)
        rms = np.sqrt(np.mean(x.astype(np.float64) ** 2, axis=1))
        out[f0:f1] = 20 * np.log10(rms + 1e-7)
    return out


def noise_floor_db(env):
    """A track's own noise floor: a low percentile of its frame levels. Frames
    at the -120 dB fill (a gap between a recorder's parts, a mic not yet
    recording) don't count: an 8-minute restart gap in an hour is 14% of the
    frames, enough to make the fill the "floor" and every real frame look loud."""
    real = env[env > -119.0]
    return float(np.percentile(real if len(real) else env, 10))


def _norm_env(env):
    """Envelope prepared for correlation: dB above noise floor, clipped, mean-removed."""
    x = np.clip(env - noise_floor_db(env), 0, 40).astype(np.float64)
    x -= x.mean()
    return x


def xcorr_offset(a, b, max_lag=None, expect=None, window=None):
    """Lag (in frames) of b relative to a maximizing correlation, plus a peak score.

    Positive lag: b starts *later* in real time than a, i.e. b[i] ≈ a[i + lag].
    Score = peak / robust std of the correlation function (z-score). With
    `expect` and `window` the peak is searched only within `window` frames of
    the expected lag, while the z-score stays relative to all lags."""
    n = len(a) + len(b) - 1
    size = 1 << (n - 1).bit_length()
    fa = np.fft.rfft(a, size)
    fb = np.fft.rfft(b, size)
    corr = np.fft.irfft(fa * np.conj(fb), size)
    # corr[k] = Σ a[i+k] b[i] for k ≥ 0; negative k wrap around the end.
    lags = np.concatenate([np.arange(0, len(a)), np.arange(-(len(b) - 1), 0)])
    corr = np.concatenate([corr[:len(a)], corr[size - (len(b) - 1):]])
    if max_lag is not None:
        keep = np.abs(lags) <= max_lag
        lags, corr = lags[keep], corr[keep]
    # Normalize by overlap length so short overlaps aren't penalized too much
    overlap = np.minimum(len(a) - lags, len(b) + lags).clip(1, None)
    normed = corr / np.sqrt(overlap)
    med = np.median(normed)
    # (an almost flat correlation — most lags exactly alike — must not turn a
    # tiny median deviation into an astronomical score)
    mad = max(np.median(np.abs(normed - med)) * 1.4826, 0.1 * float(np.std(normed)), 1e-12)
    if expect is not None and window is not None:
        near = np.abs(lags - expect) <= window
        if not near.any():
            return int(expect), 0.0
        k = int(np.flatnonzero(near)[np.argmax(normed[near])])
    else:
        k = int(np.argmax(normed))
    score = float((normed[k] - med) / mad)
    return int(lags[k]), score


def fingerprint(w):
    """Compact per-track descriptor used for grouping: the dB envelope."""
    return envelope(w)


def pair_match(env_a, env_b, expect_s=None):
    """Compare two envelopes; returns (lag_frames, score, overlap_seconds).
    With `expect_s` (the lag the recorders' timestamps predict) the peak is
    looked for near it."""
    a, b = _norm_env(env_a), _norm_env(env_b)
    if expect_s is None:
        lag, score = xcorr_offset(a, b)
    else:
        lag, score = xcorr_offset(a, b, expect=int(round(expect_s * ENV_RATE)),
                                  window=int(MATCH_TIMED_WINDOW_S * ENV_RATE))
    overlap = min(len(a) - lag, len(b) + lag) / ENV_RATE
    return lag, score, overlap


# ---------------------------------------------------------------- alignment

def _decimate(x, factor):
    n = len(x) // factor * factor
    return x[:n].reshape(-1, factor).mean(axis=1)


def _refine_window(ref, trk, ref_pos, trk_pos, win_s=6.0, search_ms=60, factor=6):
    """Sample-accurate lag between windows of two tracks around predicted positions.

    Returns (lag_seconds, quality) where lag is how much later `trk` content
    sits than `ref` content, beyond the predicted trk_pos-ref_pos. Works on
    8 kHz-decimated audio: speech energy lives there and it's fast."""
    rate = ref.rate // factor
    n = int(win_s * ref.rate)
    pad = int(search_ms / 1000 * ref.rate)
    a = _decimate(ref.read(ref_pos, n), factor)
    b = _decimate(trk.read(trk_pos - pad, n + 2 * pad), factor)
    a = a - a.mean()
    b = b - b.mean()
    if np.max(np.abs(a)) < 1e-4 or np.max(np.abs(b)) < 1e-4:
        return None
    size = 1 << (len(a) + len(b) - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size)
    valid = corr[:len(b) - len(a) + 1]  # valid[k]: a placed at position k inside b
    k = int(np.argmax(valid))
    seg = b[k:k + len(a)]
    denom = math.sqrt(np.dot(a, a) * np.dot(seg, seg)) + 1e-12
    quality = valid[k] / denom  # normalized correlation, 0..1
    # Sub-sample peak position by parabolic interpolation
    kf = k
    if 0 < k < len(valid) - 1:
        y0, y1, y2 = valid[k - 1], valid[k], valid[k + 1]
        d = y0 - 2 * y1 + y2
        if d < 0:
            kf = k + 0.5 * (y0 - y2) / d
    # a's predicted position inside b is pad/factor (decimated samples)
    lag_samples = kf * factor - pad
    return lag_samples / ref.rate, quality


def align_pair(ref, trk, env_ref, env_trk, coarse_lag_frames=None):
    """Full alignment of `trk` onto `ref`'s timeline.

    Returns dict(offset, drift, quality, points) where a sample at time t on
    the reference clock is found at time `offset + t*(1+drift)` in `trk`."""
    a, b = _norm_env(env_ref), _norm_env(env_trk)
    if coarse_lag_frames is None:
        coarse_lag_frames, _ = xcorr_offset(a, b)
    # trk starts coarse_lag frames after ref: trk time = ref time - lag
    coarse_offset = -coarse_lag_frames / ENV_RATE

    # Overlap on the reference clock
    ov_start = max(0.0, -coarse_offset)
    ov_end = min(ref.duration, trk.duration - coarse_offset)
    if ov_end - ov_start < 20:
        return {"offset": coarse_offset, "drift": 0.0, "quality": 0.0, "points": []}

    # Second envelope pass in windows to pin down drift coarsely (10 ms res).
    span = ov_end - ov_start
    n_win = int(min(40, max(6, span / 120)))
    win_s = min(120.0, span / n_win)
    centers = np.linspace(ov_start + win_s / 2, ov_end - win_s / 2, n_win)
    pts = []
    for c in centers:
        f0 = int((c - win_s / 2) * ENV_RATE)
        f1 = int((c + win_s / 2) * ENV_RATE)
        g0 = int((c - win_s / 2 + coarse_offset) * ENV_RATE)
        g1 = int((c + win_s / 2 + coarse_offset) * ENV_RATE)
        pad = ENV_RATE * 2  # ±2 s search around the coarse prediction
        if f0 < 0 or g0 - pad < 0 or f1 > len(a) or g1 + pad > len(b):
            continue
        seg_a = a[f0:f1]
        seg_b = b[g0 - pad:g1 + pad]
        if np.std(seg_a) < 1e-3 or np.std(seg_b) < 1e-3:
            continue
        size = 1 << (len(seg_a) + len(seg_b) - 1).bit_length()
        corr = np.fft.irfft(np.fft.rfft(seg_b, size) * np.conj(np.fft.rfft(seg_a, size)), size)
        valid = corr[:len(seg_b) - len(seg_a) + 1]
        k = int(np.argmax(valid))
        q = valid[k] / (np.linalg.norm(seg_a) * np.linalg.norm(seg_b[k:k + len(seg_a)]) + 1e-9)
        kf = k
        if 0 < k < len(valid) - 1:
            y0, y1, y2 = valid[k - 1], valid[k], valid[k + 1]
            d = y0 - 2 * y1 + y2
            if d < 0:
                kf = k + 0.5 * (y0 - y2) / d
        extra = (kf - pad) / ENV_RATE
        pts.append((c, coarse_offset + extra, float(q)))
    if len(pts) < 3:
        return {"offset": coarse_offset, "drift": 0.0, "quality": 0.0, "points": pts}
    t = np.array([p[0] for p in pts])
    off = np.array([p[1] for p in pts])
    q = np.array([p[2] for p in pts])
    good = q >= max(0.15, np.percentile(q, 30))
    drift, intercept = _robust_line(t[good], off[good])

    # Sample-accurate refinement: short windows at high-energy spots.
    fine = []
    n_fine = int(min(30, max(8, span / 150)))
    fine_win = 6.0
    # In each slice of the overlap, take the window where the reference is loudest
    # (speech on both mics correlates far better than room tone).
    lv = np.clip(env_ref - noise_floor_db(env_ref), 0, None)
    win_f = int(fine_win * ENV_RATE)
    csum = np.cumsum(np.insert(lv.astype(np.float64), 0, 0))
    centers = []
    for s0 in np.linspace(ov_start + 2, ov_end - fine_win - 2, n_fine + 1)[:-1]:
        s1 = s0 + (ov_end - fine_win - 4 - ov_start) / n_fine
        f_lo, f_hi = int(s0 * ENV_RATE), int(max(s0 * ENV_RATE + 1, s1 * ENV_RATE))
        f_hi = min(f_hi, len(lv) - win_f)
        if f_hi <= f_lo:
            continue
        sums = csum[f_lo + win_f:f_hi + win_f] - csum[f_lo:f_hi]
        best = f_lo + int(np.argmax(sums))
        centers.append(best / ENV_RATE)
    for c in centers:
        pred = intercept + drift * c
        r = _refine_window(ref, trk, int(c * ref.rate), int((c + pred) * trk.rate), win_s=fine_win)
        if r is None:
            continue
        lag, quality = r
        fine.append((c, pred + lag, quality))
    if len(fine) >= 3:
        ft = np.array([p[0] for p in fine])
        fo = np.array([p[1] for p in fine])
        fq = np.array([p[2] for p in fine])
        keep = fq >= max(0.05, np.percentile(fq, 25))
        if keep.sum() >= 3:
            d2, i2 = _robust_line(ft[keep], fo[keep])
            # accept only if consistent with the envelope fit (within 30 ms at both ends)
            if (abs((i2 + d2 * ov_start) - (intercept + drift * ov_start)) < 0.03 and
                    abs((i2 + d2 * ov_end) - (intercept + drift * ov_end)) < 0.03):
                drift, intercept = d2, i2
                pts = fine
    quality = float(np.median(q[good])) if good.any() else 0.0
    return {"offset": float(intercept), "drift": float(drift), "quality": quality,
            "points": [(float(x), float(y), float(z)) for x, y, z in pts]}


def _robust_line(t, y, iters=4):
    """Least-squares line y = b + a*t with iterative outlier trimming; returns (a, b)."""
    t = np.asarray(t, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    mask = np.ones(len(t), dtype=bool)
    a, b = 0.0, float(np.median(y))
    for _ in range(iters):
        if mask.sum() < 2:
            break
        A = np.vstack([t[mask], np.ones(mask.sum())]).T
        (a, b), *_ = np.linalg.lstsq(A, y[mask], rcond=None)
        resid = y - (a * t + b)
        scale = np.median(np.abs(resid[mask])) * 1.4826 + 1e-4
        mask = np.abs(resid) < 3.5 * scale
    return float(a), float(b)


# --------------------------------------------------------------------- mixing

class Aligned:
    """A track viewed on the shared (reference) clock: sample t_ref ↔ offset + t*(1+drift)."""

    def __init__(self, wav, offset, drift, env=None):
        self.wav = wav
        self.offset = offset
        self.drift = drift
        self.env = env

    def start(self):
        """First reference time covered by this track."""
        return -self.offset / (1 + self.drift)

    def end(self):
        return (self.wav.duration - self.offset) / (1 + self.drift)

    def read_ref(self, t0, n, rate=MIX_RATE):
        """`n` samples on the reference clock starting at time t0 (seconds), resampled."""
        t = t0 + np.arange(n) / rate
        src = (self.offset + t * (1 + self.drift)) * self.wav.rate
        lo = int(math.floor(src[0])) - 2
        hi = int(math.ceil(src[-1])) + 3
        buf = self.wav.read(lo, hi - lo)
        pos = src - lo
        i = np.floor(pos).astype(np.int64)
        frac = (pos - i).astype(np.float32)
        i = np.clip(i, 1, len(buf) - 3)
        # 4-point cubic (Catmull-Rom) interpolation — clean for ppm-scale warps
        p0, p1, p2, p3 = buf[i - 1], buf[i], buf[i + 1], buf[i + 2]
        a0 = p1
        a1 = 0.5 * (p2 - p0)
        a2 = p0 - 2.5 * p1 + 2 * p2 - 0.5 * p3
        a3 = 0.5 * (p3 - p0) + 1.5 * (p1 - p2)
        return ((a3 * frac + a2) * frac + a1) * frac + a0

    def env_ref(self, n_frames):
        """This track's dB envelope resampled onto the reference frame grid."""
        t = (np.arange(n_frames) + 0.5) / ENV_RATE
        src_f = (self.offset + t * (1 + self.drift)) * ENV_RATE
        idx = np.round(src_f).astype(np.int64)
        out = np.full(n_frames, -120.0, dtype=np.float32)
        ok = (idx >= 0) & (idx < len(self.env))
        out[ok] = self.env[idx[ok]]
        gaps = getattr(self.wav, "gap_frames", None)
        if gaps is not None and len(gaps) == len(self.env):
            ok = ok & ~np.where(ok, gaps[np.clip(idx, 0, len(gaps) - 1)], False)  # a restart gap is no audio
        return out, ok


def _smooth_asym(x, attack, release):
    """One-pole smoother with separate attack/release coefficients (frame domain)."""
    y = np.empty_like(x)
    prev = x[0]
    for i in range(len(x)):
        v = x[i]
        coef = attack if v > prev else release
        prev = prev + coef * (v - prev)
        y[i] = prev
    return y


def _box(x, k):
    if k <= 1:
        return x
    c = np.cumsum(np.insert(x.astype(np.float64), 0, 0))
    out = np.empty_like(x, dtype=np.float64)
    h = k // 2
    n = len(x)
    lo = np.clip(np.arange(n) - h, 0, n)
    hi = np.clip(np.arange(n) + k - h, 0, n)
    out = (c[hi] - c[lo]) / np.maximum(hi - lo, 1)
    return out.astype(x.dtype)


def compute_levels(tracks, n_frames):
    """Per-track levels on the reference grid, normalized so that mics compare fairly.

    Each mic's level L_i (dBFS) gets a constant c_i subtracted so that
    `argmax_i (L_i - c_i)` names the mic nearest the current speaker. With a
    near/far gap G and mic gain a_i, a mic sits at a_i + S when its owner talks
    and a_i + S' - G when someone else does: c_i is the midpoint of those two
    clusters, found by iterating dominance ↔ midpoints over frames where the
    dominant mic clearly carries a voice (pauses and breaths are left out).
    This cancels mic gain without caring how loud each person talks or how
    noisy each spot is.

    Returns (norm[N, F] dB, present[N, F], own_db[N] absolute own-speech level)."""
    n = len(tracks)
    env = np.full((n, n_frames), -120.0, dtype=np.float32)
    present = np.zeros((n, n_frames), dtype=bool)
    floor = np.zeros(n, dtype=np.float32)
    for i, tr in enumerate(tracks):
        env[i], present[i] = tr.env_ref(n_frames)
        floor[i] = noise_floor_db(tr.env)
    neg = np.float32(-1e9)
    multi = present.sum(axis=0) >= 2  # statistics only where mics can be compared
    cols = np.arange(n_frames)
    # Initial guess: own speech = loud frames (95th percentile), midpoint to the floor
    own_db = np.array([np.percentile(env[i][present[i]], 95) if present[i].any() else -30.0
                       for i in range(n)], dtype=np.float32)
    c = (own_db + floor) / 2
    has_owner = np.ones(n, dtype=bool)
    for _ in range(12):
        norm = env - c[:, None]
        dom = np.argmax(np.where(present, norm, neg), axis=0)
        dom_level = env[dom, cols]
        thr = (own_db + floor) / 2  # a mic is "carrying a voice" above this
        talking = present[dom, cols] & (dom_level > thr[dom])
        new_c, new_own = c.copy(), own_db.copy()
        for i in range(n):
            own = talking & multi & (dom == i)
            far = talking & multi & present[i] & (dom != i)
            if own.sum() >= 100:
                new_own[i] = np.median(env[i][own])
                has_owner[i] = True
                if far.sum() >= 100:
                    new_c[i] = (new_own[i] + np.median(env[i][far])) / 2
            else:
                # Nobody (yet) talks into this mic: pin its zero at its loudest
                # level so it only wins frames when its owner really speaks.
                has_owner[i] = False
                new_c[i] = new_own[i]
        done = np.allclose(new_c, c, atol=0.1) and np.allclose(new_own, own_db, atol=0.1)
        c, own_db = new_c, new_own
        if done:
            break
    return env - c[:, None], present, own_db


def mix_gains(norm_levels, present, sharp=MIX_SHARPNESS):
    """Dugan-style gain shares per frame from normalized dB levels."""
    n, F = norm_levels.shape
    lin = 10 ** (np.clip(norm_levels, -60, 20) / 20)  # amplitude, ~1 for own speech
    attack = 1 - math.exp(-FRAME_MS / MIX_ATTACK_MS)
    release = 1 - math.exp(-FRAME_MS / MIX_RELEASE_MS)
    sm = np.empty_like(lin)
    for i in range(n):
        sm[i] = _smooth_asym(lin[i], attack, release)
    sm = np.where(present, sm, 0.0)
    p = sm ** sharp
    # In pauses every mic is clamped to the same floor, so they share evenly
    # (≈1/N each) instead of chasing noise — room tone stays steady. Near
    # speech sits around +G/2 dB and far speech around −G/2, so a floor at
    # −12 dB never touches the mic that is actually carrying a voice.
    floor = (10 ** (-6 / 20)) ** sharp
    p = np.maximum(p, np.where(present, floor, 0.0))
    total = p.sum(axis=0, keepdims=True)
    g = np.where(total > 0, p / np.maximum(total, 1e-12), 0.0)
    # Light extra smoothing (30 ms) so gains never step within a sample block
    for i in range(n):
        g[i] = _box(g[i], 3)
    return g.astype(np.float32)


def limiter_gains(block_peaks, scale, ceiling):
    """Per-block gain (≤ 1) that keeps `scale * signal` under `ceiling`.

    `block_peaks` are the peak |sample| of consecutive LIMIT_BLOCK_MS blocks.
    Lookahead attack: the gain ramps down linearly over LIMIT_ATTACK_MS so it
    has fully arrived when the peak does (the block holding the peak *and both
    its neighbours* end at or below the required gain, so linear interpolation
    between block centres never overshoots). Release: exponential recovery
    with time constant LIMIT_RELEASE_MS, taken as the envelope of the decay
    curves launched from every block, so nothing pops back up too early."""
    r = np.minimum(1.0, ceiling / np.maximum(block_peaks * scale, 1e-9))
    if not (r < 1.0).any():
        return None
    a = max(1, LIMIT_ATTACK_MS // LIMIT_BLOCK_MS)
    # m[b] = min r over blocks b-1 … b+a
    m = r.copy()
    for k in range(-1, a + 1):
        if k == 0:
            continue
        sh = np.ones_like(r)
        if k > 0:
            sh[:-k] = r[k:]
        else:
            sh[-k:] = r[:k]
        np.minimum(m, sh, out=m)
    # g[b] = mean of m over blocks b-a+1 … b (a causal box → linear ramp)
    padded = np.concatenate((np.ones(a - 1), m))
    c = np.cumsum(np.concatenate(([0.0], padded)))
    g = (c[a:] - c[:-a]) / a
    # release: envelope of 1 - (1-g[b]) * rho^(n-b) over all b ≤ n, computed
    # in the log domain as n*log(rho) + cummax(log(1-g[b]) - b*log(rho))
    with np.errstate(divide="ignore"):
        d = np.log1p(-g)  # -inf where g == 1
    log_rho = -LIMIT_BLOCK_MS / LIMIT_RELEASE_MS
    n = np.arange(len(g), dtype=np.float64) * log_rho
    env = n + np.maximum.accumulate(d - n)
    return 1.0 - np.exp(env)


def check_mix_length(seconds, rate=MIX_RATE):
    """A 16-bit mono WAV's size field is 32 bits (about 12 h at 48 kHz): refuse
    a longer mix up front instead of failing after writing gigabytes."""
    if 36 + int(seconds * rate) * 2 > 0xFFFFFFFF:
        raise ValueError(f"The mix would be {seconds / 3600:.1f} hours long, more than a WAV file can hold "
                         f"({0xFFFFFFFF // (2 * rate * 3600)} h). Most likely a recorder's clock is hours off "
                         "and its files were taken for one mic across the gap.")


def render_mix(tracks, gains, own_db, out_wav, n_frames, chunk_s=300, rate=MIX_RATE,
               progress=None, log_fn=log):
    """Sum the aligned tracks with per-frame gains into a 16-bit WAV on disk."""
    hop = rate * FRAME_MS // 1000
    total = n_frames * hop
    check_mix_length(total / rate, rate)
    # Per-track make-up (±8 dB max) so every speaker's own-mic voice lands at
    # about the same loudness — the median mic sets the target.
    own_db = np.asarray(own_db, dtype=np.float64)
    trim = 10 ** (np.clip(np.median(own_db) - own_db, -8, 8) / 20)
    blk = rate * LIMIT_BLOCK_MS // 1000
    block_peaks = []
    # Hidden and *.tmp (it's twice the mix's size), so nothing takes it for a recording's file.
    out_wav = Path(out_wav)
    tmp = out_wav.with_name(f".{out_wav.name}.raw.tmp")
    try:
        with open(tmp, "wb") as raw:
            for s0 in range(0, total, chunk_s * rate):
                n = min(chunk_s * rate, total - s0)
                f0 = s0 // hop
                f1 = (s0 + n + hop - 1) // hop
                acc = np.zeros(n, dtype=np.float32)
                for i, tr in enumerate(tracks):
                    g = gains[i, f0:f1]
                    # frame gains → sample gains (linear interpolation between frame centres)
                    gs = np.interp(np.arange(n) / hop + 0.5 - 0.0, np.arange(len(g)) + 0.5, g)
                    acc += tr.read_ref(s0 / rate, n, rate) * gs.astype(np.float32) * trim[i]
                nb = -(-n // blk)
                pad = np.zeros(nb * blk, dtype=np.float32)
                pad[:n] = np.abs(acc)
                block_peaks.append(pad.reshape(nb, blk).max(axis=1))
                raw.write(acc.astype("<f4").tobytes())
                if progress:
                    progress(min(1.0, (s0 + n) / total))
        block_peaks = np.concatenate(block_peaks) if block_peaks else np.zeros(0)
        peak = float(block_peaks.max()) if len(block_peaks) else 0.0
        # Scale so the speech peak sits at the ceiling (only ever attenuate; a
        # little make-up if very quiet), then limit the few blocks above it.
        ceiling = 10 ** (LIMIT_CEILING_DB / 20)
        scale = 1.0
        limit = None
        if peak > 0:
            speech_peak = float(np.percentile(block_peaks, NORM_PERCENTILE))
            scale = min(ceiling / max(speech_peak, 1e-9), 4.0)
            limit = limiter_gains(block_peaks, scale, ceiling)
            db = lambda v: f"{20 * math.log10(max(v, 1e-9)):+.1f} dBFS"
            msg = f"mix: speech peak {db(speech_peak)}, true peak {db(peak)} → gain {20 * math.log10(scale):+.1f} dB"
            if limit is not None:
                active = int((limit < 10 ** (-0.5 / 20)).sum()) * LIMIT_BLOCK_MS
                msg += f", limiter active {active / 1000:.1f} s (max {20 * math.log10(limit.min()):+.1f} dB)"
            log_fn(msg)
        x = np.memmap(tmp, dtype="<f4", mode="r")
        centres = (np.arange(len(block_peaks)) + 0.5) * blk
        with open(out_wav, "wb") as f:
            data_len = len(x) * 2
            f.write(b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE")
            f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
            f.write(b"data" + struct.pack("<I", data_len))
            step = rate * 600
            for s in range(0, len(x), step):
                seg = np.asarray(x[s:s + step]) * scale
                if limit is not None:
                    seg = seg * np.interp(np.arange(s, s + len(seg)), centres, limit).astype(np.float32)
                seg = np.clip(seg, -1, 1)
                f.write(np.round(seg * 32767).astype("<i2").tobytes())
        del x
    except BaseException:
        out_wav.unlink(missing_ok=True)
        raise
    finally:
        tmp.unlink(missing_ok=True)
    return out_wav


# ------------------------------------------------------------------ pipeline

def chain_envelope(w):
    """Envelope of a ConcatWav from its parts' envelopes. Gap frames are filled
    at the recorder's own noise floor (not digital silence, which would drag
    every percentile-based floor down to nothing) and marked in `w.gap_frames`."""
    hop = w.rate * FRAME_MS // 1000
    parts = [envelope(p) for p in w.parts]
    floor = float(np.percentile(np.concatenate(parts), 10))
    out, flags = [], []
    for k, e in enumerate(parts):
        out.append(e); flags.append(np.zeros(len(e), dtype=bool))
        if k < len(w.gaps):
            g = int(round(w.gaps[k] / hop))
            out.append(np.full(g, floor, dtype=np.float32)); flags.append(np.ones(g, dtype=bool))
    w.gap_frames = np.concatenate(flags)
    return np.concatenate(out)


def refine_restart_gaps(w, env, ref, ref_env, offset, drift, log_fn=log, search_s=8.0):
    """A recorder that was stopped and restarted: the silence inserted for the
    gap came from whole-second timestamps. Every stretch between restarts of
    at least RESTART_ALIGN_MIN_S is aligned against the reference on its own,
    like a track of its own, and the gap before it is set so that it lands
    right after the stretch before (sample-accurate). A stretch too short for
    that is slid against the reference envelope instead (to 10 ms), from where
    the whole-chain fit (`offset`, `drift`) expects it — that fit is itself
    thrown off by the misplaced stretch, which is why it is only the fallback.
    Returns (new ConcatWav, its envelope) — or (None, None) when nothing
    changed."""
    hop = w.rate * FRAME_MS // 1000
    a = _norm_env(ref_env)
    new_gaps = list(w.gaps)
    changed = False
    # Stretches of parts that abut exactly: [first part, end part).
    runs, first = [], 0
    for k, exact in enumerate(w.gap_exact):
        if not exact:
            runs.append((first, k + 1))
            first = k + 1
    runs.append((first, len(w.parts)))

    def run_start(r):  # samples, on the chain's own clock
        return sum(p.frames for p in w.parts[:runs[r][0]]) + sum(new_gaps[:runs[r][0]])

    def run_len(r):
        j0, j1 = runs[r]
        return sum(p.frames for p in w.parts[j0:j1]) + sum(new_gaps[j0:j1 - 1])

    fits = {}
    for r, (j0, j1) in enumerate(runs):
        s0, n = run_start(r), run_len(r)
        if n < RESTART_ALIGN_MIN_S * w.rate:
            continue
        sub = w.parts[j0] if j1 - j0 == 1 else ConcatWav(w.parts[j0:j1], new_gaps[j0:j1 - 1])
        t0 = (s0 / w.rate - offset) / (1 + drift)  # where the whole-chain fit puts its start
        fit = align_pair(ref, sub, ref_env, env[s0 // hop:(s0 + n) // hop],
                         coarse_lag_frames=int(round(t0 * ENV_RATE)))
        if fit["quality"] >= RESTART_ALIGN_MIN_Q:
            fits[r] = fit
    placed = set()
    for r in range(1, len(runs)):
        if r - 1 not in fits or r not in fits:
            continue
        k = runs[r][0] - 1  # the gap before stretch r
        before, this = fits[r - 1], fits[r]
        t_r = -this["offset"] / (1 + this["drift"])  # reference time where stretch r starts
        at = run_start(r - 1) / w.rate + before["offset"] + t_r * (1 + before["drift"])
        gap = at - (run_start(r - 1) + run_len(r - 1)) / w.rate
        if gap < 0 or abs(gap - new_gaps[k] / w.rate) > search_s:
            continue  # implausible: leave it to the envelope slide
        placed.add(k)
        g = int(round(gap * w.rate))
        if g != new_gaps[k]:
            log_fn(f"restart of {Path(w.names[k + 1]).name}: gap {new_gaps[k] / w.rate:.3f}s → {gap:.3f}s "
                   f"(aligned on its own, q={this['quality']:.2f})")
            new_gaps[k] = g
            changed = True
    for k, exact in enumerate(w.gap_exact):
        if exact or k in placed:
            continue
        j = k + 1  # the part after the gap; it runs to the next inexact gap
        f0 = int(sum(p.frames for p in w.parts[:j]) + sum(new_gaps[:j])) // hop
        end = j
        while end < len(w.parts) and (end == j or w.gap_exact[end - 1]):
            end += 1
        f1 = int(sum(p.frames for p in w.parts[:end]) + sum(new_gaps[:end - 1])) // hop
        seg = _norm_env(env[f0:f1]) if f1 - f0 > 3 * ENV_RATE else None
        if seg is None or np.std(seg) < 1e-3:
            continue
        # where the segment's first frame is expected on the reference clock;
        # the search window is clipped to the reference, so a stretch that
        # starts near (or before) the reference's start can still be placed
        t_ref = (f0 / ENV_RATE - offset) / (1 + drift)
        pad = int(search_s * ENV_RATE)
        exp_i = int(round(t_ref * ENV_RATE))
        g0 = max(0, exp_i - pad)
        g1 = min(len(a), exp_i + len(seg) + pad)
        if g1 - g0 < len(seg) + ENV_RATE:  # less than a second of play: nothing to slide against
            log_fn(f"restart of {Path(w.names[j]).name} lies outside the reference — keeping the timestamp gap")
            continue
        ref_seg = a[g0:g1]
        size = 1 << (len(seg) + len(ref_seg) - 1).bit_length()
        corr = np.fft.irfft(np.fft.rfft(ref_seg, size) * np.conj(np.fft.rfft(seg, size)), size)
        valid = corr[:len(ref_seg) - len(seg) + 1]
        kbest = int(np.argmax(valid))
        z = (valid[kbest] - np.median(valid)) / (np.median(np.abs(valid - np.median(valid))) * 1.4826 + 1e-9)
        if z < 4.0:
            log_fn(f"restart of {Path(w.names[j]).name} could not be placed (z={z:.1f}) — keeping the timestamp gap")
            continue
        shift_s = (kbest - (exp_i - g0)) / ENV_RATE   # + : the part really starts later
        new_gap = max(0, new_gaps[k] + int(round(shift_s * (1 + drift) * w.rate)))
        if new_gap != new_gaps[k]:
            log_fn(f"restart of {Path(w.names[j]).name}: gap {new_gaps[k] / w.rate:.3f}s → {new_gap / w.rate:.3f}s (z={z:.0f})")
            new_gaps[k] = new_gap
            changed = True
    if not changed:
        return None, None
    w2 = ConcatWav(w.parts, new_gaps)
    w2.gap_exact = [True] * len(new_gaps)
    w2.names = w.names
    return w2, chain_envelope(w2)


# Clock drift beyond this is not a clock: it is a restart gap or a misplaced
# stretch leaking into the line fit. Such a fit is replaced by offset only.
MAX_DRIFT_PPM = 60.0


def _sane(r, log_fn, name):
    if abs(r["drift"]) > MAX_DRIFT_PPM * 1e-6 and r.get("points"):
        offs = np.array([pt[1] for pt in r["points"]], dtype=np.float64)
        log_fn(f"{name}: fitted drift {r['drift']*1e6:+.0f} ppm is not a clock — using the offset alone")
        r = dict(r, drift=0.0, offset=float(np.median(offs)))
    return r


def pick_reference(wavs, inexact_gaps):
    """The reference clock everyone is aligned against: among the tracks long
    enough to overlap the others (REF_MIN_SHARE of the longest), the one with
    the fewest rounded restart gaps, longest among those. Without the length
    floor, a short chain that happens to abut exactly wins — 32 minutes of
    someone who joined a 3-hour meeting — and mics that never overlap it
    can't be placed at all."""
    longest = max(w.duration for w in wavs)
    cands = [i for i, w in enumerate(wavs) if w.duration >= REF_MIN_SHARE * longest]
    fewest = min(inexact_gaps(wavs[i]) for i in cands)
    cands = [i for i in cands if inexact_gaps(wavs[i]) == fewest]
    return max(cands, key=lambda i: wavs[i].duration)


def align_group(paths, workdir, log_fn=log, ref_index=None):
    """Open, fingerprint and align a set of files. Returns (tracks, meta).

    The reference clock is the longest track whose parts abut exactly (a
    chain with a rounded restart gap would smear everyone else's drift) — or
    `ref_index` when given, so a remix keeps an existing transcript's
    timeline; the shared timeline runs from the earliest start to the latest
    end. Parts after a restart are then slid into place against the
    reference."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    wavs = [open_track(p, workdir) for p in paths]
    envs = [envelope(w) for w in wavs]
    def inexact_gaps(w):
        return sum(1 for e in w.gap_exact if not e) if isinstance(w, ConcatWav) else 0

    if ref_index is None:
        ref_i = pick_reference(wavs, inexact_gaps)
    else:
        ref_i = int(ref_index)
    names = [Path(p[0] if isinstance(p, (list, tuple)) else p).name for p in paths]

    def align_others(quiet=False):
        out = {}
        for i, w in enumerate(wavs):
            if i == ref_i:
                continue
            r = align_pair(wavs[ref_i], w, envs[ref_i], envs[i])
            if isinstance(w, ConcatWav) and not all(w.gap_exact):
                w2, env2 = refine_restart_gaps(w, envs[i], wavs[ref_i], envs[ref_i], r["offset"], r["drift"],
                                               (lambda m: None) if quiet else log_fn)
                if w2 is not None:
                    wavs[i], envs[i], w = w2, env2, w2
                    r = align_pair(wavs[ref_i], w, envs[ref_i], envs[i])
            out[i] = _sane(r, (lambda m: None) if quiet else log_fn, names[i])
        return out

    fits = align_others()
    # The reference's own restart gaps can't be refined against itself: do it
    # against the best-aligned other track, then align everyone again.
    if inexact_gaps(wavs[ref_i]) and fits:
        j = max(fits, key=lambda i: fits[i]["quality"])
        if fits[j]["quality"] >= 0.3:
            off, dr = fits[j]["offset"], fits[j]["drift"]
            w2, env2 = refine_restart_gaps(wavs[ref_i], envs[ref_i], wavs[j], envs[j], -off / (1 + dr), 1 / (1 + dr) - 1, log_fn)
            if w2 is not None:
                wavs[ref_i], envs[ref_i] = w2, env2
                # the others were refined against the old reference timeline: start them over
                for i, p in enumerate(paths):
                    if i != ref_i and isinstance(wavs[i], ConcatWav) and all(wavs[i].gap_exact) and any(not e for e in open_track(p, workdir).gap_exact if isinstance(open_track(p, workdir), ConcatWav)):
                        wavs[i] = open_track(p, workdir); envs[i] = envelope(wavs[i])
                fits = align_others()
    aligned = []
    meta = []
    for i, w in enumerate(wavs):
        if i == ref_i:
            aligned.append(Aligned(w, 0.0, 0.0, envs[i]))
            meta.append({"offset": 0.0, "drift": 0.0, "quality": 1.0})
            continue
        r = fits[i]
        aligned.append(Aligned(w, r["offset"], r["drift"], envs[i]))
        meta.append({"offset": r["offset"], "drift": r["drift"], "quality": r["quality"]})
        log_fn(f"{names[i]}: offset {r['offset']:+.3f}s, drift {r['drift']*1e6:+.1f} ppm, q={r['quality']:.2f}")
    # Shift so the shared timeline starts at the earliest track start.
    t_start = min(a.start() for a in aligned)
    t_end = max(a.end() for a in aligned)
    for a in aligned:
        # new ref time t' = t - t_start  ⇒ src = offset + (t' + t_start)(1+drift)
        a.offset = a.offset + t_start * (1 + a.drift)
    for m, a in zip(meta, aligned):
        # On the shared timeline: this file's sample at time t sits at
        # offset + t·(1+drift) seconds into the file (offset ≤ 0 when it started early).
        m["offset"] = round(a.offset, 6)
        m["drift"] = a.drift
        m["start"] = round(a.start(), 3)
        m["end"] = round(a.end(), 3)
    return aligned, meta, t_end - t_start


def build_mix(paths, out_dir, log_fn=log, progress=None, ref_index=None):
    """Align + auto-mix tracks. Writes mix.wav (+ mix.m4a when an encoder exists).

    Returns a dict describing the alignment and the frame-level speaker map.
    `ref_index` pins the reference track (see align_group)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / ".work"
    tracks, meta, duration = align_group(paths, work, log_fn, ref_index=ref_index)
    check_mix_length(duration)
    n_frames = int(math.ceil(duration * ENV_RATE))
    norm, present, own_db = compute_levels(tracks, n_frames)
    # A mic that is merely hot for a while (a hand on the transmitter, fabric)
    # must not be mixed in as if its owner were talking: judge each mic
    # against its local noise floor.
    norm = norm - local_floor_excess(norm, present)
    gains = mix_gains(norm, present)
    log_fn(f"mixing {len(tracks)} tracks, {duration/60:.1f} min…")
    wav_out = out_dir / "mix.wav"
    render_mix(tracks, gains, own_db, wav_out, n_frames, progress=progress, log_fn=log_fn)
    # Frame-level normalized levels: everything word labelling needs later.
    np.savez_compressed(out_dir / "speakers.npz", norm=norm.astype(np.float16), present=present)
    shutil.rmtree(work, ignore_errors=True)
    return {
        "duration_ms": int(duration * 1000),
        "tracks": [{"file": Path(p[0] if isinstance(p, (list, tuple)) else p).name, **m}
                   for p, m in zip(paths, meta)],
        "frame_ms": FRAME_MS,
        "mix": {"sharpness": MIX_SHARPNESS, "own_db": [round(float(x), 1) for x in own_db]},
    }


# ---------------------------------------------------------- speaker labelling

def local_floor_excess(norm, present, window_s=FLOOR_WINDOW_S):
    """Per mic and frame: how far the mic's local noise floor (10th percentile
    over `window_s`) sits above its global floor, ≥ 0. Windows where the mic
    is mostly absent count as at the global floor."""
    n, F = norm.shape
    win = max(1, int(window_s * ENV_RATE))
    nb = max(1, -(-F // win))
    centres = (np.arange(nb) + 0.5) * win
    excess = np.zeros_like(norm)
    for i in range(n):
        have = present[i]
        glob = float(np.percentile(norm[i][have], 10)) if have.any() else float(np.percentile(norm[i], 10))
        loc = np.full(nb, glob, dtype=np.float32)
        for b in range(nb):
            f0, f1 = b * win, min(F, (b + 1) * win)
            vals = norm[i, f0:f1][have[f0:f1]]
            if len(vals) >= win // 10:
                loc[b] = np.percentile(vals, 10)
        excess[i] = np.clip(np.interp(np.arange(F), centres, loc) - glob, 0.0, FLOOR_EXCESS_MAX_DB)
    return excess


def _scribe_mapping(scores, frames, prov, n):
    """Scribe label → mic, from the words one mic clearly carries: a label with
    5+ such words, 60%+ of them on one mic — leaving out labels that look like
    someone without a mic (as label_words decides it: OFFMIC_*), which come
    back as a set of their own: (mapping, unmiked labels)."""
    full = ~np.isnan(scores).any(axis=1)
    s = np.where(np.isnan(scores), -np.inf, scores)
    order = np.argsort(-s, axis=1)
    best = order[:, 0]
    gap = s[np.arange(len(s)), best] - (s[np.arange(len(s)), order[:, 1]] if n > 1 else -60)
    need = CONFIDENT_GAP_DB * np.maximum(1.0, CONFIDENT_MIN_FRAMES / np.maximum(frames, 1))
    table, unmiked = {}, {}
    for k in range(len(s)):
        if not prov[k] or not full[k]:
            continue
        if gap[k] >= need[k]:
            table.setdefault(prov[k], np.zeros(n))[best[k]] += 1
        elif gap[k] < OFFMIC_LOOK_DB and frames[k] >= OFFMIC_MIN_FRAMES:
            unmiked[prov[k]] = unmiked.get(prov[k], 0) + 1
    mapping, nobody = {}, set()
    for lbl in set(table) | set(unmiked):
        c, off = table.get(lbl, np.zeros(n)), unmiked.get(lbl, 0)
        if off >= OFFMIC_MIN_WORDS and off / (off + c.sum()) >= OFFMIC_MIN_SHARE:
            nobody.add(lbl)
        elif c.sum() >= 5 and c.max() / c.sum() >= 0.6:
            mapping[lbl] = int(np.argmax(c))
    return mapping, nobody


def _leading_mic(scores, frames, n):
    """Per word: the mic that leads, and whether it leads clearly (by
    CONFIDENT_GAP_DB, more for short words), absent mics scored as in
    label_words."""
    absent = np.isnan(scores)
    live_max = np.max(np.where(absent, -np.inf, scores), axis=1)
    mirror = np.clip(-live_max - ABSENT_MARGIN_DB, -LEVEL_CAP_DB, LEVEL_CAP_DB)
    one_live = (~absent).sum(axis=1) == 1
    s = np.where(absent, np.where(one_live[:, None], mirror[:, None], -LEVEL_CAP_DB), scores)
    order = np.argsort(-s, axis=1)
    best = order[:, 0]
    gap = s[np.arange(len(s)), best] - (s[np.arange(len(s)), order[:, 1]] if n > 1 else -60)
    need = CONFIDENT_GAP_DB * np.maximum(1.0, CONFIDENT_MIN_FRAMES / np.maximum(frames, 1))
    return best, gap >= need


def local_level_offsets(scores, frames, starts_ms, prov, mapping, nobody, n, half_s=LOCAL_LEVEL_S):
    """Per word and mic: where that mic's dividing line between its wearer's
    speech and everyone else's sits around the word's time (see LOCAL_LEVEL_S);
    0 where too few words tell. Someone without a mic (`nobody`: their Scribe
    labels) counts as everyone else for every mic."""
    out = np.zeros(scores.shape, dtype=np.float32)
    if not len(scores):
        return out
    owner = np.array([mapping.get(p, -2 if p in nobody else -1) if p else -1 for p in prov])
    usable = (owner != -1) & (frames >= LOCAL_MIN_FRAMES)
    t = np.asarray(starts_ms, dtype=np.float64) / 1000.0
    grid = np.arange(0.0, t.max() + 30.0, 30.0)
    for i in range(n):
        live = usable & ~np.isnan(scores[:, i])
        mids = np.full(len(grid), np.nan)
        for g, c in enumerate(grid):
            near = live & (np.abs(t - c) <= half_s)
            own, far = scores[near & (owner == i), i], scores[near & (owner != i), i]
            if len(own) >= LOCAL_MIN_WORDS and len(far) >= LOCAL_MIN_WORDS:
                mids[g] = (np.median(own) + np.median(far)) / 2
        ok = ~np.isnan(mids)
        if ok.any():
            out[:, i] = np.interp(t, grid[ok], mids[ok])
    return out


def label_words(words, npz_path, log_fn=log, local_levels=True):
    """Assign each transcript word to a mic using the per-frame level map.

    `words` have ms timestamps and a `speaker` from the transcription
    provider's diarization; the result maps every word to `mic_<k>` (1-based)
    and returns (labels, mic ids, stats). Scribe's labels are mapped to mics by
    co-occurrence (so a library-recognized profile name can be carried over per
    mic) and then weighed against the mic levels, word by word, together with a
    cost for changing speaker, in one Viterbi pass. A Scribe label that no mic
    carries (someone without a mic, see OFFMIC_*) is a speaker of its own: its
    words keep that label."""
    data = np.load(npz_path)
    norm = data["norm"].astype(np.float32)
    present = data["present"]
    n, F = norm.shape
    norm = norm - local_floor_excess(norm, present)
    if not words:
        return [], {}, {}
    # Per-word mean level per track over its voiced frames (min 3 frames).
    scores = np.zeros((len(words), n), dtype=np.float32)
    frames = np.zeros(len(words), dtype=np.int32)
    for k, w in enumerate(words):
        f0 = max(0, int(w["start"] // FRAME_MS))
        f1 = min(F, max(f0 + 3, int(math.ceil(w["end"] / FRAME_MS))))
        if f0 >= F:
            f0, f1 = F - 3, F
        seg = norm[:, f0:f1]
        pres = present[:, f0:f1]
        loud = np.where(pres, seg, -120.0).max(axis=0)
        keep = loud >= loud.max() - WORD_VOICED_RANGE_DB
        if keep.sum() < 3:
            keep[np.argsort(-loud)[:3]] = True
        seg, pres = seg[:, keep], pres[:, keep]
        cnt = pres.sum(axis=1)
        tot = np.where(pres, seg, 0.0).sum(axis=1)
        sc = np.where(cnt > 0, tot / np.maximum(cnt, 1), np.nan)
        if np.isnan(sc).all():
            sc = np.zeros(n, dtype=np.float32)
        scores[k] = sc
        frames[k] = int(keep.sum())
    # (whether no mic carries a word is judged on the levels as they were: the
    # local dividing lines shift each mic by its own amount, which would give
    # a voice that reaches all mics alike a gap it doesn't have)
    raw_sorted = np.sort(np.where(np.isnan(scores), -np.inf, scores), axis=1)
    _, raw_confident = _leading_mic(scores, frames, n)
    if local_levels and n > 1:
        prov0 = [w.get("speaker") for w in words]
        scores = scores - local_level_offsets(scores, frames, [w["start"] for w in words], prov0,
                                              *_scribe_mapping(scores, frames, prov0, n), n)
    absent = np.isnan(scores)
    live_sorted = np.sort(np.where(absent, -np.inf, scores), axis=1)
    live_max = live_sorted[:, -1]
    mirror = np.clip(-live_max - ABSENT_MARGIN_DB, -LEVEL_CAP_DB, LEVEL_CAP_DB)
    n_live = (~absent).sum(axis=1)
    one_live = n_live == 1
    # Speakers without a mic score as a mic OFFMIC_GAP_DB above the runner-up
    # (with one live mic, like an absent mic: the mirror of its level).
    if n > 1:
        live_gap = np.where(n_live >= 2, raw_sorted[:, -1] - raw_sorted[:, -2], np.inf)
        off_score = np.where(n_live >= 2, live_max - live_gap + OFFMIC_GAP_DB, mirror)
    else:
        live_gap = np.full(len(words), np.inf)
        off_score = mirror
    scores = np.where(absent, np.where(one_live[:, None], mirror[:, None], -LEVEL_CAP_DB), scores)
    order = np.argsort(-scores, axis=1)
    best = order[:, 0]
    gap = scores[np.arange(len(words)), best] - (scores[np.arange(len(words)), order[:, 1]] if n > 1 else -60)
    need = CONFIDENT_GAP_DB * np.maximum(1.0, CONFIDENT_MIN_FRAMES / np.maximum(frames, 1))
    confident = gap >= need

    # Map provider speaker labels ↔ mics via confident words
    prov = [w.get("speaker") for w in words]
    table = {}
    for k in np.flatnonzero(confident):
        if prov[k]:
            table.setdefault(prov[k], np.zeros(n))[best[k]] += 1
    # …except labels that no mic carries: those are people without a mic.
    unmiked = {}
    for k in np.flatnonzero(live_gap < OFFMIC_LOOK_DB):
        if prov[k] and frames[k] >= OFFMIC_MIN_FRAMES:
            unmiked[prov[k]] = unmiked.get(prov[k], 0) + 1
    off_labels, off_trust = [], {}
    on_a_mic = {}
    for k in np.flatnonzero(raw_confident):
        if prov[k]:
            on_a_mic[prov[k]] = on_a_mic.get(prov[k], 0) + 1
    for lbl, cnt in sorted(unmiked.items(), key=lambda kv: -kv[1]):
        share = cnt / (cnt + on_a_mic.get(lbl, 0))
        if cnt >= OFFMIC_MIN_WORDS and share >= OFFMIC_MIN_SHARE and not re.fullmatch(r"mic_\d+", lbl):
            off_labels.append(lbl)
            off_trust[lbl] = min(1.0, (share - OFFMIC_MIN_SHARE) / (OFFMIC_FULL_SHARE - OFFMIC_MIN_SHARE))
    off_index = {lbl: n + j for j, lbl in enumerate(off_labels)}
    mapping = {}
    for lbl, counts in table.items():
        if lbl not in off_index and counts.sum() >= 5 and counts.max() / counts.sum() >= 0.6:
            mapping[lbl] = int(np.argmax(counts))

    for k in range(len(words)):
        m = mapping.get(prov[k])
        if m is not None and absent[k, m] and not one_live[k]:
            scores[k, m] = mirror[k]
    # How much to trust each Scribe label: 0 at chance (60% agreement with
    # one mic), 1 when it always named the same mic.
    trust = {lbl: min(1.0, max(0.0, (counts.max() / counts.sum() - 0.6) / 0.4))
             for lbl, counts in table.items() if lbl in mapping}

    # Cost of labelling word k with speaker i (the mics, then anyone without
    # a mic): level deficit against the leading one (weighted by how many
    # voiced frames back it up) plus Scribe's vote against, if its label
    # maps to someone else.
    W = len(words)
    S = n + len(off_labels)
    if off_labels:
        scores = np.concatenate([scores, np.repeat(off_score[:, None], len(off_labels), axis=1)], axis=1)
    trust.update(off_trust)
    weight = np.minimum(frames, CONFIDENT_MIN_FRAMES) / CONFIDENT_MIN_FRAMES
    lead = scores.max(axis=1, keepdims=True) - scores
    cost = np.minimum(lead, LEVEL_CAP_DB) * weight[:, None]
    for k in range(W):
        m = mapping.get(prov[k], off_index.get(prov[k]))
        if m is not None:
            cost[k, :] += SCRIBE_VOTE_DB * trust[prov[k]]
            cost[k, m] -= SCRIBE_VOTE_DB * trust[prov[k]]
    # Changing speaker mid-flow is expensive, across a pause it is free.
    switch = np.zeros(W)
    for k in range(1, W):
        pause = max(0.0, words[k]["start"] - words[k - 1]["end"])
        switch[k] = SWITCH_COST_DB * max(0.0, 1.0 - pause / SWITCH_FREE_MS)
    # Viterbi over words
    acc = cost[0].copy()
    back = np.zeros((W, S), dtype=np.int32)
    for k in range(1, W):
        stay = acc                                   # same speaker as before
        move = acc.min() + switch[k]                 # from the cheapest other one
        arg_min = int(np.argmin(acc))
        better_move = move < stay
        back[k] = np.where(better_move, arg_min, np.arange(S))
        acc = np.where(better_move, move, stay) + cost[k]
    labels = np.zeros(W, dtype=np.int64)
    labels[-1] = int(np.argmin(acc))
    for k in range(W - 1, 0, -1):
        labels[k - 1] = back[k, labels[k]]

    # Mic → display id: a library profile name when Scribe recognized one for
    # (most of) that mic's words, else mic_<k>.
    mic_names = {}
    for lbl, m in mapping.items():
        if lbl and not re.fullmatch(r"speaker_\d+|[A-Z]", lbl):
            mic_names.setdefault(m, []).append((table[lbl][m], lbl))
    ids = []
    for i in range(n):
        cands = sorted(mic_names.get(i, []), reverse=True)
        ids.append(cands[0][1] if cands else f"mic_{i+1}")
    stats = {
        "confident": int(confident.sum()), "total": len(words),
        "provider_map": {lbl: ids[m] for lbl, m in mapping.items()},
        "words_per_mic": {ids[i]: int((labels == i).sum()) for i in range(n)},
    }
    if off_labels:
        stats["no_mic"] = {lbl: int((labels == off_index[lbl]).sum()) for lbl in off_labels}
    log_fn(f"speakers from mics: {stats['confident']}/{len(words)} words confident; "
           + ", ".join(f"{k}={v}" for k, v in stats["words_per_mic"].items())
           + "".join(f"; {k}={v} (no mic)" for k, v in stats.get("no_mic", {}).items()))
    ids += off_labels
    return [ids[i] for i in labels], ids[:n], stats


def regroup_utterances(words):
    """Rebuild utterances after relabelling (same rule as to_transcript)."""
    utterances = []
    cur = None
    for w in words:
        if (cur is None or w["speaker"] != cur["speaker"]
                or w["start"] - cur["end"] > 1500 or len(cur["texts"]) >= 200):
            cur = {"speaker": w["speaker"], "start": w["start"], "end": w["end"], "texts": []}
            utterances.append(cur)
        cur["end"] = w["end"]
        cur["texts"].append(w["text"])
    return [{"speaker": u["speaker"], "start": u["start"], "end": u["end"],
             "text": " ".join(u["texts"])} for u in utterances]


# --------------------------------------------------------------- grouping

FP_VERSION = 1

# Recorder file names carry a timestamp; DJI: TX01_MIC002_20250527_202904_orig.wav
# (TX01 = which transmitter, MIC002 = running number, then date_time, then a
# variant — DJI saves every take twice, _orig raw and _edit processed).
NAME_RE = re.compile(r"^(?P<prefix>.*?)[_-]?(?P<idx>\d{2,4})?[_-](?P<ts>20\d{2}\d{2}\d{2}[_-]?\d{6})(?P<rest>.*)$")


def parse_name(name):
    """(prefix, index or None, timestamp as epoch seconds or None, variant) from
    a file name; `variant` is whatever follows the timestamp ("_orig", "")."""
    stem = Path(name).stem
    m = NAME_RE.match(stem)
    if not m:
        return stem, None, None, ""
    ts = re.sub(r"[_-]", "", m.group("ts"))
    try:
        import datetime as _dt
        t = _dt.datetime.strptime(ts, "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        t = None
    idx = int(m.group("idx")) if m.group("idx") else None
    return m.group("prefix"), idx, t, m.group("rest")


def _link_rank(prev, nxt, tolerance_s):
    """How plausibly file `nxt` continues file `prev` of the same recorder
    (lower is better): a running number one up, then a seamless split before
    a restart, then the smaller timing error. Two transmitters can carry the
    same name (every DJI kit calls its first one TX01) and, started together,
    save their 30-minute parts a second or two apart — the running number is
    what tells them apart, since each transmitter counts its own files."""
    delta = nxt["t"] - (prev["t"] + prev["dur"])
    if prev["idx"] is None or nxt["idx"] is None:
        numbered = 1
    else:
        numbered = 0 if nxt["idx"] == prev["idx"] + 1 else 2
    return (numbered, 0 if abs(delta) <= tolerance_s else 1, abs(delta))


def _not_next(prev, nxt):
    """Both files carry a running number and `nxt`'s isn't one up from
    `prev`'s: then it's another transmitter's file. Each transmitter numbers
    its own files one up, across 30-minute splits and restarts alike, while
    same-named transmitters of two kits count on their own and overlap (two
    TX01s can both have a MIC019)."""
    return prev["idx"] is not None and nxt["idx"] is not None and nxt["idx"] != prev["idx"] + 1


def _ties(links, span):
    """Restart links (not seamless splits) with a rival as good: another link
    from the same file into one that overlaps this one's in time, or into the
    same file from one overlapping this one's, with the same numbering and
    also a restart. Same-named transmitters of two kits, restarted together
    on the same running number (seen: both at MIC020→MIC021, ten seconds
    apart), can't be told apart by name or timing, so neither link is made:
    two mics for one person beats one mic for two. (Files one after another
    never overlap, so a sequence still chains.) `links` are (rank, a, b) with
    rank as _link_rank's; span(k) is (start, end) of node k."""
    def overlap(x, y):
        (s1, e1), (s2, e2) = span(x), span(y)
        return s1 < e2 and s2 < e1

    tied = set()
    for r1, a1, b1 in links:
        if r1[1] == 0:
            continue
        for r2, a2, b2 in links:
            if r2[:2] == r1[:2] and ((a2 == a1 and b2 != b1 and overlap(b2, b1))
                                     or (b2 == b1 and a2 != a1 and overlap(a2, a1))):
                tied.add((a1, b1))
                break
    return tied


def chain_parts(paths, durations, tolerance_s=8.0, max_gap_s=CHAIN_MAX_GAP_S):
    """Group files that are consecutive segments of one recorder into chains.

    Two files chain when they share a name prefix (and variant suffix — an
    _orig never chains with an _edit) and the second starts (by its timestamp)
    where the first ends: within `tolerance_s` for a seamless split, or after
    a gap of up to `max_gap_s` (a stop and restart; the gap is filled with
    silence when the chain is read). Several recorders may share a prefix and
    record at the same time, so every file gets at most one successor and one
    predecessor, best links first (see _link_rank), a numbered file only ever
    continues the one numbered one below it (_not_next), and a restart that
    another file could continue just as well is left alone (_ties). Returns a list of chains
    (lists of paths in time order); unrelated files are one-element chains."""
    info = []
    for p, d in zip(paths, durations):
        prefix, idx, t, variant = parse_name(Path(p).name)
        info.append({"path": p, "prefix": (prefix, variant), "idx": idx, "t": t, "dur": d})
    by_prefix = {}
    for k, it in enumerate(info):
        if it["t"] is not None:
            by_prefix.setdefault(it["prefix"], []).append(k)
    links = []
    for ks in by_prefix.values():
        for a in ks:
            for b in ks:
                delta = info[b]["t"] - (info[a]["t"] + info[a]["dur"])
                if info[b]["t"] > info[a]["t"] and -tolerance_s <= delta <= max_gap_s and not _not_next(info[a], info[b]):
                    links.append((_link_rank(info[a], info[b], tolerance_s), a, b))
    links.sort()
    tied = _ties(links, lambda k: (info[k]["t"], info[k]["t"] + info[k]["dur"]))
    nxt, prv = {}, {}
    for _, a, b in links:
        if a not in nxt and b not in prv and (a, b) not in tied:
            nxt[a], prv[b] = b, a
    chains = []
    for k in sorted(range(len(info)), key=lambda k: (info[k]["t"] is None, info[k]["t"] or 0)):
        if k in prv:
            continue
        chain = [k]
        while chain[-1] in nxt:
            chain.append(nxt[chain[-1]])
        chains.append([info[c]["path"] for c in chain])
    return chains


def _heard_together(a, b, fingerprint):
    """Whether chains `a` and `b` recorded the same sound at the same time:
    then they are two recorders, whatever their names and clocks say."""
    try:
        lag, score, overlap = pair_match(fingerprint(a), fingerprint(b))
    except Exception:
        return False
    return score >= MATCH_MIN_SCORE and overlap >= MATCH_MIN_OVERLAP_S


def fold_recorders(chains, durations=None, tolerance_s=8.0, fingerprint=None, max_gap_s=None):
    """Chains that turn out to be in the same conversation and come from the
    same recorder (same name prefix and variant, non-overlapping in time) are
    one person: merge them into one chain in time order. A chain that could
    continue several (same-named recorders) goes with the best link, as in
    chain_parts. `durations` maps path → seconds (used to check for overlap;
    if missing, only the order is checked).

    Same-named isn't same recorder: every DJI kit calls its first transmitter
    TX01, their running numbers overlap, and another kit's TX01 whose clock is
    hours off looks like a restart of this one long after it stopped (seen: a
    15 h fast clock, the gap filled with silence, a mix over the WAV 4 GB
    limit). So a chain only continues another if its running number is the
    next one (a transmitter numbers every file one up, restarts included), if
    it restarts while the conversation could still be going — within the
    longest chain's length (and at least CHAIN_MAX_GAP_S) of the other's end —
    and, given `fingerprint` (chain → its envelope, as file_fingerprint), if
    the two didn't record the same sound at the same time. `max_gap_s`
    overrides that longest-chain limit (COMBINE: the person picked recordings
    of one conversation, or ones made one after another, whatever the gaps).
    A restart two chains could continue equally well is left alone (_ties)."""
    def part(path):
        prefix, idx, t, variant = parse_name(Path(path).name)
        return {"idx": idx, "t": t, "dur": (durations or {}).get(path, 0.0)}

    def span(ch):
        a, b = part(ch[0]), part(ch[-1])
        return b["t"] + b["dur"] - a["t"] if a["t"] is not None and b["t"] is not None else 0.0

    max_gap = max([CHAIN_MAX_GAP_S] + [span(ch) for ch in chains]) if max_gap_s is None else max_gap_s
    by_rec = {}
    for ch in chains:
        prefix, _, t, variant = parse_name(Path(ch[0]).name)
        key = (prefix, variant) if t is not None else (id(ch),)
        by_rec.setdefault(key, []).append(ch)
    def link(a, b):
        """_link_rank of chain b continuing chain a, or None if it can't."""
        last, first = part(a[-1]), part(b[0])
        if first["t"] < last["t"] + last["dur"] - tolerance_s:
            return None  # overlapping in time: really another recorder
        if first["t"] - (last["t"] + last["dur"]) > max_gap:
            return None  # starts after the conversation is over: another kit's, its clock off
        if _not_next(last, first):
            return None  # not this transmitter's next file: another one's count
        if fingerprint is not None and _heard_together(a, b, fingerprint):
            return None  # overlapping by sound: likewise, whatever the clocks say
        return _link_rank(last, first, tolerance_s)

    out = []
    for key, chs in by_rec.items():
        if len(chs) == 1 or len(key) == 1:
            out.extend(chs)
            continue
        links = sorted((r, i, j) for i, a in enumerate(chs) for j, b in enumerate(chs)
                       if i != j and (r := link(a, b)) is not None)
        tied = _ties(links, lambda i: (part(chs[i][0])["t"], part(chs[i][-1])["t"] + part(chs[i][-1])["dur"]))
        nxt, prv = {}, {}
        for _, i, j in links:
            if i not in nxt and j not in prv and (i, j) not in tied:
                nxt[i], prv[j] = j, i
        for i in sorted(range(len(chs)), key=lambda i: parse_name(Path(chs[i][0]).name)[2]):
            if i in prv:
                continue
            merged = list(chs[i])
            while i in nxt:
                i = nxt[i]
                merged += chs[i]
            out.append(merged)
    return out


def file_duration(path, workdir=None):
    """Seconds of audio in a file (header read for WAV, afinfo/afconvert otherwise)."""
    path = Path(path)
    if path.suffix.lower() == ".wav":
        try:
            return WavData(path).duration
        except ValueError:
            pass
    if path.suffix.lower() == ".aac":  # raw ADTS: afinfo only guesses from the bitrate (a quarter short, seen)
        with tempfile.TemporaryDirectory(dir=workdir) as tmp:
            return WavData(decode_to_wav(path, Path(tmp) / "length.wav")).duration
    if _tool("afinfo"):
        proc = subprocess.run(["afinfo", str(path)], capture_output=True, text=True)
        m = re.search(r"estimated duration:\s*([\d.]+)", proc.stdout)
        if m:
            return float(m.group(1))
    if _tool("ffprobe"):
        proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "csv=p=0", str(path)], capture_output=True, text=True)
        try:
            return float(proc.stdout.strip())
        except ValueError:
            pass
    return 0.0


def file_fingerprint(path, cache_dir):
    """Envelope fingerprint for grouping, cached next to the inbox as .npy.

    A list of paths (one recorder's consecutive parts) yields one concatenated
    fingerprint."""
    if isinstance(path, (list, tuple)):
        envs = [file_fingerprint(p, cache_dir) for p in path]
        gaps = part_gaps(list(path), [len(e) / ENV_RATE for e in envs])
        floor = float(np.percentile(np.concatenate(envs), 10))
        out = [envs[0]]
        for env, gap in zip(envs[1:], gaps):
            out.append(np.full(int(round(gap * ENV_RATE)), floor, dtype=np.float32))
            out.append(env)
        return np.concatenate(out)
    path = Path(path)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    st = path.stat()
    import hashlib
    where = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:8]
    key = f"{path.stem}.{where}.{st.st_size}.{int(st.st_mtime)}.v{FP_VERSION}.npy"
    cached = cache_dir / key
    if cached.exists():
        return np.load(cached)
    with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
        w = open_track(path, tmp)
        env = envelope(w)
        del w
    np.save(cached, env)
    return env


def _windowed_match(env_a, env_b, expect_s):
    """The lag (frames) on which most MATCH_WINDOW_S windows of the stretch the
    clocks say both recorded agree, each window's peak looked for near the lag
    the clocks predict — or None (fewer than two windows, or no agreement)."""
    W = int(MATCH_WINDOW_S * ENV_RATE)
    e = int(round(expect_s * ENV_RATE))  # b starts e frames after a
    lo, hi = max(0, e), min(len(env_a), len(env_b) + e)
    starts = list(range(lo, hi - W // 2, W))
    if len(starts) < 2:
        return None
    found = []
    for s0 in starts:
        s1 = min(hi, s0 + W)
        lag, score, _ = pair_match(env_a[s0:s1], env_b[s0 - e:s1 - e], expect_s=0.0)
        if score >= MATCH_MIN_SCORE_TIMED:
            found.append(lag)
    if len(found) < 2:
        return None
    med = float(np.median(found))
    agree = sum(abs(lag - med) <= ENV_RATE // 2 for lag in found)
    return e + int(round(med)) if agree >= 2 and 2 * agree >= len(starts) else None


def group_files(fps, min_score=MATCH_MIN_SCORE, min_overlap=MATCH_MIN_OVERLAP_S):
    """Cluster fingerprints into conversations.

    fps: list of (key, envelope). Returns list of groups, each a list of
    (key, lag_seconds_relative_to_group_reference, score). Singletons are
    returned as one-element groups."""
    n = len(fps)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def start_time(key):
        names = key if isinstance(key, (list, tuple)) else [key]
        return parse_name(Path(names[0]).name)[2]

    edges = {}
    for i in range(n):
        for j in range(i + 1, n):
            ea, eb = fps[i][1], fps[j][1]
            # Quick reject: no way to overlap min_overlap if either is shorter
            if min(len(ea), len(eb)) / ENV_RATE < min_overlap:
                continue
            lag, score, overlap = pair_match(ea, eb)
            ok = score >= min_score and overlap >= min_overlap
            ta, tb = start_time(fps[i][0]), start_time(fps[j][0])
            if ok and ta is not None and tb is not None and all(
                    DJI_NAME_RE.match(Path(names[0]).name)
                    for names in (fps[i][0] if isinstance(fps[i][0], (list, tuple)) else [fps[i][0]],
                                  fps[j][0] if isinstance(fps[j][0], (list, tuple)) else [fps[j][0]])):
                if abs(lag / ENV_RATE - (tb - ta)) > MATCH_DJI_CLOCK_S:
                    ok = False  # the sound says they overlap; their clocks say they can't
            if not ok:
                # Recorder timestamps predict the offset: look for the peak there.
                ta, tb = start_time(fps[i][0]), start_time(fps[j][0])
                if ta is not None and tb is not None:
                    expect = tb - ta
                    timed_overlap = min(len(ea) / ENV_RATE - expect, len(eb) / ENV_RATE + expect)
                    if timed_overlap >= min_overlap:
                        lag2, score2, overlap2 = pair_match(ea, eb, expect_s=expect)
                        if score2 >= MATCH_MIN_SCORE_TIMED and overlap2 >= min_overlap:
                            lag, score, overlap, ok = lag2, score2, overlap2, True
                        else:
                            lag3 = _windowed_match(ea, eb, expect)
                            if lag3 is not None:
                                lag, score, overlap, ok = lag3, MATCH_MIN_SCORE_TIMED, timed_overlap, True
            edges[(i, j)] = (lag, score, overlap)
            if ok:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = []
    for members in groups.values():
        members.sort(key=lambda i: -len(fps[i][1]))
        ref = members[0]
        g = [(fps[ref][0], 0.0, float("inf"))]
        for m in members[1:]:
            key = (min(ref, m), max(ref, m))
            lag, score, _ = edges[key]
            if key[0] != ref:
                lag = -lag
            g.append((fps[m][0], -lag / ENV_RATE, score))
        out.append(g)
    return out, edges
