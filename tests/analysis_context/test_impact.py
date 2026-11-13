from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cancer_policy_graph.impact import build_outputs


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/policy_impact.json"


def entry_by_name(entries, name):
    for entry in entries:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"missing entry {name}")


class ImpactJointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.manifest, cls.joint, cls.interactions, cls.unrecognized, cls.summary = build_outputs(data)

    def candidate(self, name):
        return entry_by_name(self.joint["candidates"], name)

    def interaction_candidate(self, name):
        return entry_by_name(self.interactions["candidates"], name)

    def resident(self, result, sid):
        for row in result["residents"]:
            if row["subject_id"] == sid:
                return row
        raise AssertionError(f"missing resident {sid}")

    def test_baseline_handcheck(self):
        baseline = self.joint["baseline"]
        self.assertEqual(baseline["status"], "evaluated")
        result = baseline["result"]
        self.assertEqual(result["coverage"]["certain"]["count"], 19)
        self.assertEqual(result["coverage"]["certain"]["residents"][0], "resident-05")
        self.assertEqual(result["coverage"]["certain"]["residents"][-1], "resident-23")
        self.assertEqual(result["payment"]["certain_total"], 1520)
        self.assertEqual(result["payment"]["budget_range"], [1520, 1520])
        self.assertEqual(result["payment"]["by_payer"]["certain"], {"public": 1520})

    def test_support_joint_handcheck(self):
        result = self.candidate("support")["result"]
        # 联合覆盖不去重就会超过名册；这里同一居民只计一次
        self.assertEqual(result["coverage"]["certain"]["count"], 22)
        self.assertEqual(result["coverage"]["possible"]["count"], 24)
        self.assertLessEqual(result["coverage"]["possible"]["count"], self.joint["roster_size"])
        self.assertEqual(result["payment"]["certain_total"], 2000)
        self.assertEqual(result["payment"]["possible_max_total"], 2400)
        self.assertEqual(result["payment"]["budget_range"], [2000, 2400])
        self.assertEqual(result["payment"]["by_payer"]["certain"], {"district": 480, "public": 1520})
        self.assertEqual(result["payment"]["by_payer"]["possible_max"], {"district": 880, "public": 1520})
        # 补充抵扣：resident-06 的 income-support 只补 universal 之后的差额
        row = self.resident(result, "resident-06")
        self.assertEqual(row["certain"]["payments"], {"income-support": 40, "universal": 80})
        self.assertEqual(row["certain"]["total"], 120)

    def test_support_attribution_preserves_interaction(self):
        entry = self.interaction_candidate("support")
        marginals = {item["policy_id"]: item for item in entry["marginals"]}
        universal = marginals["universal"]
        self.assertEqual(universal["marginal"]["covered"], 16)
        self.assertEqual(universal["marginal"]["payment"], 1280)
        self.assertEqual(universal["marginal"]["by_payer"], {"district": -240, "public": 1520})
        support = marginals["income-support"]
        self.assertEqual(support["marginal"]["covered"], 3)
        self.assertEqual(support["marginal"]["payment"], 480)
        self.assertEqual(support["new_beneficiaries"], ["resident-01", "resident-03", "resident-04"])
        (interaction,) = entry["interactions"]
        self.assertEqual(interaction["policies"], ["income-support", "universal"])
        self.assertEqual(interaction["kinds"], ["supplementary_deduction"])
        self.assertEqual(interaction["payment"], -240)
        self.assertEqual(interaction["covered"], -3)
        self.assertEqual(
            {row["subject_id"] for row in interaction["residents"]},
            {"resident-06", "resident-07", "resident-08"},
        )
        self.assertEqual(interaction["evidence"], ["doc-1:section-2", "doc-2:section-3"])
        change = {item["policy"]: item for item in interaction["marginal_change"]}
        self.assertEqual(change["universal"]["in_bundle"]["payment"], 1280)
        self.assertEqual(change["universal"]["without_other"]["payment"], 1520)
        (shift,) = entry["cost_shifts"]
        self.assertEqual(shift["policy_id"], "universal")
        self.assertEqual(shift["from_payers"], [{"amount": 240, "payer": "district"}])
        self.assertEqual(shift["amount"], 240)
        self.assertEqual(shift["payment_neutral_residents"], ["resident-06", "resident-07", "resident-08"])

    def test_expired_and_unknown_attributes_not_certain(self):
        result = self.candidate("support")["result"]
        expired = self.resident(result, "resident-02")
        self.assertEqual(expired["certain"]["payments"], {})
        self.assertEqual(expired["certain"]["total"], 0)
        self.assertFalse(expired["certain"]["covered"])
        self.assertEqual(expired["possible"]["total_max"], 120)
        causes = {cause for item in expired["uncertainty"] for cause in item["causes"]}
        self.assertIn("expired_attribute:income", causes)
        unknown = self.resident(result, "resident-00")
        self.assertEqual(unknown["certain"]["total"], 0)
        self.assertIn("unknown_attribute:income", {cause for item in unknown["uncertainty"] for cause in item["causes"]})
        wideners = {(item["cause"], item["policy_id"]): item for item in result["range_wideners"]}
        self.assertEqual(wideners[("expired_attribute", "income-support")]["residents"], ["resident-02"])
        self.assertEqual(wideners[("expired_attribute", "income-support")]["evidence"], ["doc-2:section-3"])
        self.assertIn("resident-00", wideners[("unknown_attribute", "income-support")]["residents"])
        uncertainty = {
            (row["subject_id"], row["attribute"], row["cause"])
            for row in self.unrecognized["attribute_uncertainty"]
        }
        self.assertIn(("resident-02", "income", "expired_attribute"), uncertainty)

    def test_disputed_policy_never_gives_certain_benefit(self):
        result = self.candidate("pending")["result"]
        self.assertEqual(result["coverage"]["certain"]["count"], 0)
        self.assertEqual(result["payment"]["certain_total"], 0)
        self.assertEqual(result["coverage"]["possible"]["count"], 24)
        self.assertEqual(result["payment"]["possible_max_total"], 1440)
        for row in result["residents"]:
            self.assertEqual(row["certain"]["total"], 0)
            self.assertFalse(row["certain"]["covered"])
        disputed = self.unrecognized["disputed_policies"]
        self.assertEqual([row["policy_id"] for row in disputed], ["unresolved"])
        self.assertEqual(disputed[0]["evidence"], ["doc-4:conflict-1"])
        entry = self.interaction_candidate("pending")
        (marginal,) = entry["marginals"]
        self.assertEqual(marginal["policy_id"], "unresolved")
        self.assertEqual(marginal["status"], "disputed")
        self.assertEqual(marginal["marginal"]["payment"], 0)
        self.assertEqual(marginal["marginal"]["covered"], 0)

    def test_pilot_conflict_exclusivity_and_cap(self):
        result = self.candidate("pilot-conflict")["result"]
        self.assertEqual(result["coverage"]["certain"]["count"], 21)
        self.assertEqual(result["payment"]["certain_total"], 1920)
        self.assertEqual(result["payment"]["budget_range"], [1920, 1920])
        self.assertEqual(result["payment"]["cap_adjustment"]["certain"], 0)
        self.assertEqual(result["payment"]["by_payer"]["certain"], {"pilot": 1200, "public": 720})
        # 互斥：east 居民只拿较高的 east-pilot，不会同时拿 universal
        row5 = self.resident(result, "resident-05")
        self.assertEqual(row5["certain"]["payments"], {"east-pilot": 100})
        row1 = self.resident(result, "resident-01")
        self.assertEqual(row1["certain"]["payments"], {"east-pilot": 100})
        row6 = self.resident(result, "resident-06")
        self.assertEqual(row6["certain"]["payments"], {"universal": 80})
        entry = self.interaction_candidate("pilot-conflict")
        marginals = {item["policy_id"]: item for item in entry["marginals"]}
        # east-pilot 只新增 2 名受益人（east 且未满 50），其余是替换 universal
        self.assertEqual(marginals["east-pilot"]["marginal"]["covered"], 2)
        self.assertEqual(marginals["east-pilot"]["marginal"]["payment"], 400)
        self.assertEqual(marginals["east-pilot"]["new_beneficiaries"], ["resident-01", "resident-03"])
        self.assertEqual(marginals["east-pilot"]["marginal"]["by_payer"], {"pilot": 1200, "public": -800})
        self.assertEqual(marginals["universal"]["marginal"]["covered"], 9)
        self.assertEqual(marginals["universal"]["marginal"]["payment"], 720)
        (interaction,) = entry["interactions"]
        self.assertEqual(interaction["kinds"], ["exclusivity"])
        self.assertEqual(interaction["payment"], -800)
        self.assertEqual(interaction["covered"], -10)
        self.assertEqual(len(interaction["residents"]), 10)
        self.assertTrue(all(row["payment"] == -80 for row in interaction["residents"]))
        (shift,) = entry["cost_shifts"]
        self.assertEqual(shift["policy_id"], "east-pilot")
        self.assertEqual(shift["from_payers"], [{"amount": 800, "payer": "public"}])
        notes = {note["code"] for note in result["notes"]}
        self.assertIn("exclusivity_resolution", notes)

    def test_vs_baseline_and_order_independence(self):
        support = self.interaction_candidate("support")
        self.assertEqual(support["vs_baseline"]["covered_delta"], 3)
        self.assertEqual(support["vs_baseline"]["payment_delta"], 480)
        self.assertEqual(support["vs_baseline"]["new_beneficiaries"], ["resident-01", "resident-03", "resident-04"])
        pilot = self.interaction_candidate("pilot-conflict")
        self.assertEqual(pilot["vs_baseline"]["covered_delta"], 2)
        self.assertEqual(pilot["vs_baseline"]["payment_delta"], 400)
        # pending 是整体替换基准的组合：确定支付从 1520 降为 0（未裁决不计确定待遇）
        pending = self.interaction_candidate("pending")
        self.assertEqual(pending["vs_baseline"]["payment_delta"], -1520)
        self.assertEqual(pending["vs_baseline"]["covered_delta"], -19)
        # 输入顺序不影响结果
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        shuffled = copy.deepcopy(data)
        shuffled["policies"] = list(reversed(shuffled["policies"]))
        shuffled["residents"] = list(reversed(shuffled["residents"]))
        for candidate in shuffled["candidates"]:
            candidate["policies"] = list(reversed(candidate["policies"]))
        _, joint2, interactions2, unrecognized2, _ = build_outputs(shuffled)
        canon = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
        self.assertEqual(canon(self.joint), canon(joint2))
        self.assertEqual(canon(self.interactions), canon(interactions2))
        self.assertEqual(canon(self.unrecognized), canon(unrecognized2))


