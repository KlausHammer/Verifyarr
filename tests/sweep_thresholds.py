"""Sensitivity sweep for the anchor/candidate-selection thresholds against real Whisper data --
NOT a full joint optimization. With only 5 real episodes (4 "healthy" + 1 real-world-drift case)
as data, a joint grid search across every threshold would just overfit to this tiny set and
report a fake-precise "optimum". This instead varies ONE parameter at a time around its current
default and scores each candidate value against a small, diagnostic battery of scenarios --
answering "does moving this knob help, hurt, or do nothing on the evidence we actually have",
not "here is THE optimal value".

Reuses tests/sync_verification.py's fixtures/ground truth/shim -- no new API calls, no new
fixtures needed. Standalone script (not test_*.py -- not picked up by `unittest discover`), since
a full sweep takes several minutes.

Run: python3 tests/sweep_thresholds.py
"""

from __future__ import annotations

import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verifyarr import db, pipeline, subtitles
from verifyarr.settings import Config
from verifyarr.subtitles import load_subs

from sync_verification import load_fixture, fixture_paths, build_ground_truth, patch_whisper

HEALTHY_SLUGS = ["S02E01", "S02E06", "S02E10", "S02E15"]
SCRATCH_DIR = Path(tempfile.gettempdir()) / "verifyarr-threshold-sweep"


def _test_config(conn) -> Config:
    cfg = Config.from_db(conn)
    for k, v in dict(
        groq_api_key="shim-no-real-key-needed", stt_provider="groq",
        backup_originals=False, dry_run=False, sync_enabled=True,
        enable_correctness_check=True, anchor_check_enabled=True,
        line_order_enabled=False, line_order_audio_confirm=False,  # not what this sweep tests
    ).items():
        object.__setattr__(cfg, k, v)
    return cfg


def _shift(subs, seconds: float):
    import copy
    out = copy.deepcopy(subs)
    out.shift(s=seconds)
    return out


def _partial_shift(subs, seconds: float, fraction: float = 0.5):
    import copy
    out = copy.deepcopy(subs)
    cutoff = out.events[int(len(out.events) * fraction)].start
    for e in out.events:
        if e.start >= cutoff:
            e.shift(s=seconds)
    return out


def _run(cfg, conn, fixture, subs, tag: str, lang: str = "en"):
    video_path, _ = fixture_paths(fixture)
    tmp = SCRATCH_DIR / f"{tag}.srt"
    subs.save(str(tmp))
    with patch_whisper(fixture):
        row, current_subs = pipeline.sync_pair(video_path, tmp, lang, cfg, {}, SCRATCH_DIR)
        row = pipeline.correctness_and_finish(video_path, tmp, lang, cfg, conn, row, current_subs)
    return row, load_subs(tmp)


def scenario_clean_ok(cfg, conn, fixtures, ground_truths, tag) -> bool:
    """No false SUSPECT escalation on an unmodified, healthy subtitle."""
    ok = True
    for slug in HEALTHY_SLUGS:
        subs = load_subs(fixture_paths(fixtures[slug])[1])
        row, _final = _run(cfg, conn, fixtures[slug], subs, f"{tag}_clean_{slug}")
        if row["correctness_flag"] != "ok" or "Escalated to SUSPECT" in (row.get("note") or ""):
            ok = False
    return ok


def scenario_partial_shift_fixed(cfg, conn, fixtures, ground_truths, tag) -> bool:
    """A genuine two-part discontinuity must be fully corrected in BOTH halves -- the scenario
    that most directly exercises _resolve_ambiguous_sync's candidate ranking."""
    ok = True
    for slug in HEALTHY_SLUGS:
        original = load_subs(fixture_paths(fixtures[slug])[1])
        broken = _partial_shift(original, 18.0)
        row, final = _run(cfg, conn, fixtures[slug], broken, f"{tag}_partial_{slug}")
        gt = ground_truths[slug]
        residuals = gt.residuals_for(final)
        n = len(final.events)
        first = [r for i, r in residuals if i < n // 2]
        second = [r for i, r in residuals if i >= n // 2]
        if not first or not second:
            ok = False
            continue
        if statistics.median(map(abs, first)) >= 2.5 or statistics.median(map(abs, second)) >= 2.5:
            ok = False
    return ok


def scenario_real_world_drift(cfg, conn, fixtures, ground_truths, tag) -> bool:
    """S02E21, unmodified -- the real bug this whole set of thresholds exists to catch/fix."""
    if "S02E21" not in fixtures:
        return True  # not loaded for this sweep run
    gt = ground_truths["S02E21"]
    original = gt.original_subs
    before = gt.summary_for(original)
    row, final = _run(cfg, conn, fixtures["S02E21"], original, f"{tag}_e21")
    after = gt.summary_for(final)
    if before is None or after is None:
        return False
    if "rejected" in row["sync_status"]:
        return False
    if after["max_abs_residual"] >= before["max_abs_residual"] * 0.75:
        return False
    return row["correctness_flag"] == "SUSPECT"


SCENARIOS = [scenario_clean_ok, scenario_partial_shift_fixed, scenario_real_world_drift]


def score_config(cfg, conn, fixtures, ground_truths, tag: str) -> tuple[int, int]:
    passed = sum(1 for fn in SCENARIOS if fn(cfg, conn, fixtures, ground_truths, tag))
    return passed, len(SCENARIOS)


# One-parameter-at-a-time sweep, each candidate list centered on the current shipped default
# (kept in, not just the alternates, so every row is directly comparable to "what ships today").
PARAM_SWEEPS = {
    "subtitles.ANCHOR_SUSPECT_THRESHOLD_S": [1.5, 2.0, 2.5, 3.0, 3.5],
    "subtitles.ANCHOR_MIN_COUNT": [2, 3, 4, 5],
    "pipeline.ANCHOR_PREFER_MARGIN_S": [0.5, 1.0, 1.5, 2.0],
    "pipeline.CONTENT_SCORE_TIE_MARGIN": [0.05, 0.1, 0.15, 0.2],
}


def main():
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    conn = db.connect(SCRATCH_DIR / "scratch.db")
    cfg = _test_config(conn)

    print("Loading fixtures + building ground truth...")
    fixtures = {slug: load_fixture(slug) for slug in HEALTHY_SLUGS + ["S02E21"]}
    ground_truths = {slug: build_ground_truth(f) for slug, f in fixtures.items()}
    print(f"Loaded {len(fixtures)} fixtures.\n")

    for name, candidates in PARAM_SWEEPS.items():
        print(f"=== {name} ===")
        module_name, attr = name.split(".")
        mod = pipeline if module_name == "pipeline" else subtitles
        default = getattr(mod, attr)
        for value in candidates:
            targets = [pipeline, subtitles]
            old = {}
            for t in targets:
                if hasattr(t, attr):
                    old[t] = getattr(t, attr)
                    setattr(t, attr, value)
            try:
                tag = f"{attr}_{value}".replace(".", "p")
                passed, total = score_config(cfg, conn, fixtures, ground_truths, tag)
            finally:
                for t, v in old.items():
                    setattr(t, attr, v)
            marker = " <- current default" if value == default else ""
            print(f"  {attr} = {value:<6} {passed}/{total}{marker}")
        print()


if __name__ == "__main__":
    main()
