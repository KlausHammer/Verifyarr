"""Offline tests for the (episode x model) sharding in e2e_matrix_parallel.py.

No subprocesses, no video: only the shard grid construction that the
150-shard / 16-worker run depends on.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_matrix as M
import e2e_matrix_parallel as P


class ShardGridTests(unittest.TestCase):
    def test_full_grid_is_150_unique_pairs(self):
        shards = P.build_shards(list(M.SLUGS), list(M.ALL_MODELS))
        self.assertEqual(len(shards), 10 * 15)
        self.assertEqual(len(set(shards)), 150)

    def test_full_grid_covers_every_combination(self):
        shards = set(P.build_shards(list(M.SLUGS), list(M.ALL_MODELS)))
        for slug in M.SLUGS:
            for model in M.ALL_MODELS:
                self.assertIn((slug, model), shards)

    def test_slug_major_order_keeps_episode_rows_together(self):
        shards = P.build_shards(["A", "B"], ["m1", "m2"])
        self.assertEqual(shards, [("A", "m1"), ("A", "m2"),
                                  ("B", "m1"), ("B", "m2")])

    def test_subset_selection_limits_grid(self):
        shards = P.build_shards(["A"], ["m1", "m2"])
        self.assertEqual(shards, [("A", "m1"), ("A", "m2")])

    def test_shard_index_maps_to_unique_output_file(self):
        shards = P.build_shards(list(M.SLUGS), list(M.ALL_MODELS))
        paths = [f"e2e_matrix_{i}.jsonl" for i in range(len(shards))]
        self.assertEqual(len(set(paths)), 150)


if __name__ == "__main__":
    unittest.main()