class ImpactRejectionTests(unittest.TestCase):
    def build_scenario(self):
        policy = {
            "id": "ok",
            "version": 1,
            "status": "adjudicated",
            "effective_from": "2026-01-01",
            "effective_until": "2027-01-01",
            "eligibility": {"gte": ["age", 50]},
            "payment": {"amount": 100, "payer": "public", "cap": 100},
            "evidence": ["doc-0:section-0"],
        }

        def variant(pid, **changes):
            row = copy.deepcopy(policy)
            row["id"] = pid
            row.update(changes)
            return row

        return {
            "schema_version": 1,
            "as_of": "2026-08-01",
            "population_version": "test-roster",
            "funding_version": "test-fund",
            "overall_payment_cap_per_person": 1000,
            "residents": [
                {
                    "subject_id": "r1",
                    "region": "east",
                    "attributes": {"age": {"value": 55, "valid_from": "2026-01-01", "valid_until": "2027-01-01"}},
                }
            ],
            "policies": [
                policy,
                variant("bad-op", eligibility={"between": ["age", 40, 60]}),
                variant("cyc-a", payment={"amount": 10, "payer": "x", "deduct_payments_of": ["cyc-b"]}),
                variant("cyc-b", payment={"amount": 10, "payer": "x", "deduct_payments_of": ["cyc-a"]}),
                variant("gone", status="withdrawn"),
            ],
            "baseline": ["ok"],
            "candidates": [
                {"name": "good", "policies": ["ok"]},
                {"name": "badop", "policies": ["ok", "bad-op"]},
                {"name": "cycle", "policies": ["cyc-a", "cyc-b"]},
                {"name": "ghost", "policies": ["ok", "ghost-policy"]},
                {"name": "withdrawn", "policies": ["gone"]},
            ],
        }

    def test_rejected_candidates_do_not_block_feasible_ones(self):
        _, joint, _, unrecognized, _ = build_outputs(self.build_scenario())
        entries = {entry["name"]: entry for entry in joint["candidates"]}
        self.assertEqual(entries["good"]["status"], "evaluated")
        self.assertEqual(entries["good"]["result"]["payment"]["certain_total"], 100)
        for name in ("badop", "cycle", "ghost", "withdrawn"):
            self.assertEqual(entries[name]["status"], "rejected")
        codes = {
            name: {reason["code"] for reason in entries[name]["reasons"]}
            for name in ("badop", "cycle", "ghost", "withdrawn")
        }
        self.assertIn("unrecognized_policy", codes["badop"])
        self.assertIn("unknown_policy_reference", codes["ghost"])
        self.assertIn("inactive_policy", codes["withdrawn"])
        self.assertIn("unexplainable_combination", codes["cycle"])
        cycle_detail = json.dumps(entries["cycle"]["reasons"], ensure_ascii=False)
        self.assertIn("deduction_cycle", cycle_detail)
        bad = unrecognized["unrecognized_policies"]
        self.assertEqual([row["policy_id"] for row in bad], ["bad-op"])
        self.assertIn("unknown_eligibility_operator:between", bad[0]["reasons"][0])
        self.assertEqual([row["policy_id"] for row in unrecognized["inactive_policies"]], ["gone"])
        self.assertEqual(
            sorted(entry["name"] for entry in unrecognized["rejected_candidates"]),
            ["badop", "cycle", "ghost", "withdrawn"],
        )


