# Reel

Transcribe audio files with a state-of-the-art speech model, then read the
transcript in your browser and **click any word to jump the audio to that
moment**. Speaker labels, timecodes, keyboard shortcuts, everything stored
locally. No dependencies beyond Python 3 (multi-mic support additionally needs numpy).

```
python3 transcribe.py                  # open the library UI
python3 transcribe.py interview.m4a    # transcribe a file, then open the UI
```

The library opens in a light look by default; the ☾ DARK / ☀ LIGHT button in
its header switches, and the choice is remembered in the browser. You can also
drag-and-drop audio files straight onto the page, or hit
**● RECORD** in the library to capture the microphone directly — stop, and the
recording is transcribed like any other file (with pause/resume and a live
input meter while recording). Each recording remembers where you stopped
listening and resumes there. If a transcription fails (network, key, plan
limit), the library row and the recording page show the error with a
**↻ RETRY** button that re-runs it from the saved audio — nothing needs
re-uploading, and speaker names you've set are kept. After transcription, Claude names the recording
from its content (stored as `ai_title` in `item.json`; the original filename
is kept and shown on hover). **Click the title** on a recording's page to
edit it (Enter saves, Esc cancels); a title you type is yours and is kept
even when the recording is rebuilt, e.g. when another mic is added.
**↻ RETITLE** has Claude write a new one (as does saving it empty;
`POST /api/item/<id>/name` with `{"title": "…"}` or `{}` does the same).

## Getting started

