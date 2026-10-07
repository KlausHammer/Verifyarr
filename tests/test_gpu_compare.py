from verifyarr.gpu_compare import clip_starts, compare


def seg(t, text):
    return {"start": t, "end": t + 1, "text": text}


def test_identical_runs_have_zero_drift():
    a = [seg(1, "Hello there."), seg(5, "How are you?")]
    r = compare(a, a)
    assert r["matched_pct"] == 100.0 and r["start_abs_max_s"] == 0


def test_drift_and_missing_segment():
    a = [seg(1, "Hello there."), seg(5, "How are you?"), seg(9, "Fine.")]
    b = [seg(1.4, "hello there"), seg(9.0, "Fine!")]
    r = compare(a, b)
    assert r["matched"] == 2 and r["start_abs_max_s"] == 0.4


def test_clip_starts_spread_inside_file():
    assert clip_starts(1300, 300, 2) == [333.3, 666.7]


def test_cue_diff(tmp_path):
    from verifyarr.gpu_compare import cue_diff
    a, b = tmp_path / "a.srt", tmp_path / "b.srt"
    a.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n2\n00:00:05,000 --> 00:00:06,000\nBye\n")
    b.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n\n2\n00:00:06,500 --> 00:00:07,500\nBye\n")
    r = cue_diff(a, b)
    assert r["changed_cues"] == 1 and r["max_abs_shift_s"] == 1.5 and not r["identical"]
    assert cue_diff(a, a)["identical"]


def test_rate_gate_levels():
    from verifyarr.subtitles import rate_gate_level
    base = {"slope": 0.0005, "intercept": 0.0, "tilt": 1.0, "rho": 0.5, "gain": 0.12,
            "resid": 0.2, "n": 150, "keep_frac": 0.96, "span": 1500}
    assert rate_gate_level(base) == 1
    assert rate_gate_level({**base, "tilt": 0.7}) == 0
    assert rate_gate_level({**base, "gain": 0.0}) == 0
    assert rate_gate_level({**base, "tilt": 2.0, "rho": 0.8, "gain": 0.3}) == 2
