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



class BlockRepairPartsTests(unittest.TestCase):
    def test_parts_from_status_and_direct_write(self):
        from verifyarr.pipeline import _block_repair_parts as parts
        self.assertEqual(parts({"sync_status": "fixed (Δ9.0s, 3 sync block(s))"}), 3)
        self.assertEqual(parts({"sync_status": "fixed (Δ9.0s, 2 anchor region(s))"}), 2)
        # sync_pair's direct multi-block write (no verification possible).
        self.assertEqual(parts({"sync_status": "fixed (Δ12.0s)", "sync_split_blocks": 3}), 3)
        self.assertEqual(parts({"sync_status": "fixed (Δ12.0s)", "sync_split_blocks": 1}), 1)
        # A rate fix replaced the file: alass' old block count no longer describes it.
        self.assertEqual(parts({"sync_status": "fixed (rate 25/24, up to 99.1s)",
                                "sync_split_blocks": 3}), 0)

if __name__ == "__main__":
    unittest.main()