Reel is made for macOS (it uses macOS's built-in `afconvert` for audio; on
Linux, installing `ffmpeg` should stand in for it, untested). It runs on
Windows too: see [On Windows](#on-windows). You need:

- **Python 3.9 or newer.** The one that comes with macOS is fine: type
  `python3` in Terminal, and if it isn't there yet macOS offers to install the
  command line tools (or run `xcode-select --install`).
- **An ElevenLabs API key** for transcription (a paid plan that includes
  speech to text). **Before you transcribe anything, opt out of training** —
  see [API keys and privacy](#api-keys-and-privacy).
- Optionally, **an Anthropic API key** for titles and the filler clean-up.
- Optionally, **numpy** (multi-mic recordings, cutting and splitting) and
  **DeepFilterNet** (denoised listening copies) — `bin/setup.sh` installs
  both — and **Google Drive for desktop** if you want the
  [backup](#backing-up-to-google-drive).

Then, in Terminal (or download the ZIP from GitHub's **Code** button instead
of cloning):

```
git clone <this repository's URL> reel
cd reel
bin/setup.sh            # optional: numpy and DeepFilterNet
python3 transcribe.py
```

Reel opens in your browser at `http://127.0.0.1:8765` and asks for your keys:
click **⚙ ADD YOUR API KEYS** (or **⚙ SETTINGS**), paste the ElevenLabs key,
and Reel checks it with ElevenLabs and keeps it. Then drop an audio file on
the page. Your recordings live in `~/Reel Recordings`; Reel runs until you press Ctrl-C
in its Terminal window.

**Easiest of all, with [Claude Code](https://claude.com/claude-code):** open
Terminal, run `claude`, and paste

    Set up Reel for me: clone <this repository's URL> into ~/reel, then follow SETUP-CLAUDE.md in it.

It installs what's missing, starts Reel, has you paste your keys into the
page (Claude never sees them) and runs a test transcription.

## On Windows

Reel runs on Windows 10 and 11, mic import and clearing included (tested on
Windows 11 with a DJI Mic 3). What's different from the Mac:

- **Setup.** Install Python 3.9 or newer (`winget install Python.Python.3.13`,
  or python.org) and git (`winget install Git.Git`), then, in PowerShell:

  ```
  git clone <this repository's URL> reel
  cd reel
  powershell -ExecutionPolicy Bypass -File bin\setup.ps1
  python transcribe.py
  ```

  `bin\setup.ps1` installs **ffmpeg** (through winget), **numpy** and
  **DeepFilterNet**. ffmpeg is needed here, not optional: Windows has no
  `afconvert`, and ffmpeg does its job (decoding recordings for mixing and
  denoising, encoding the mix). Open a new terminal after it's installed so
  it's on PATH. Commands are `python`, not `python3`; recordings live in
  `%USERPROFILE%\Reel Recordings`.
- **Plugging in the mics.** A DJI Mic 3 hands its recordings over through the
  charging case: transmitters in, lid shut, case on USB-C. Each transmitter
  shows up as a drive of its own (D:, E:, …, the disk named "Wireless Mic Tx"),
  and Reel picks up any removable USB or SD drive with recorder files on it,
  just as it does `/Volumes` on a Mac. Copying, checking and **CLEAR THE MIC
  AFTER COPYING** work the same: a file is deleted from the mic only once Reel
  holds a byte-identical copy of it on another disk. Only removable drives are
  looked at; a transmitter that shows up as a fixed disk isn't, so copy its
  files into the inbox by hand.
- **Google Drive.** Drive for desktop puts your Drive on a drive letter (G:
  by default, with `My Drive` at its top) or, in mirror mode, in
  `%USERPROFILE%\My Drive`; Reel finds either. Drive for desktop has to be
  running and signed in for the folder to be there.
- **Test audio**, in place of macOS's `say`:

  ```
  Add-Type -AssemblyName System.Speech
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $s.SetOutputToWaveFile("$env:TEMP\reel-test.wav"); $s.Speak("This is a test of Reel transcription."); $s.Dispose()
  ```
- **Smaller things.** Editing a recording copies its audio in full where a Mac
  makes an instant APFS clone, so it briefly takes more disk. In Windows
  PowerShell 5, `curl` is another command: use `curl.exe` for the API
  examples.

## Several recorders, one conversation (multi-mic)

If everyone in a conversation wears their own recorder (DJI Mic style: each
transmitter records its wearer loud and everyone else quiet), Reel turns the
pile of files into a single recording whose speakers are known for certain.

**Plug the mics in** (USB) while Reel is running, all of them if you can: a DJI
transmitter mounts as a "Mic Tx" volume, and Reel copies its new recordings
into the inbox (`~/Reel Recordings/inbox/`) by itself — the raw `_orig` of each take, its
`_edit` twin into `inbox/skipped/` (Reel doesn't use it; empty that folder
whenever), only files it hasn't seen (not in the inbox, the library, the
archive, or copied before: `inbox/.imported.json`). The recordings stay on the
mic; the INBOX panel shows COPYING, then "… COPIED · LEFT ON THE MIC · SAFE TO
UNPLUG".

To have Reel **clear the mic** as well, tick **CLEAR THE MIC AFTER COPYING** in
⚙ SETTINGS (it is off until you do). Then a file is deleted from the recorder
only once Reel holds a byte-identical copy of that very file (SHA-256 — of what
was read off the mic against the copy re-read from disk after flushing it to
the drive, or, for a file copied some other way, of both files), and only if
every copy that time succeeded. A file with no identical copy in Reel stays on
the mic and is reported. Nothing is deleted while a copy by hand into the inbox
is still running. The INBOX panel shows COPYING / CHECKING / CLEARING, then
"… CLEARED FROM THE MIC · SAFE TO UNPLUG". The setting takes effect from the
next time a recorder is plugged in, and also clears recordings left on it
earlier that Reel holds copies of.

Several mics plugged in at once copy side by side (a DJI transmitter reads at
~13 MB/s, its own limit, so two take as long as one). The inbox sorts nothing
while recordings are being copied — the `_edit` twins don't hold it up — and
then waits the usual 2 minutes for a partner mic. An SD card in a reader works
the same. `REEL_AUTO_IMPORT=0` (in `.env` next to `transcribe.py`, or the
environment) turns the copying off altogether.
Or **drop the recorders' files into the inbox** yourself — Finder
drag-and-drop is fine, straight from the SD cards.
The library page shows an INBOX panel while files are being sorted. What
happens, per file, once it has finished copying:

1. **Matching.** Its loudness envelope is cross-correlated with every other
   waiting file (and with the library, in case a partner file was already
   transcribed on its own). Files from the same conversation correlate
   strongly at their true offset; unrelated ones don't. Any number of mics per
   conversation works, and several conversations can sit in the inbox at once.
   Recorders that save a new file every 30 minutes (DJI does) are handled:
   consecutive files of one recorder — same name prefix, and a timestamp that
   picks up where the previous file ended — are chained into one track first.
   Two transmitters may carry the same name (every DJI kit calls its first one
   `TX01`) and, started together, save their parts a second or two apart; the
   running number tells them apart (`MIC014`→`MIC015` is one transmitter,
   `MIC020`→`MIC021` the other): a file only ever continues the one numbered
   one below it, since each transmitter numbers its files one up, restarts
   included. A recorder that was stopped and restarted (up to 10 minutes
   later) chains too, the gap filled with silence, and if a restart only
   shows up as a separate match it is folded into the same mic rather than
   becoming a third one — as long as it restarts while the conversation
   could still be going (within the longest mic's length of where it
   stopped), with the next running number, and without having recorded the
   same sound at the same time as that mic. Another kit's `TX01` whose clock
   is hours off would otherwise pass for a restart long after this one
   stopped; it stays a mic of its own, and the alignment puts it in place by
   its audio. Kits' running numbers overlap, though, and when two same-named
   transmitters restart together on the same number (both `MIC020`→`MIC021`,
   seconds apart), nothing in the names or timing says whose file is whose:
   rather than guess, and risk giving one person's words to another, those
   files stay mics of their own (give them the same speaker name). Gaps come from the WAVs' bext time reference (sample-accurate: DJI's
   30-minute splits continue exactly where the previous file ended); after a
   restart, where that reference starts over, the names' whole seconds give
   a first guess and the restarted part is then slid into place against the
   other mics (to 10 ms). The reference clock is the longest recorder whose
   parts abut exactly. Two lav mics on two people pick up little of each other, so the
   envelope match alone is often weak; when the recorders' timestamps say two
   files overlap, a much weaker peak at the offset the timestamps predict is
   enough.
   DJI's twin files are handled too: each take comes as `…_orig.wav` (the raw
   32-bit float capture) and `…_edit.wav` (the same audio with the
   transmitter's own processing baked in — its limiter, plus noise cancelling
   and low-cut when those are switched on, which also delays it ~7 ms). Reel
   uses `_orig`. An `_edit` whose twin is in the inbox shows as NOT USED and
   moves to `inbox/skipped/` once the twin has been taken (delete that folder
   whenever); an `_edit` on its own waits the grace period below for its twin
   to arrive, then goes ahead as it is.
   An unmatched file waits `INBOX_SINGLE_DELAY_S` (2 min) for a partner, then
   becomes an ordinary single-track recording. A file whose timestamps overlap
   a recording that is still being processed waits for it (up to 30 min)
   rather than going ahead alone; with timestamps, lengths don't need to be
   similar, so someone who was there for ten minutes of an hour still matches.

   **Adding a mic later.** A file dropped in later matches any recording in
   the library and is attached to it as another mic. Adding mics rebuilds the
   recording (re-align, re-mix, re-transcribe), and its old mix, transcript
   and clean-up are replaced. So Reel first keeps them (APFS clones in
   `archive/before-rebuild/`), and if anything fails it puts the recording back
   exactly as it was, sets the new mics' files aside in the recording's `held/`
   folder, and the recording says why, with ↻ TRY AGAIN and LEAVE OUT (which
   moves those files to the archive). Reel restarted mid-rebuild picks it up
   again. A mix that would be longer than a WAV can hold (about 12 hours) is
   refused before anything is written. And when one conversation still ends
   up as two recordings — say two mics copied in together after a third was
   transcribed on its own — Reel notices once both are finished (their
   recorder timestamps overlap and their audio matches) and merges the newer
   into the older, archiving the separate copy. **SORT NOW** skips the wait,
   **ALONE** forces one file through single-track. (Forcing unrelated files
   into one conversation is still possible from the command line,
   `--group A B`, or via `POST /api/inbox/group`; the UI doesn't offer it.)
2. **Alignment.** Recorders start at different moments and their clocks run at
   slightly different speeds (tens of ppm, which is hundreds of milliseconds
   over an hour). Reel finds each file's start offset and drift — coarse from
   the envelopes, then sample-accurate from the waveforms — and resamples the
   files onto one shared timeline. The recording page's "N MICS" tooltip shows
   what it found.
3. **Auto-mix.** The tracks are summed with Dugan-style gain sharing: each
   mic's gain follows its share of the total level at that instant, so the mic
   nearest the speaker carries them while the others drop 20–40 dB (levels are
   judged against each mic's local noise floor, so a mic that is merely hot —
   a hand on the transmitter — is not mixed in as a voice). Room tone
   stays steady in pauses, overlapping speech keeps both voices, and every
   speaker's own-mic voice is trimmed to similar loudness. The mix is scaled so
   that speech peaks sit at −1 dBFS; the odd knock on a mic — which a 32-bit
   float recorder captures far above full scale — is caught by a lookahead
   limiter instead of making the whole recording quieter. The result is
   encoded to AAC (`audio.m4a`) and is what the player plays. The raw files
   stay in the recording's `tracks/` folder — the MIX / MIC 1 / MIC 2 toggle on
   the recording page plays any of them, position preserved.
   **Optional denoising for listening:** with DeepFilterNet's `deep-filter`
   binary (and `DeepFilterNet3_onnx.tar.gz`) in `bin/` — `bin/setup.sh` fetches
   them — every finished recording also gets a denoised playback copy
   (`denoised.m4a`, attenuation limited to 25 dB so voices stay natural; ~30×
   real time on Apple silicon, entirely offline). A DENOISED toggle on the
   recording page switches between it and the untouched mix, and the choice is
   remembered. Scribe always transcribes the untouched audio — denoisers make
   audio nicer for people and measurably worse for recognizers.
4. **Transcription + speakers.** The mix goes to Scribe like any recording.
   Then every word is assigned to a mic by weighing two kinds of evidence in
   one pass: the level gap between mics over the word's voiced frames (judged
   against each mic's *local* noise floor, so a hand on a transmitter doesn't
   make it "speak"), and Scribe's voice-based diarization, weighted by how
   consistently each of its labels has agreed with the mics. Changing speaker
   mid-flow costs something, changing across a pause is free — so a quiet
   80 ms "the" can't flip to the other person, while a real "Yeah" still can.
   A mic that wasn't recording yet is judged by mirror: quiet bleed on the
   only live mic means someone else spoke, and Scribe says who. A mic's levels
   drift during a recording (clipped on, then on the table, then under a
   jumper), so each mic's dividing line between its wearer and everyone else
   is measured within ±2 minutes of every word, from the words Scribe's voice
   recognition attributes there; a mic that stops telling voices apart then
   cancels out and the other mics decide. If the speaker
   library recognized a person on some mic, that mic gets their name;
   otherwise speakers are "Mic 1", "Mic 2", … — rename them by clicking, as
   usual. **Not everyone needs a mic.** Someone without one (or a video
   playing) reaches every mic from a distance, so no mic leads — the loudest
   mic is ~1 dB ahead of the next, where a wearer's own mic is typically
   ~10 dB ahead. A Scribe voice whose words mostly look like that becomes a
   speaker of its own and keeps Scribe's label ("Speaker 4", or their name if
   the speaker library knows them) instead of being pinned on whoever sat
   nearest; the item's `speaker_stats.no_mic` counts their words. Their voice
   is in the mix only as the mics' bleed, so it plays quieter (~10 dB) than
   the people wearing mics. When the labelling improves (`LABEL_VERSION`), finished recordings
   are relabelled at the next start, at no cost. A recording with a single
   recorder keeps Scribe's diarization as it is.

