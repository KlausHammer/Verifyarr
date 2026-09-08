"""
verifyarr

Three things in one pipeline:

1. SYNC: runs alass (video as reference) against each subtitle file. Handles
   both slow drift and mid-file jumps (e.g. cut scenes), unlike Bazarr's
   built-in ffsubsync which only computes one global offset. Supports
   .srt/.ass/.ssa/.vtt via pysubs2.

2. CORRECTNESS CHECK: samples a few short audio clips spread across the
   episode, transcribes them with Whisper via Groq's API, and compares the
   words against the subtitle at the same timestamps. Only runs when the
   episode's spoken audio matches the configured language (Whisper is much
   better at English than most other languages, so that's the default). If
   the subtitle itself is in a different language than the audio, its text
   is auto-translated to English first so the language difference alone
   doesn't trigger a false SUSPECT.

3. ACTION: when a file is SUSPECT, it can clean up automatically — never by
   deleting permanently, but by moving the file to quarantine, optionally
   telling Bazarr to blacklist that specific source so it isn't re-downloaded,
   or (remediate) trying to fetch a working replacement itself. Controlled
   per-check via the auto-action setting (off/quarantine/blacklist/remediate).

Used either as a periodic full "sweep" over the library, as a "single" call
from Bazarr's post-processing hook right after a new subtitle is fetched, or
via the built-in webapp (see `verifyarr.web`).
"""

import logging
import os

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("verifyarr")

# Custom levels for Activity's per-file log lines -- both between INFO(20) and WARNING(30) so
# they're filtered out the same way ordinary INFO progress lines are once Settings -> Log's
# level is raised past INFO, but rendered distinctly in the UI (see levelHEADER/levelSUCCESS in
# ActivityDetail.module.css / Settings.module.css):
#   HEADER  — "now starting file X" -- bold, marks where one file's own log lines begin so its
#              name doesn't have to be repeated on every line below it (see jobs._run_sweep).
#   SUCCESS — a file's "fully OK, nothing needed" outcome line (see jobs._outcome_summary) --
#              colored green instead of blending into every other INFO line.
HEADER = 21
SUCCESS = 25
logging.addLevelName(HEADER, "HEADER")
logging.addLevelName(SUCCESS, "SUCCESS")
