"""Runs the real pipeline on the Known Good data inside a test: no media, no network, only what is in the repo.

KgReplayCase installs the dataset hooks (stored Whisper output, recorded alass answers, no audio) for the
duration of one test class and puts every patched name back afterwards, so the self-contained tests around it
see the untouched modules. Ten episodes are available (tests/known_good/data/episodes.json).
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
KG_DATA = HERE / "known_good" / "data"
MODEL = "tiny.en-greedy-cpu"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "known_good"))


def have_data() -> bool:
    return (KG_DATA / "episodes.json").exists() and (KG_DATA / "transcripts" / MODEL).is_dir()


def episodes() -> list[str]:
    return list(json.loads((KG_DATA / "episodes.json").read_text(encoding="utf-8"))) if have_data() else []


@contextlib.contextmanager
def replay():
    import e2e_matrix as M
    from verifyarr import correctness, line_order, pipeline
    import replay as kg

    mods = [M, correctness, line_order, pipeline]
    before = [dict(vars(m)) for m in mods]
    prior = os.environ.get("VERIFYARR_KG")
    os.environ["VERIFYARR_KG"] = prior if prior == "auto" else "replay"      # auto: maintainers record missing alass answers
    try:
        kg.install(M)
        yield M
    finally:
        for m, snap in zip(mods, before):
            for k in set(vars(m)) - set(snap):
                delattr(m, k)
            vars(m).update(snap)
        if prior is None:
            os.environ.pop("VERIFYARR_KG", None)
        else:
            os.environ["VERIFYARR_KG"] = prior


class KgReplayCase(unittest.TestCase):
    """Base for tests that run the pipeline on Known Good rows."""
    M = None

    @classmethod
    def setUpClass(cls):
        if not have_data():
            raise AssertionError("tests/known_good/data is missing: it is part of the repository")
        cls._stack = contextlib.ExitStack()
        cls.M = cls._stack.enter_context(replay())

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()