Up to three conversations are aligned and mixed at once (fewer on a Mac with
less than 12 cores or 24 GB). Everything after step 1 is local (numpy only; the decoding/encoding uses
macOS's built-in `afconvert`, or `ffmpeg` if installed). Only the mixed audio
leaves the machine, once. `python3 transcribe.py --group A.WAV B.WAV` does the
same for files you pass explicitly, and `--ingest` sorts the inbox immediately
at start-up. If the inbox got it wrong — two halves of one conversation
transcribed as single-mic recordings, or a restart counted as an extra mic —
⧉ COMBINE… on the main page fixes it: pick the recordings (tick them, or click
their rows), then ⧉ COMBINE. Each recording's mics are kept as they are and
only joined end to end where one continues another (as above, with no limit
on the gap: you picked them together), then aligned and mixed into one
recording, which is transcribed afresh: mics that
recorded at the same time become one conversation, recordings made one after
another are joined with silence for the time in between (a file without a
recorder timestamp in its name counts by when it was recorded). Signal voice
notes (`signal-2026-10-14-09-31-05-250.aac`: named by when they were *sent*)
are joined end to end instead, in the order sent, when all you picked are
voice notes — the time between them was spent listening. They must be in
the same folder; the result keeps their notes. ⧉ UNCOMBINE on the combined
recording's page (or next to RETRY if it failed) puts them back exactly as
they were — titles, transcripts, clean-ups, speaker names — and archives the
combined one: each original, minus its recorder files, waits inside it in
`originals/`. From the command line,
`python3 transcribe.py --regroup <id> <id> …` (recording folder names; repeat
`--regroup` for several conversations) rebuilds one conversation from those
recordings' files and transcribes it afresh; the old ones are removed. The
other way round, `--split <id> 25:40` makes two recordings of one, divided
25:40 in — the same as ⫽ SPLIT BEFORE IT on the recording's page (see
[Using the player](#using-the-player)): the transcript is divided rather than
transcribed again, and the original is archived.
`--remix <id> …` redoes only the alignment, mix and speaker labels of finished
recordings (e.g. after an alignment fix), in the background, without
transcribing again — timestamps follow the longest recorder's own timeline,
which a remix leaves alone. Single-track recordings work exactly as before. The library lists recordings
by when they were recorded (from the recorder files' timestamps, else the
file's date), not by when they were added.

## Cleaned-up view (Claude)

The clean-up runs by itself on every new transcript (`CLEAN_AUTO` in
`transcribe.py`; skipped without an Anthropic key). Every transcript also has a
**✂ CLEAN UP FILLER** button. It sends the transcript
text (never the audio) to the Claude API, which returns two kinds of edits:
word indices to *hide* (filler sounds, stutters, false starts, empty
"like"/"you know") and *span rewrites* `{s, e, text}` that fix transcription
errors, punctuation (missing/stray commas, sentence breaks), and
capitalization, using the recording's context (title, speaker names and your
key terms).

A rewrite's words get timestamps remapped
proportionally across the original span it replaces, so **every displayed
word stays click-to-seek** — corrected words show a dotted underline, with
the original transcription on hover. Toggle **VERBATIM / CLEANED** in the
transcript header (↻ re-runs the clean-up); copy/export follows whichever
view is active.

The instructions Claude follows are editable in the app: **✎ PROMPT** in a
transcript's header opens the system prompt in an editor. The built-in prompt
knows nothing about you, so a sentence or two of context — who usually
speaks, what you talk about, names that come up — helps it fix garbled
words; add it and **SAVE AS DEFAULT**. The prompt is global — whatever is
saved is used for every clean-up — and there are three tiers, from the
bottom up:

- **Built-in** — `CLEAN_SYSTEM` in `transcribe.py`. Never changes; the
  **BUILT-IN** button loads it into the editor.
- **Your default** — **SAVE AS DEFAULT** stores the editor text as
  `clean_prompt_default.txt` and makes it the active prompt. This is what
  **RESET TO DEFAULT** restores.
- **Active** — **SAVE** stores the editor text as `clean_prompt.txt` and uses
  it from the next clean-up on, without touching your default. Saving text
  identical to the default just removes this file. **SAVE & RE-CLEAN** saves
  and reprocesses the current recording right away.

Both files sit next to `transcribe.py` (git ignores them), so they're easy to
hand-edit; deleting them falls back a tier. The JSON reply format is enforced
separately via a schema, so you can rewrite the rules freely; keep the
description of what "remove" and "edit" mean if you want sensible results.

Domain vocabulary lives in **`keyterms.txt`** (one term per line, optional
gloss in parentheses: `ASR (automatic speech recognition)`). It's used twice:
sent to ElevenLabs as transcription keyterms so new recordings get the terms
right, and given to Claude's clean-up pass so it can fix mis-transcriptions of
them. Edit it in the app with **✎ TERMS** on a recording's page (it says how
many terms ElevenLabs gets, and which are too long for it: over 5 words or 49
characters), or by hand. **SAVE & RE-CLEAN** there reprocesses the recording
with the new terms.

- Uses `claude-fable-5` at low effort — flip `CLEAN_MODEL` / `CLEAN_EFFORT`
  at the top of `transcribe.py` to taste. Rough cost: $1–3 for a 2-hour
  recording.
- Key: the Anthropic one in ⚙ SETTINGS (or `ANTHROPIC_API_KEY`). Anthropic
  doesn't train on API data; requests are retained per their standard API
  policy.
- The choices live in `~/Reel Recordings/<folder>/<id>/clean.json`: `removed` (hidden word
  indices) and `edits` (span rewrites) — plain JSON, easy to hand-correct.

## API keys and privacy

Transcription uses **ElevenLabs Scribe v2**. To keep your audio out of their
training data:

1. **Create an account** at <https://elevenlabs.io>. Scribe works on paid
   self-serve plans; pick one with enough transcription hours for your use.
2. **Opt out of training *before* transcribing anything.** In your ElevenLabs
   account, open **Terms and Privacy → Data use** and opt out of your data
   being used for training. Per their privacy policy the opt-out only covers
   data submitted *after* you flip it, so do it first.
3. **Give Reel your API key** (ElevenLabs: Developers → API keys): paste it
   into **⚙ SETTINGS** on the library page. Reel checks it with ElevenLabs
   and keeps it in a **`.env` file** next to `transcribe.py` (git ignores it,
   and it is readable only by you). The Anthropic key goes in the same way
   (<https://console.anthropic.com>, API keys). Or set `ELEVENLABS_API_KEY` /
   `ANTHROPIC_API_KEY` yourself, in `.env` (`KEY=value` lines) or in the
   environment, which wins over `.env`. `.env` can also hold any `REEL_*`
   setting.

Additional privacy measures the tool takes automatically:

- After each transcript is downloaded, it is **deleted from ElevenLabs'
  servers** (`DELETE /v1/speech-to-text/transcripts/…`). Flip
  `DELETE_REMOTE_TRANSCRIPT` in `transcribe.py` if you don't want that.
- The web UI binds to `127.0.0.1` only — nothing is served to your network.
- (ElevenLabs' full Zero Retention Mode exists but is enterprise-only.)

**Cost:** roughly $0.22–0.40 per audio hour depending on plan, on top of the
subscription.

## Speaker recognition (the speaker library)

Reel requests `diarize=true` + `use_speaker_library=true` on every
transcription. That second flag matches detected voices against **speaker
profiles registered in your ElevenLabs workspace** — matched speakers come
back with their profile name instead of `speaker_0`, and Reel displays
whatever name arrives.

State of play (as of Aug 2026 — the feature shipped June 2026):

- There is **no public API for managing the library** (confirmed against
  their OpenAPI spec) — profiles are registered through the ElevenLabs web
  app. Their transcription UI lets you rename speakers on a transcript; that
  flow is how profiles get into the workspace library. So: transcribe
  something containing each person once in the ElevenLabs playground, name
  the speakers there, and future Reel transcriptions will recognize them.
- If your plan rejects `use_speaker_library`, Reel automatically retries the
  request without it — transcription always works; you just get anonymous
  `Speaker 1/2` labels.
- Independent of the library, you can **click any speaker label in Reel to
  rename it** for that recording. Names are stored locally in the recording's
  `item.json` and used in the on-screen transcript and text exports.
- Privacy note: enrolled voice profiles live in your ElevenLabs workspace.
  If that ever feels wrong, the fallback plan is AssemblyAI + pyannoteAI
  voiceprints (voiceprints stored locally, nothing retained server-side).

## Why ElevenLabs Scribe v2 (researched Aug 2026)

Requirements: best available accuracy, word-level timestamps (needed for
click-to-seek), a real way to keep data out of training — plus persistent
speaker recognition, which tipped the choice.

- **ElevenLabs Scribe v2** — accuracy within 1–2 points of the best batch
  models, native word timestamps + diarization (up to 32 speakers), audio
  event tags, 90+ languages, and the only major STT API with **built-in
  cross-file speaker recognition** (`use_speaker_library`). Training opt-out
  in account settings. **← chosen.**
- **AssemblyAI Universal-3 Pro** — tops the independent
  [Coval benchmarks](https://www.coval.ai/blog/best-speech-to-text-providers-in-2026-independent-benchmarks-and-how-to-choose/)
  for English conversational audio; no cross-file speaker ID (needs a second
  service, e.g. pyannoteAI). The previous backend of this tool.
- **OpenAI `gpt-4o-transcribe`** / **Google Gemini** — both lack word-level
  timestamps, which kills click-to-seek.
- **Deepgram Nova-3** — clean per-request training opt-out but 2× price for
  it and slightly lower accuracy; no speaker enrollment.

To switch providers, only `transcribe_worker()`/`to_transcript()` in
`transcribe.py` need changing — the UI just reads
`{words: [{text, start, end, speaker, confidence}], utterances: [...]}` with
millisecond timestamps.

## Using the player

- **Click any word** to jump there and play. Click a margin timecode to jump
  to that utterance. **Click a speaker label** to rename that speaker.
- **Space** play/pause · **← / →** skip 5 s · **1.0×** button cycles playback
  speed.
- The transcript follows playback with a red cursor; scroll anywhere to look
  around freely, then hit **Resume following**.
- Words the model heard with low confidence get a subtle dotted underline
  (hover to see the confidence). Sounds like *(laughter)* appear in italics.
- **Copy text / Download .txt** exports the transcript with speaker names and
  timecodes.
- **✂ CUT / SPLIT** edits the recording itself. Pick words in the transcript —
  click one, then shift-click another or drag across the text — and then:
  **✂ CUT IT OUT** (the picked words and the pauses around them, keeping
  0.3 s of pause beside the words that stay), **⇤ CUT ALL BEFORE** /
  **CUT ALL AFTER ⇥** (trim the start or end — a mic left running after the
  conversation), or **⫽ SPLIT BEFORE IT** (two recordings, the second starting
  with the picked words). **▶ PLAY IT** plays the picked words first.
  Nothing is transcribed again: the transcript, clean-up and speaker names are
  kept, divided or renumbered. A split gives each part its share of the audio
  and of every mic's file (a recorder file the split falls in is cut in two,
  the second piece named and time-referenced from where it starts, so it still
  chains), keeps the folder, flags and notes, and has Claude title each part
  (a title you typed is kept, numbered). Cutting off the start or end trims
  every file the same way. A cut in the middle *erases* that stretch in place —
  the mix, the denoised copy and each mic's own file are silenced there — so
  the timeline and the recorders' timing stay as they were (a remix still lines
  up); the player skips it, the transcript shows ✂ where it was, and the
  seek bar a gap. Either way the recording as it was goes to `archive/` (empty
  it if the point was to get rid of something — and a Drive backup made
  before the cut still has it too).
  WAV files keep their format; other audio is re-encoded to AAC. Browser
  recordings (WebM) need ffmpeg (`brew install ffmpeg`).

## Folders and flags

Recordings are filed in folders: new ones land in `general`, and the folder
drop-down on a recording's row in the list (or on its page) moves it to
another one — "new folder…" creates one on the spot (lowercase letters,
digits and dashes, e.g. `interviews`). Combining needs the recordings in one
folder; everything else works across them.

The NOT FOR TRAINING checkbox, on each row of the list and on the recording
page, marks a recording to be kept out of training data, for whoever later
assembles training data from the library. Ticking it brings up ASK FIRST, a
free-text list of people who could still OK it for training; unticking clears
that list. Text boxes save on Enter or on leaving the box. It doesn't move or
hide anything: it is `"no_training": true` and `"training_ask"` in the
recording's `item.json`, next to the transcript, so whoever assembles training
data filters on them. Merging recordings (a late match, combining, or
`--regroup`) keeps the flag if any of them had it and joins their notes.

⚙ SETTINGS on the main page holds the API keys, the
[Google Drive backup](#backing-up-to-google-drive), CLEAR THE MIC AFTER
COPYING for [plugged-in recorders](#several-recorders-one-conversation-multi-mic),
and NOT FOR TRAINING BY DEFAULT for new recordings — recorded, added or from
the inbox. A box ticked
by that setting rather than by hand gets `<default>` as its ASK FIRST until
someone writes the real reason. Recordings rebuilt by a split, a cut or
`--regroup` keep what they had. The settings are `settings.json` next to
`transcribe.py`, ignored by git like `.env`.

## Backing up to Google Drive

Tick **BACK UP TO GOOGLE DRIVE** in ⚙ SETTINGS, and Reel copies every finished
recording into a `Reel backup` folder in your Google Drive.

It goes through **[Google Drive for desktop](https://www.google.com/drive/download/)**:
install it and sign in, and it keeps a folder on your Mac in step with your
Drive (`~/Library/CloudStorage/GoogleDrive-<you>/My Drive`). Reel copies into
that folder and Drive uploads in the background, carrying on after the Mac
has been offline — so Reel needs no Google sign-in or keys of its own. Reel
finds the folder by itself; with several Google accounts signed in to Drive,
pick one under FOLDER. "other folder…" there backs up anywhere else instead —
an external disk, another sync service.

What goes, per recording, into `Reel backup/<folder>/<recording>/`: its
`item.json` (title, speaker names, flags), `transcript.json`, `clean.json`,
the mic levels (`speakers.npz`) and its audio — the mix of a multi-mic
recording, or the file as added. **INCLUDE EACH MIC'S OWN FILES** adds the
recorders' original files (`tracks/`, and `originals/` of a combined
recording), so a restored multi-mic recording can still be re-mixed, combined
or given another mic; they are large, often ten times the rest. The denoised
copy is left out (it is made again from the audio), and so are the inbox and
the archive.

When: shortly after start-up, every 15 minutes while Reel runs, a minute and a
half after a recording is finished (so its title and clean-up go along), and
on **BACK UP NOW**. Only what's new or changed is copied (`.backup.json` in the
data folder remembers what was). **Nothing in the backup is ever deleted by
Reel**: an archived recording stays in it, and a file that changed replaces
its copy (Drive keeps earlier versions for 30 days). The backup isn't
encrypted: it is as private as your Google Drive. In Drive for desktop's
default *Stream files* mode, the copies don't stay on the Mac once they're
uploaded; in *Mirror files* mode they do, which doubles the space your
recordings take.

To restore a recording, copy its folder from `Reel backup/<folder>/` back into
`~/Reel Recordings/<folder>/`; Reel picks it up within seconds.

## Where things live

```
~/Reel Recordings/general/<recording>/    where new recordings land
~/Reel Recordings/<folder>/<recording>/   the folders you file recordings in
~/Reel Recordings/inbox/                  recorder files waiting to be sorted
~/Reel Recordings/archive/                archived recordings and inbox files; recordings
                                          as they were before a cut, split or rebuild
```

Inside a recording's folder:

```
audio.m4a          the auto-mix (or the uploaded file); what plays and what Scribe heard
denoised.m4a       optional cleaner listening copy (DeepFilterNet)
item.json          title, status, duration, language, folder, speaker names,
                   no_training (true: keep it out of training data),
                   training_ask (with no_training: who could still OK it);
                   multi-mic: tracks[] with each recorder file's offset/drift/speaker;
                   cuts: [[start ms, end ms], …] silenced in the middle (✂ CUT IT OUT)
transcript.json    words with ms timestamps, utterances, full text
                   (multi-mic: word.diarized keeps Scribe's guess where the mic disagreed)
clean.json         the AI clean-up, if generated
tracks/            multi-mic: the recorders' original files
originals/         combined: the recordings it was made from, minus their
                   recorder files (for ⧉ UNCOMBINE)
speakers.npz       multi-mic: per-10 ms mic levels used to label words
```

Next to `transcribe.py`, ignored by git: `.env` (your API keys and any
`REEL_*` settings), `settings.json` (⚙ SETTINGS), `keyterms.txt`,
`clean_prompt.txt` and `clean_prompt_default.txt`.

Everything is plain JSON on disk. `REEL_DATA` points the data elsewhere (a
folder that must exist: Reel won't start on a missing one, which may be a disk
that isn't plugged in; nor on one that overlaps the code's folder), `REEL_INBOX` moves just the inbox, `REEL_ARCHIVE` the
archive, `REEL_GROUP` changes the folder new recordings land in.

Deleting a recording (or an inbox file) archives it: recordings move to
`archive/<id>/`, inbox files to `archive/inbox/<name>`, and nothing prunes
the archive — empty it by hand, restore by moving a folder back into the
folder its `item.json` names.

## Command line

```
python3 transcribe.py                       open the library
python3 transcribe.py a.mp3 b.m4a           transcribe these files (handed to the Reel already
                                            running, if there is one)
python3 transcribe.py --group A.WAV B.WAV   one conversation from several recorders' files
python3 transcribe.py --ingest              sort the inbox right away at start-up
python3 transcribe.py --regroup ID ID …     rebuild one conversation from those recordings' files
python3 transcribe.py --split ID 25:40      two recordings of one, divided 25:40 in
python3 transcribe.py --remix ID …          redo alignment, mix and speaker labels (no transcription)
python3 transcribe.py --no-browser          don't open the browser
```

One Reel runs per data folder: starting it again opens the one that's running
(and hands it any audio files you named). `--group`, `--ingest`, `--regroup`,
`--split` and `--remix` run as Reel starts, so stop the running one first
(Ctrl-C in its Terminal window).

## License

MIT — see [LICENSE](LICENSE).