class ImpactCapAndDeductionTests(unittest.TestCase):
    """上限与补充抵扣的小案例，数字可手工对账。"""

    def build_scenario(self):
        def policy(pid, amount, payer, deduct=None):
            row = {
                "id": pid,
                "version": 1,
                "status": "adjudicated",
                "effective_from": "2026-01-01",
                "effective_until": "2027-01-01",
                "eligibility": {"gte": ["age", 50]},
                "payment": {"amount": amount, "payer": payer, "cap": amount},
                "evidence": [f"doc:{pid}"],
            }
            if deduct:
                row["payment"]["deduct_payments_of"] = deduct
            return row

        return {
            "schema_version": 1,
            "as_of": "2026-08-01",
            "population_version": "t",
            "funding_version": "t",
            "overall_payment_cap_per_person": 180,
            "residents": [
                {
                    "subject_id": "r1",
                    "region": "east",
                    "attributes": {"age": {"value": 55, "valid_from": "2026-01-01", "valid_until": "2027-01-01"}},
                }
            ],
            "policies": [
                policy("base", 100, "public"),
                policy("topup", 120, "district", deduct=["base"]),
                policy("grant", 150, "district"),
            ],
            "baseline": ["base"],
            "candidates": [
                {"name": "deduct", "policies": ["base", "topup"]},
                {"name": "cap", "policies": ["base", "grant"]},
            ],
        }

    def test_deduction_and_cap_handcheck(self):
        _, joint, interactions, _, _ = build_outputs(self.build_scenario())
        entries = {entry["name"]: entry for entry in joint["candidates"]}
        # 补充抵扣：topup 只补 base 之后的差额 120-100=20
        deduct = entries["deduct"]["result"]
        self.assertEqual(deduct["payment"]["certain_total"], 120)
        (row,) = deduct["residents"]
        self.assertEqual(row["certain"]["payments"], {"base": 100, "topup": 20})
        # 整体上限：100+150=250 超出 180，超出 70 单列，不摊回任何条款或支付方
        cap = entries["cap"]["result"]
        self.assertEqual(cap["payment"]["certain_total"], 180)
        self.assertEqual(cap["payment"]["cap_adjustment"]["certain"], 70)
        self.assertEqual(cap["payment"]["by_payer"]["certain"], {"district": 150, "public": 100})
        (row,) = cap["residents"]
        self.assertEqual(row["certain"]["total_before_cap"], 250)
        self.assertEqual(row["certain"]["cap_adjustment"], 70)
        self.assertEqual(row["certain"]["total"], 180)
        # 上限约束在交互项中可见：180-100-150+0=-70
        cap_entry = entry_by_name(interactions["candidates"], "cap")
        (interaction,) = cap_entry["interactions"]
        self.assertEqual(interaction["payment"], -70)
        self.assertIn("overall_payment_cap", interaction["kinds"])
        self.assertEqual(interaction["residents"], [{"payment": -70, "subject_id": "r1"}])


class ImpactCliTests(unittest.TestCase):
    def test_cli_writes_machine_readable_files(self):
        with tempfile.TemporaryDirectory() as directory:
            outdir = Path(directory) / "out"
            proc = subprocess.run(
                [sys.executable, "-m", "cancer_policy_graph.impact", str(FIXTURE), "--output", str(outdir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for name in ("manifest.json", "joint_results.json", "interactions.json", "unrecognized.json"):
                payload = json.loads((outdir / name).read_text(encoding="utf-8"))
                self.assertEqual(payload["schema_version"], 1)
            manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["versions"]["population_version"], "roster-v1")
            self.assertEqual(manifest["versions"]["funding_version"], "fund-v2")
            self.assertTrue(manifest["versions"]["interpretation_version"])
            self.assertIn("不决定任何个人待遇", manifest["disclaimer"])
            summary = json.loads(proc.stdout)
            self.assertEqual(summary["rejected"], [])
            self.assertIn("support", summary["evaluated"])
            self.assertEqual(summary["baseline"], {"covered_certain": 19, "payment_certain": 1520})


if __name__ == "__main__":
    unittest.main()
