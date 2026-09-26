"""Small in-process memo that forgets its oldest entry past a size cap."""
from __future__ import annotations


class BoundedMemo(dict):
    """dict with put(): past max_items the oldest entry goes (the webapp lives for weeks)."""

    def __init__(self, max_items: int):
        super().__init__()
        self.max_items = max_items

    def put(self, key, value):
        if key not in self and len(self) >= self.max_items:
            self.pop(next(iter(self)))
        self[key] = value
        return value
