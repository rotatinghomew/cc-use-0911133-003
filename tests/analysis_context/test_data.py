from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DataContextTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "fixtures/policy_impact.json").read_text(encoding="utf-8"))

    def test_policy_references_and_payment_dependencies(self):
        policies = {row["id"] for row in self.data["policies"]}
        for candidate in self.data["candidates"]:
            self.assertTrue(set(candidate["policies"]) <= policies)
        self.assertTrue(any(row["payment"].get("deduct_payments_of") for row in self.data["policies"]))

    def test_unknowns_and_disputed_interpretation_exist(self):
        self.assertTrue(any(row["attributes"]["income"]["value"] is None for row in self.data["residents"]))
        self.assertTrue(any(row["status"] == "disputed" for row in self.data["policies"]))
        self.assertEqual(len(self.data["residents"]), len({row["subject_id"] for row in self.data["residents"]}))

