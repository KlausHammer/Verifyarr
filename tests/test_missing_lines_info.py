"""Missing lines inform, they do not trigger the auto action unless asked to."""
import types
import unittest

from verifyarr import pipeline as P, settings


def _cfg(action, act):
    return types.SimpleNamespace(correctness_auto_action=action, act_on_missing_lines=act)


class MissingLinesAction(unittest.TestCase):
    def test_missing_lines_only_inform_by_default(self):
        self.assertEqual(P.suspect_action(P.REASON_MISSING_LINES, _cfg("remediate", False)), "off")

    def test_other_reasons_keep_the_configured_action(self):
        for reason in (P.REASON_WRONG_SUBTITLE, P.REASON_PARTLY_OUT_OF_SYNC, P.REASON_PAST_AUDIO_END,
                       P.REASON_UNRELIABLE_TIMING):
            self.assertEqual(P.suspect_action(reason, _cfg("blacklist", False)), "blacklist")

    def test_switch_on_restores_the_action(self):
        self.assertEqual(P.suspect_action(P.REASON_MISSING_LINES, _cfg("remediate", True)), "remediate")

    def test_default_is_off(self):
        self.assertIs(settings.SETTING_DEFS["correctness.act_on_missing_lines"][2], False)


if __name__ == "__main__":
    unittest.main()
