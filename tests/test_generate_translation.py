"""Batched LLM translation: the numbered-list contract, batch sizing, and what happens when a
line can't be translated.

This is the highest-stakes validation in the whole feature. Every other failure mode produces
something visibly wrong; a mis-parsed numbered reply produces a subtitle where the words are
fine, the timings are fine, and line 14's text sits on line 13's timestamp for the rest of the
file. Nothing downstream can detect that -- the correctness check is deliberately skipped for
generated subtitles, and it compares bags of words per window anyway, so a permutation inside a
window scores the same as the correct order.

The LLM is stubbed out; no network, no API key, no cost.

Run: python3 -m unittest discover -s tests
"""

import unittest
from unittest import mock

from verifyarr import correctness
from verifyarr import generate as g
from verifyarr.settings import Config, SETTING_DEFS


def _cfg(**overrides) -> Config:
    """A Config built straight from SETTING_DEFS' own defaults, so these tests keep working if a
    field is added -- and fail loudly if one they depend on is removed."""
    values = {key: default for key, (_group, _kind, default) in SETTING_DEFS.items()}
    with mock.patch("verifyarr.settings.get_all_settings", return_value=values):
        cfg = Config.from_db(object())
    return cfg if not overrides else type(cfg)(**{**cfg.__dict__, **overrides})


class ParseNumberedReply(unittest.TestCase):
    def test_well_formed_reply(self):
        self.assertEqual(g._parse_numbered_reply("1. en\n2. to\n3. tre", 3), ["en", "to", "tre"])

    def test_a_wrapped_line_is_rejoined_not_truncated(self):
        # A model that wrapped one translation across two physical lines used to pass the count
        # check (the continuation simply didn't match the numbered pattern) while silently
        # losing half of that subtitle line's text.
        self.assertEqual(g._parse_numbered_reply("1. first half\n   second half\n2. to", 2),
                         ["first half second half", "to"])

    def test_repeated_number_is_rejected(self):
        self.assertIsNone(g._parse_numbered_reply("1. en\n1. igen\n2. to", 2))

    def test_missing_or_renumbered_lines_are_rejected(self):
        self.assertIsNone(g._parse_numbered_reply("1. en\n3. tre", 2))
        self.assertIsNone(g._parse_numbered_reply("2. to\n3. tre", 2))

    def test_wrong_format_is_rejected(self):
        self.assertIsNone(g._parse_numbered_reply("1) en\n2) to", 2))

    def test_extra_lines_are_rejected(self):
        self.assertIsNone(g._parse_numbered_reply("1. en\n2. to\n3. bonus", 2))

    def test_preamble_without_numbers_does_not_corrupt_the_first_line(self):
        self.assertIsNone(g._parse_numbered_reply("Sure! Here you go:", 1))

    def test_empty_or_missing_reply(self):
        self.assertIsNone(g._parse_numbered_reply(None, 1))
        self.assertIsNone(g._parse_numbered_reply("", 1))


class IterBatches(unittest.TestCase):
    def test_batches_respect_the_line_limit(self):
        segments = [{"text": "kort"} for _ in range(45)]
        self.assertEqual([len(b) for b in g._iter_batches(segments, 20, 8000)], [20, 20, 5])

    def test_batches_respect_the_character_limit(self):
        # REGRESSION: only the line count was bounded, so a large batch_size produced a request
        # that translate_text then TRUNCATED -- dropping the tail of the numbered list, failing
        # the count check, and falling back to one expensive call per line for no reason.
        segments = [{"text": "x" * 500} for _ in range(50)]
        for batch in g._iter_batches(segments, 100, 8000):
            self.assertLessEqual(sum(len(s["text"]) + g._NUMBER_PREFIX_COST for s in batch), 8000)

    def test_every_segment_appears_exactly_once_in_order(self):
        segments = [{"text": f"line {i}"} for i in range(37)]
        flattened = [s for batch in g._iter_batches(segments, 7, 8000) for s in batch]
        self.assertEqual(flattened, segments)

    def test_a_single_oversized_line_still_gets_its_own_batch(self):
        segments = [{"text": "y" * 20000}, {"text": "short"}]
        self.assertEqual([len(b) for b in g._iter_batches(segments, 20, 8000)], [1, 1])


class TranslateSegments(unittest.TestCase):
    SEGMENTS = [{"start": float(i), "end": i + 1.0, "text": f"line {i}"} for i in range(5)]

    def test_timestamps_are_never_touched(self):
        with mock.patch.object(correctness, "translate_text",
                               side_effect=lambda text, *a, **k: "\n".join(
                                   f"{i + 1}. oversat {i}" for i in range(len(text.splitlines())))):
            out = g.translate_segments(_cfg(), self.SEGMENTS, "da")
        self.assertEqual([(s["start"], s["end"]) for s in out],
                         [(s["start"], s["end"]) for s in self.SEGMENTS])
        self.assertEqual([s["text"] for s in out], [f"oversat {i}" for i in range(5)])

    def test_a_malformed_batch_falls_back_to_one_line_at_a_time(self):
        calls = []

        def fake(text, *args, **kwargs):
            calls.append(text)
            if "\n" in text:       # the batched numbered list
                return "1. merged everything into one line"
            return f"[{text}]"     # the per-line retry

        with mock.patch.object(correctness, "translate_text", side_effect=fake):
            out = g.translate_segments(_cfg(), self.SEGMENTS, "da")
        self.assertEqual([s["text"] for s in out], [f"[line {i}]" for i in range(5)])
        self.assertEqual(len(calls), 1 + len(self.SEGMENTS))

    def test_an_untranslatable_line_aborts_instead_of_leaving_source_text(self):
        # REGRESSION: a failed line used to fall back to its UNTRANSLATED source text, producing
        # a Danish subtitle with English lines scattered through it -- and nothing downstream
        # could ever catch that, since the correctness check is skipped for generated files and
        # would have scored those English lines against the English audio as a perfect match.
        with mock.patch.object(correctness, "translate_text", return_value=None):
            with self.assertRaises(g.GenerationError) as caught:
                g.translate_segments(_cfg(), self.SEGMENTS, "da")
        self.assertIn("could not be translated", str(caught.exception))

    def test_an_empty_translation_counts_as_a_failure(self):
        def fake(text, *args, **kwargs):
            return "" if "\n" not in text else "1. ok\n2. ok\n3. \n4. ok\n5. ok"

        with mock.patch.object(correctness, "translate_text", side_effect=fake):
            with self.assertRaises(g.GenerationError):
                g.translate_segments(_cfg(), self.SEGMENTS, "da")

    def test_language_name_is_used_in_the_prompt_not_the_code(self):
        seen = {}

        def fake(text, target, *args, **kwargs):
            seen["target"] = target
            seen["system"] = kwargs.get("system_prompt") or ""
            return "\n".join(f"{i + 1}. x" for i in range(len(text.splitlines())))

        with mock.patch.object(correctness, "translate_text", side_effect=fake):
            g.translate_segments(_cfg(), self.SEGMENTS, "da")
        self.assertEqual(seen["target"], "Danish")
        self.assertIn("Danish", seen["system"])


if __name__ == "__main__":
    unittest.main()
