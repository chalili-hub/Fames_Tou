import subprocess
import sys
from datetime import datetime
from pathlib import Path
import unittest

from memory.decay import fact_strength, filter_active_facts
from memory.guard_facts import backfill_guard_facts


ROOT = Path(__file__).resolve().parents[1]


class FakeStorage:
    def __init__(self):
        self.rows = [{
            "id": 1,
            "identity_key": "bili:1001",
            "viewer_name": "ExampleViewer",
            "message_text": "上舰：ExampleViewer：开通了舰长",
            "created_at": "2026-06-13T21:10:45",
        }]
        self.facts = []

    def list_guard_messages(self, session_id=None):
        return list(self.rows)

    def has_active_viewer_fact(self, identity_key, category, fact_key):
        return any(
            fact["identity_key"] == identity_key
            and fact["category"] == category
            and fact["fact_key"] == fact_key
            for fact in self.facts
        )

    def add_viewer_fact(self, **fact):
        self.facts.append(fact)


class PublicCoreTests(unittest.TestCase):
    def test_zero_setup_demo(self):
        result = subprocess.run(
            [sys.executable, "main.py"], cwd=ROOT,
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("demo complete", result.stdout)

    def test_decay_filters_stale_fact(self):
        strength = fact_strength(
            "2026-01-01T00:00:00",
            now=datetime(2026, 7, 1),
            half_life_hours=168,
        )
        self.assertLess(strength, 0.1)
        facts = [{"created_at": "2026-01-01T00:00:00", "source": "llm", "category": "preference"}]
        self.assertEqual(
            filter_active_facts(
                facts, now=datetime(2026, 7, 1),
                half_life_hours=168, prune_threshold=0.1,
            ),
            [],
        )

    def test_membership_backfill_is_idempotent(self):
        storage = FakeStorage()
        self.assertEqual(backfill_guard_facts(storage), 1)
        self.assertEqual(backfill_guard_facts(storage), 0)
        self.assertEqual(storage.facts[0]["source"], "manual")


if __name__ == "__main__":
    unittest.main()
