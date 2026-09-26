"""apply_pending_sync is the cleanup every non-correctness caller relies on."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pysubs2

from verifyarr.pipeline import apply_pending_sync


class PrivateKeyTests(unittest.TestCase):
    def test_pre_sync_objects_do_not_leak(self):
        # Generate path: sync_pair -> finish_generated -> write_report (JSON).
        subs = pysubs2.SSAFile()
        row = {"note": "", "sync_status": "fixed (Δ3.0s)",
               "_pre_sync_subs": subs, "_orig_subs": subs}
        apply_pending_sync(Path("/nonexistent.srt"), None, row, reason="test")
        self.assertNotIn("_pre_sync_subs", row)
        self.assertNotIn("_orig_subs", row)
        json.dumps(row)


if __name__ == "__main__":
    unittest.main()
