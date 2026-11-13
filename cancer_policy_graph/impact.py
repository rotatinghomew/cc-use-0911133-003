from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping

from cancer_policy_graph.runtime import DomainError, fingerprint, normalize_text, utc_now

SCHEMA_VERSION = 1
STATUS_CERTAIN = "adjudicated"
STATUS_POSSIBLE = frozenset({"disputed"})
STATUS_INACTIVE = frozenset({"withdrawn"})
KNOWN_STATUSES = frozenset({STATUS_CERTAIN}) | STATUS_POSSIBLE | STATUS_INACTIVE
OPERATORS = frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "in"})
MAX_MAYBE_POLICIES = 12
MAX_EXCLUSIVE_POLICIES = 16
OUTPUT_FILES = ("manifest.json", "joint_results.json", "interactions.json", "unrecognized.json")

DISCLAIMER = (
    "批量筛查补助影响分析：不决定任何个人待遇，不构成诊断。"
    "确定覆盖仅基于已裁决条款与有效属性；可能覆盖与预算上限另含未裁决条款与未知或过期属性。"
)

ASSUMPTIONS = [
    "attribute_window: 属性有效期为左闭右开区间；as_of 不在区间内、值为空或属性缺失时，该属性按未知处理",
    "unknown_eligibility: 资格未知只进入可能覆盖与预算上限，不进入确定覆盖",
    "disputed_policy: 未裁决条款只进入可能覆盖与预算上限，确定结果不含未裁决条款",
    "inactive_policy: 已撤回或 as_of 不在有效期内的条款不参与评估；包含此类条款的组合整体拒绝并记录原因",
    "exclusivity: 互斥条款按居民上限后所得最高的可行组合取舍，并列时按条款 id 字典序；这是分析假设，不是对个人待遇的决定",
    "deduction: deduct_payments_of 按声明关系从本条款支付中减去对方实际支付；关系构成有向无环图，成环的组合拒绝",
    "overall_cap: overall_payment_cap_per_person 作用于个人合计；超出部分记为 cap_adjustment，不按任何顺序摊回条款或支付方",
    "budget_range: 下界为确定支付合计，上界为可能最大支付合计；增加条款不会减少个人合计，故下界即确定值",
    "attribution: 边际作用与交互项基于确定结果计算，与输入遍历顺序无关",
]

METHOD = {
    "name": "leave-one-out marginal attribution with pairwise interaction terms",
    "metrics": ["covered_certain", "payment_certain", "payment_certain_by_payer"],
    "interaction_definition": "I(p,q) = J(B) - J(B-{p}) - J(B-{q}) + J(B-{p,q})，等于移除 q 后 p 的边际变化",
    "by_payer_note": "by_payer 为 overall cap 前口径；payment 为 cap 后合计，两者之差即 cap_adjustment 变化",
    "uncertainty_note": "边际与交互仅基于确定结果（已裁决条款+有效属性）；可能覆盖与预算区间见 joint_results.json",
    "order_independence": "支付由声明的业务关系（抵扣图、互斥、上限）决定，与输入遍历顺序无关",
}


class CandidateError(DomainError):
    """候选组合无法解释：拒绝该组合，但不阻断其他组合。"""


def as_number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainError(f"{field} 必须是数值")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainError(f"{field} 必须是有限数值")
        if value.is_integer():
            return int(value)
    return value


def parse_date(value: Any, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise DomainError(f"{field} 不是有效日期: {value!r}") from None


def window_contains(day: date, start: date | None, end: date | None) -> bool:
    """左闭右开区间。"""
    if start is not None and day < start:
        return False
    if end is not None and day >= end:
        return False
    return True


def validate_eligibility(expr: Any, path: str = "eligibility") -> None:
    if not isinstance(expr, Mapping) or not expr:
        raise CandidateError(f"{path} 必须是非空对象")
    for op, args in expr.items():
        if op in ("and", "or"):
            if not isinstance(args, list) or not args:
                raise CandidateError(f"{path}.{op} 必须是非空数组")
            for index, sub in enumerate(args):
                validate_eligibility(sub, f"{path}.{op}[{index}]")
        elif op in OPERATORS:
            if not isinstance(args, (list, tuple)) or len(args) != 2:
                raise CandidateError(f"{path}.{op} 参数必须是 [字段, 阈值]")
            if not isinstance(args[0], str) or not args[0]:
                raise CandidateError(f"{path}.{op} 字段名必须是非空字符串")
            if op == "in" and not isinstance(args[1], list):
                raise CandidateError(f"{path}.in 阈值必须是数组")
        else:
            raise CandidateError(f"unknown_eligibility_operator:{op}")


def compare_values(op: str, value: Any, target: Any) -> bool | None:
    """返回 True/False；类型不可比时返回 None（按未知处理）。"""
    if op == "eq":
        return value == target
    if op == "neq":
        return value != target
    if op == "in":
        return value in target
    if isinstance(value, bool) or isinstance(target, bool):
        return None
    if not isinstance(value, (int, float)) or not isinstance(target, (int, float)):
        return None
    if op == "gt":
        return value > target
    if op == "gte":
        return value >= target
    if op == "lt":
        return value < target
    if op == "lte":
        return value <= target
    return None


def eval_eligibility(expr: Mapping[str, Any], lookup) -> tuple[bool | None, list[str]]:
    """三值求值：True/False/None(未知)。返回 (状态, 导致未知的原因列表)。"""
    causes: set[str] = set()

    def walk(node: Mapping[str, Any]) -> bool | None:
        states: list[bool | None] = []
        for op, args in node.items():
            if op == "and":
                sub = [walk(item) for item in args]
                states.append(False if False in sub else (None if None in sub else True))
            elif op == "or":
                sub = [walk(item) for item in args]
                states.append(True if True in sub else (None if None in sub else False))
            else:
                field, target = args[0], args[1]
                value, cause = lookup(field)
                if cause is not None:
                    causes.add(cause)
                    states.append(None)
                else:
                    outcome = compare_values(op, value, target)
                    if outcome is None:
                        causes.add(f"uncomparable_attribute:{field}")
                    states.append(outcome)
        return False if False in states else (None if None in states else True)

    state = walk(expr)
    return state, sorted(causes)


def normalize_residents(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise DomainError("residents 必须是非空数组")
    residents = []
    seen = set()
    for index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            raise DomainError(f"residents[{index}] 必须是对象")
        sid = normalize_text(row.get("subject_id"), "subject_id", 96)
        if not sid:
            raise DomainError(f"residents[{index}] 缺少 subject_id")
        if sid in seen:
            raise DomainError(f"subject_id 重复: {sid}")
        seen.add(sid)
        attributes = row.get("attributes", {})
        if not isinstance(attributes, Mapping):
            raise DomainError(f"{sid} attributes 必须是对象")
        residents.append({"subject_id": sid, "region": row.get("region"), "attributes": attributes})
    return residents


def normalize_policy(raw: Any, as_of: date) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CandidateError("policy 必须是对象")
    pid = normalize_text(raw.get("id"), "policy.id", 96)
    if not pid:
        raise CandidateError("policy 缺少 id")
    status = normalize_text(raw.get("status"), f"{pid}.status", 40)
    if status not in KNOWN_STATUSES:
        raise CandidateError(f"unknown_status:{status or '<missing>'}")
    eligibility = raw.get("eligibility")
    validate_eligibility(eligibility)
    payment = raw.get("payment")
    if not isinstance(payment, Mapping):
        raise CandidateError(f"{pid}.payment 必须是对象")
    amount = as_number(payment.get("amount"), f"{pid}.payment.amount")
    if amount < 0:
        raise CandidateError(f"{pid}.payment.amount 不能为负")
    cap = payment.get("cap")
    cap = as_number(cap, f"{pid}.payment.cap") if cap is not None else None
    if cap is not None and cap < 0:
        raise CandidateError(f"{pid}.payment.cap 不能为负")
    payer = normalize_text(payment.get("payer"), f"{pid}.payment.payer", 96) or "unknown"

    def text_list(value: Any, field: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise CandidateError(f"{pid}.{field} 必须是数组")
        items = {normalize_text(item, f"{pid}.{field}", 96) for item in value}
        items.discard("")
        return sorted(items)

    deduct = text_list(payment.get("deduct_payments_of"), "payment.deduct_payments_of")
    exclusive = text_list(raw.get("exclusive_with"), "exclusive_with")
    evidence = text_list(raw.get("evidence"), "evidence")
    eff_from = parse_date(raw.get("effective_from"), f"{pid}.effective_from")
    eff_until = parse_date(raw.get("effective_until"), f"{pid}.effective_until")
    inactive_reasons = []
    if status in STATUS_INACTIVE:
        inactive_reasons.append(f"status:{status}")
    if not window_contains(as_of, eff_from, eff_until):
        inactive_reasons.append("not_in_force_at_as_of")
    return {
        "id": pid,
        "version": as_number(raw.get("version", 1), f"{pid}.version"),
        "status": status,
        "eligibility": eligibility,
        "amount": amount,
        "cap": cap,
        "payer": payer,
        "deduct_payments_of": deduct,
        "exclusive_with": exclusive,
        "evidence": evidence,
        "effective_from": eff_from.isoformat() if eff_from else None,
        "effective_until": eff_until.isoformat() if eff_until else None,
        "active": not inactive_reasons,
        "inactive_reasons": inactive_reasons,
    }


def normalize_policies(raw_list: Any, as_of: date) -> tuple[dict[str, dict], list[dict], list[dict]]:
    if not isinstance(raw_list, list):
        raise DomainError("policies 必须是数组")
    policies: dict[str, dict] = {}
    unrecognized = []
    for index, raw in enumerate(raw_list):
        try:
            policy = normalize_policy(raw, as_of)
        except DomainError as exc:
            pid = raw.get("id") if isinstance(raw, Mapping) else None
            label = pid if isinstance(pid, str) and pid else f"index:{index}"
            unrecognized.append({"policy_id": label, "reasons": [str(exc)]})
            continue
        pid = policy["id"]
        if pid in policies:
            raise DomainError(f"policy id 重复: {pid}")
        policies[pid] = policy
    inactive = [
        {"policy_id": policy["id"], "reasons": list(policy["inactive_reasons"])}
        for policy in policies.values()
        if not policy["active"]
    ]
    return policies, unrecognized, inactive


def topo_order(ids, policies: Mapping[str, dict]) -> list[str]:
    """按声明的抵扣关系拓扑排序（被抵扣方先算）；成环则组合无法解释。"""
    ids = sorted(ids)
    remaining = {pid: {d for d in policies[pid]["deduct_payments_of"] if d in ids} for pid in ids}
    order: list[str] = []
    ready = sorted(pid for pid in ids if not remaining[pid])
    while ready:
        node = ready.pop(0)
        order.append(node)
        for pid in ids:
            if node in remaining[pid]:
                remaining[pid].discard(node)
                if not remaining[pid] and pid not in order and pid not in ready:
                    ready.append(pid)
        ready.sort()
    if len(order) != len(ids):
        cycle = sorted(pid for pid in ids if remaining[pid])
        raise CandidateError("deduction_cycle:" + ",".join(cycle))
    return order


def raw_payments(ids, policies: Mapping[str, dict]) -> dict[str, Any]:
    amounts: dict[str, Any] = {}
    for pid in topo_order(ids, policies):
        policy = policies[pid]
        base = policy["amount"] if policy["cap"] is None else min(policy["amount"], policy["cap"])
        deduction = sum(amounts[d] for d in policy["deduct_payments_of"] if d in amounts)
        amounts[pid] = max(0, base - deduction)
    return amounts


def best_payment(ids, policies: Mapping[str, dict], overall_cap: Any) -> dict[str, Any]:
    """互斥按居民上限后所得最高取舍（并列取 id 字典序最小），与输入顺序无关。"""
    ids = frozenset(ids)
    if not ids:
        return {"amounts": {}, "total_before_cap": 0, "cap_adjustment": 0, "total": 0, "by_payer": {}, "exclusive_choice": False}
    pairs = set()
    for pid in ids:
        for other in policies[pid]["exclusive_with"]:
            if other in ids:
                pairs.add(frozenset((pid, other)))
    constrained = sorted({pid for pair in pairs for pid in pair})
    if len(constrained) > MAX_EXCLUSIVE_POLICIES:
        raise CandidateError("exclusivity_group_too_large")
    free = frozenset(ids) - frozenset(constrained)
    best = None
    for size in range(len(constrained) + 1):
        for sub in combinations(constrained, size):
            sub_set = frozenset(sub)
            if any(pair <= sub_set for pair in pairs):
                continue
            candidate = free | sub_set
            amounts = raw_payments(candidate, policies)
            before = sum(amounts.values())
            adjustment = max(0, before - overall_cap) if overall_cap is not None else 0
            total = before - adjustment
            sorted_ids = tuple(sorted(candidate))
            if best is None or total > best[0] or (total == best[0] and sorted_ids < best[1]):
                best = (total, sorted_ids, amounts, before, adjustment)
    total, _, amounts, before, adjustment = best
    by_payer: dict[str, Any] = {}
    for pid, amount in amounts.items():
        payer = policies[pid]["payer"]
        by_payer[payer] = by_payer.get(payer, 0) + amount
    return {
        "amounts": dict(sorted(amounts.items())),
        "total_before_cap": before,
        "cap_adjustment": adjustment,
        "total": total,
        "by_payer": dict(sorted(by_payer.items())),
        "exclusive_choice": bool(constrained),
    }


def resident_result(
    resident: Mapping[str, Any],
    bundle_ids: list[str],
    policies: Mapping[str, dict],
    as_of: date,
    overall_cap: Any,
    attr_issues: dict,
) -> dict[str, Any]:
    sid = resident["subject_id"]
    attributes = resident["attributes"]

    def lookup(field: str):
        if field == "region":
            value = resident.get("region")
            if value in (None, ""):
                return None, "missing_attribute:region"
            return value, None
        entry = attributes.get(field)
        if not isinstance(entry, Mapping):
            return None, f"missing_attribute:{field}"
        try:
            start = parse_date(entry.get("valid_from"), f"{sid}.{field}.valid_from")
            end = parse_date(entry.get("valid_until"), f"{sid}.{field}.valid_until")
        except DomainError as exc:
            attr_issues[(sid, field)] = {"subject_id": sid, "attribute": field, "detail": str(exc)}
            return None, f"malformed_attribute:{field}"
        if start is not None and end is not None and start > end:
            attr_issues[(sid, field)] = {"subject_id": sid, "attribute": field, "detail": "valid_from 晚于 valid_until"}
            return None, f"malformed_attribute:{field}"
        if not window_contains(as_of, start, end):
            return None, f"expired_attribute:{field}"
        value = entry.get("value")
        if value is None:
            return None, f"unknown_attribute:{field}"
        return value, None

    certain_ids: list[str] = []
    maybe_ids: list[str] = []
    uncertainty = []
    for pid in bundle_ids:
        policy = policies[pid]
        state, causes = eval_eligibility(policy["eligibility"], lookup)
        if policy["status"] == STATUS_CERTAIN:
            if state is True:
                certain_ids.append(pid)
            elif state is None:
                maybe_ids.append(pid)
                uncertainty.append({"policy_id": pid, "causes": causes, "evidence": policy["evidence"]})
        elif policy["status"] in STATUS_POSSIBLE and state is not False:
            maybe_ids.append(pid)
            uncertainty.append({"policy_id": pid, "causes": sorted(set(causes) | {"disputed_policy"}), "evidence": policy["evidence"]})
    certain_payment = best_payment(certain_ids, policies, overall_cap)
    maybe_ids = sorted(set(maybe_ids))
    approximate = len(maybe_ids) > MAX_MAYBE_POLICIES
    if approximate:
        subsets = [frozenset(), frozenset(maybe_ids)]
    else:
        subsets = [frozenset(combo) for size in range(len(maybe_ids) + 1) for combo in combinations(maybe_ids, size)]
    possible_payment = certain_payment
    possible_extra: list[str] = []
    for sub in subsets:
        payment = best_payment(frozenset(certain_ids) | sub, policies, overall_cap)
        if payment["total"] > possible_payment["total"]:
            possible_payment = payment
            possible_extra = sorted(sub)
    return {
        "certain_payment": certain_payment,
        "possible_payment": possible_payment,
        "possible_extra_policies": possible_extra,
        "uncertainty": uncertainty,
        "approximate": approximate,
    }


def evaluate_bundle(
    bundle_ids,
    policies: Mapping[str, dict],
    residents: list[dict],
    as_of: date,
    overall_cap: Any,
    attr_issues: dict,
    attr_uncertainty: dict,
) -> tuple[dict, dict]:
    """把同一组合真正应用到同一人群快照上，返回 (对外结构, 归因内部结构)。"""
    bundle_ids = frozenset(bundle_ids)
    if bundle_ids:
        topo_order(bundle_ids, policies)
    sorted_ids = sorted(bundle_ids)
    notes = []
    for pid in sorted_ids:
        for ref in policies[pid]["deduct_payments_of"]:
            if ref not in bundle_ids:
                notes.append({
                    "code": "dormant_deduction",
                    "policy_id": pid,
                    "references": ref,
                    "detail": "抵扣对象不在组合内，本次评估该抵扣不发生",
                })
    entries = []
    covered_certain: set[str] = set()
    covered_possible: set[str] = set()
    payment_certain = 0
    payment_possible = 0
    cap_certain = 0
    cap_possible = 0
    payer_certain: dict[str, Any] = {}
    payer_possible: dict[str, Any] = {}
    resident_totals: dict[str, Any] = {}
    resident_payers: dict[str, dict] = {}
    wideners: dict[tuple, dict] = {}
    exclusivity_applied = False
    approximate = False
    for resident in residents:
        sid = resident["subject_id"]
        result = resident_result(resident, sorted_ids, policies, as_of, overall_cap, attr_issues)
        certain = result["certain_payment"]
        possible = result["possible_payment"]
        exclusivity_applied = exclusivity_applied or certain["exclusive_choice"] or possible["exclusive_choice"]
        approximate = approximate or result["approximate"]
        for item in result["uncertainty"]:
            for cause in item["causes"]:
                prefix, _, attribute = cause.partition(":")
                attr_uncertainty[(sid, item["policy_id"], cause)] = {
                    "subject_id": sid,
                    "policy_id": item["policy_id"],
                    "attribute": attribute or None,
                    "cause": prefix,
                }
                key = (prefix, item["policy_id"])
                slot = wideners.setdefault(key, {
                    "cause": prefix,
                    "policy_id": item["policy_id"],
                    "evidence": item["evidence"],
                    "residents": set(),
                })
                slot["residents"].add(sid)
        if certain["total"] > 0:
            covered_certain.add(sid)
        if possible["total"] > 0:
            covered_possible.add(sid)
        payment_certain += certain["total"]
        payment_possible += possible["total"]
        cap_certain += certain["cap_adjustment"]
        cap_possible += possible["cap_adjustment"]
        for payer, amount in certain["by_payer"].items():
            payer_certain[payer] = payer_certain.get(payer, 0) + amount
        for payer, amount in possible["by_payer"].items():
            payer_possible[payer] = payer_possible.get(payer, 0) + amount
        resident_totals[sid] = certain["total"]
        resident_payers[sid] = certain["by_payer"]
        if certain["total"] > 0 or possible["total"] > 0 or result["uncertainty"]:
            entries.append({
                "subject_id": sid,
                "certain": {
                    "covered": certain["total"] > 0,
                    "payments": certain["amounts"],
                    "total": certain["total"],
                    "total_before_cap": certain["total_before_cap"],
                    "cap_adjustment": certain["cap_adjustment"],
                },
                "possible": {
                    "covered": possible["total"] > 0,
                    "total_max": possible["total"],
                    "cap_adjustment_max": possible["cap_adjustment"],
                    "extra_policies": result["possible_extra_policies"],
                },
                "uncertainty": result["uncertainty"],
            })
    if exclusivity_applied:
        notes.append({
            "code": "exclusivity_resolution",
            "detail": "组合内含互斥条款，已按居民上限后所得最高取舍（分析假设，见 manifest assumptions）",
        })
    if approximate:
        notes.append({
            "code": "possible_range_approximate",
            "detail": "不确定条款过多，可能区间按 空集/全集 近似",
        })
    entries.sort(key=lambda row: row["subject_id"])
    public = {
        "policies": sorted_ids,
        "coverage": {
            "certain": {"count": len(covered_certain), "residents": sorted(covered_certain)},
            "possible": {"count": len(covered_possible), "residents": sorted(covered_possible)},
        },
        "payment": {
            "certain_total": payment_certain,
            "possible_max_total": payment_possible,
            "budget_range": [payment_certain, payment_possible],
            "by_payer": {
                "certain": dict(sorted(payer_certain.items())),
                "possible_max": dict(sorted(payer_possible.items())),
            },
            "cap_adjustment": {"certain": cap_certain, "possible_max": cap_possible},
        },
        "policy_details": {
            pid: {
                "payer": policies[pid]["payer"],
                "amount": policies[pid]["amount"],
                "cap": policies[pid]["cap"],
                "status": policies[pid]["status"],
                "evidence": policies[pid]["evidence"],
                "deduct_payments_of": policies[pid]["deduct_payments_of"],
                "exclusive_with": policies[pid]["exclusive_with"],
            }
            for pid in sorted_ids
        },
        "residents": entries,
        "range_wideners": [
            {**slot, "residents": sorted(slot["residents"])}
            for _, slot in sorted(wideners.items())
        ],
        "notes": notes,
    }
    internal = {
        "covered_set": covered_certain,
        "payment_total": payment_certain,
        "by_payer": payer_certain,
        "cap_adjustment_total": cap_certain,
        "resident_totals": resident_totals,
        "resident_payers": resident_payers,
    }
    return public, internal


def run_bundle(name: str, raw_ids: Any, policies: Mapping[str, dict], unrecognized_ids: set, get_bundle) -> tuple[dict, dict | None]:
    reasons = []
    ids = []
    if not isinstance(raw_ids, list):
        raise DomainError(f"{name} policies 必须是数组")
    for raw in raw_ids:
        pid = normalize_text(raw, "policy_ref", 96)
        if not pid:
            reasons.append({"code": "empty_policy_reference"})
            continue
        ids.append(pid)
    if len(ids) != len(set(ids)):
        reasons.append({"code": "duplicate_policy_reference"})
    for pid in sorted(set(ids)):
        if pid in unrecognized_ids:
            reasons.append({"code": "unrecognized_policy", "policy_id": pid})
        elif pid not in policies:
            reasons.append({"code": "unknown_policy_reference", "policy_id": pid})
        elif not policies[pid]["active"]:
            reasons.append({"code": "inactive_policy", "policy_id": pid, "detail": list(policies[pid]["inactive_reasons"])})
    sorted_ids = sorted(set(ids))
    if reasons:
        return {"name": name, "policies": sorted_ids, "status": "rejected", "reasons": reasons}, None
    try:
        public, internal = get_bundle(sorted_ids)
    except CandidateError as exc:
        reasons = [{"code": "unexplainable_combination", "detail": str(exc)}]
        return {"name": name, "policies": sorted_ids, "status": "rejected", "reasons": reasons}, None
    return {"name": name, "policies": sorted_ids, "status": "evaluated", "result": public}, internal


def payer_delta(current: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    return {payer: current.get(payer, 0) - baseline.get(payer, 0) for payer in sorted(set(current) | set(baseline))}


def marginal_metric(full: Mapping[str, Any], sub: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "covered": len(full["covered_set"]) - len(sub["covered_set"]),
        "payment": full["payment_total"] - sub["payment_total"],
        "by_payer": payer_delta(full["by_payer"], sub["by_payer"]),
        "cap_adjustment": full["cap_adjustment_total"] - sub["cap_adjustment_total"],
    }


def interactions_entry(ids, policies: Mapping[str, dict], get_bundle) -> dict[str, Any]:
    """leave-one-out 边际 + 成对交互项；全部基于确定结果，可回查居民键与条款证据。"""
    ids = frozenset(ids)
    _, full = get_bundle(ids)
    marginals = []
    for pid in sorted(ids):
        _, sub = get_bundle(ids - {pid})
        marginals.append({
            "policy_id": pid,
            "status": policies[pid]["status"],
            "evidence": policies[pid]["evidence"],
            "marginal": marginal_metric(full, sub),
            "new_beneficiaries": sorted(full["covered_set"] - sub["covered_set"]),
            "without": {
                "policies": sorted(ids - {pid}),
                "covered_certain": len(sub["covered_set"]),
                "payment_certain": sub["payment_total"],
            },
        })
    interactions = []
    for first, second in combinations(sorted(ids), 2):
        _, without_first = get_bundle(ids - {first})
        _, without_second = get_bundle(ids - {second})
        _, without_both = get_bundle(ids - {first, second})
        covered = (
            len(full["covered_set"]) - len(without_first["covered_set"])
            - len(without_second["covered_set"]) + len(without_both["covered_set"])
        )
        payment = (
            full["payment_total"] - without_first["payment_total"]
            - without_second["payment_total"] + without_both["payment_total"]
        )
        residents_effect = []
        for sid in sorted(set(full["resident_totals"]) | set(without_first["resident_totals"])
                          | set(without_second["resident_totals"]) | set(without_both["resident_totals"])):
            delta = (
                full["resident_totals"].get(sid, 0) - without_first["resident_totals"].get(sid, 0)
                - without_second["resident_totals"].get(sid, 0) + without_both["resident_totals"].get(sid, 0)
            )
            if delta:
                residents_effect.append({"subject_id": sid, "payment": delta})
        kinds = []
        if second in policies[first]["exclusive_with"] or first in policies[second]["exclusive_with"]:
            kinds.append("exclusivity")
        if second in policies[first]["deduct_payments_of"] or first in policies[second]["deduct_payments_of"]:
            kinds.append("supplementary_deduction")
        if any(bundle["cap_adjustment_total"] for bundle in (full, without_first, without_second, without_both)):
            kinds.append("overall_payment_cap")
        if not kinds:
            kinds.append("overlap")
        marginal_change = [
            {
                "policy": first,
                "in_bundle": marginal_metric(full, without_first),
                "without_other": marginal_metric(without_second, without_both),
            },
            {
                "policy": second,
                "in_bundle": marginal_metric(full, without_second),
                "without_other": marginal_metric(without_first, without_both),
            },
        ]
        interactions.append({
            "policies": [first, second],
            "kinds": kinds,
            "covered": covered,
            "payment": payment,
            "residents": residents_effect,
            "evidence": sorted(set(policies[first]["evidence"]) | set(policies[second]["evidence"])),
            "marginal_change": marginal_change,
        })
    cost_shifts = []
    for marginal in marginals:
        negatives = {payer: -delta for payer, delta in marginal["marginal"]["by_payer"].items() if delta < 0}
        if not negatives:
            continue
        _, sub = get_bundle(ids - {marginal["policy_id"]})
        neutral = sorted(
            sid for sid in full["resident_totals"]
            if full["resident_totals"].get(sid, 0) == sub["resident_totals"].get(sid, 0)
            and full["resident_payers"].get(sid) != sub["resident_payers"].get(sid)
        )
        cost_shifts.append({
            "policy_id": marginal["policy_id"],
            "from_payers": [{"payer": payer, "amount": amount} for payer, amount in sorted(negatives.items())],
            "to_payers": sorted(payer for payer, delta in marginal["marginal"]["by_payer"].items() if delta > 0),
            "amount": sum(negatives.values()),
            "payment_neutral_residents": neutral,
            "note": "该条款存在使 from_payers 支出减少；居民总待遇变化见 marginal.payment",
        })
    return {
        "policies": sorted(ids),
        "joint": {
            "covered_certain": len(full["covered_set"]),
            "payment_certain": full["payment_total"],
            "by_payer_certain": dict(sorted(full["by_payer"].items())),
            "cap_adjustment_certain": full["cap_adjustment_total"],
        },
        "marginals": marginals,
        "interactions": interactions,
        "cost_shifts": cost_shifts,
    }


def vs_baseline(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "covered_delta": len(candidate["covered_set"]) - len(baseline["covered_set"]),
        "payment_delta": candidate["payment_total"] - baseline["payment_total"],
        "by_payer_delta": payer_delta(candidate["by_payer"], baseline["by_payer"]),
        "cap_adjustment_delta": candidate["cap_adjustment_total"] - baseline["cap_adjustment_total"],
        "new_beneficiaries": sorted(candidate["covered_set"] - baseline["covered_set"]),
    }


def build_outputs(data: Any, fixture_name: str | None = None) -> tuple[dict, dict, dict, dict, dict]:
    if not isinstance(data, Mapping):
        raise DomainError("分析输入必须是对象")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise DomainError(f"schema_version 必须是 {SCHEMA_VERSION}")
    as_of = parse_date(data.get("as_of"), "as_of")
    if as_of is None:
        raise DomainError("as_of 不能为空")
    residents = normalize_residents(data.get("residents"))
    policies, unrecognized_policies, inactive_policies = normalize_policies(data.get("policies"), as_of)
    raw_cap = data.get("overall_payment_cap_per_person")
    overall_cap = as_number(raw_cap, "overall_payment_cap_per_person") if raw_cap is not None else None

    attr_issues: dict = {}
    attr_uncertainty: dict = {}
    cache: dict = {}

    def get_bundle(ids):
        key = frozenset(ids)
        if key not in cache:
            cache[key] = evaluate_bundle(key, policies, residents, as_of, overall_cap, attr_issues, attr_uncertainty)
        return cache[key]

    unrecognized_ids = {item["policy_id"] for item in unrecognized_policies}
    baseline_entry, baseline_internal = run_bundle("baseline", data.get("baseline"), policies, unrecognized_ids, get_bundle)

    raw_candidates = data.get("candidates")
    if not isinstance(raw_candidates, list):
        raise DomainError("candidates 必须是数组")
    candidate_entries = []
    candidate_internals: dict[str, dict] = {}
    seen_names = set()
    for index, raw in enumerate(raw_candidates):
        if not isinstance(raw, Mapping):
            raise DomainError(f"candidates[{index}] 必须是对象")
        name = normalize_text(raw.get("name"), "candidate.name", 96) or f"candidate-{index}"
        if name in seen_names:
            raise DomainError(f"candidate 名称重复: {name}")
        seen_names.add(name)
        entry, internal = run_bundle(name, raw.get("policies"), policies, unrecognized_ids, get_bundle)
        candidate_entries.append(entry)
        if internal is not None:
            candidate_internals[name] = internal

    def interactions_for(entry: dict) -> dict:
        if entry["status"] != "evaluated":
            return {"name": entry["name"], "status": "rejected", "reasons": entry["reasons"]}
        result = interactions_entry(entry["policies"], policies, get_bundle)
        result["name"] = entry["name"]
        result["status"] = "evaluated"
        return result

    interactions_baseline = interactions_for(baseline_entry)
    interactions_candidates = []
    for entry in candidate_entries:
        item = interactions_for(entry)
        if entry["status"] == "evaluated" and baseline_internal is not None:
            item["vs_baseline"] = vs_baseline(candidate_internals[entry["name"]], baseline_internal)
        interactions_candidates.append(item)

    evaluated = [entry["name"] for entry in [baseline_entry, *candidate_entries] if entry["status"] == "evaluated"]
    rejected = [entry for entry in [baseline_entry, *candidate_entries] if entry["status"] == "rejected"]

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "as_of": as_of.isoformat(),
        "fixture": fixture_name,
        "input_fingerprint": fingerprint(data),
        "versions": {
            "population_version": data.get("population_version"),
            "funding_version": data.get("funding_version"),
            "interpretation_version": fingerprint(data.get("policies")),
        },
        "roster_size": len(residents),
        "overall_payment_cap_per_person": overall_cap,
        "assumptions": ASSUMPTIONS,
        "disclaimer": DISCLAIMER,
        "files": list(OUTPUT_FILES),
    }
    joint = {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "roster_size": len(residents),
        "overall_payment_cap_per_person": overall_cap,
        "baseline": baseline_entry,
        "candidates": candidate_entries,
    }
    interactions = {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "method": METHOD,
        "baseline": interactions_baseline,
        "candidates": interactions_candidates,
    }
    unrecognized = {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of.isoformat(),
        "unrecognized_policies": unrecognized_policies,
        "inactive_policies": inactive_policies,
        "disputed_policies": [
            {"policy_id": policy["id"], "evidence": policy["evidence"], "note": "未裁决条款：仅计入可能覆盖与预算上限"}
            for policy in policies.values()
            if policy["status"] in STATUS_POSSIBLE
        ],
        "attribute_uncertainty": sorted(attr_uncertainty.values(), key=lambda row: (row["subject_id"], row["policy_id"], row["cause"])),
        "attribute_issues": sorted(attr_issues.values(), key=lambda row: (row["subject_id"], row["attribute"])),
        "rejected_candidates": rejected,
    }
    summary = {
        "as_of": as_of.isoformat(),
        "files": list(OUTPUT_FILES),
        "evaluated": evaluated,
        "rejected": [entry["name"] for entry in rejected],
    }
    if baseline_internal is not None:
        summary["baseline"] = {
            "covered_certain": len(baseline_internal["covered_set"]),
            "payment_certain": baseline_internal["payment_total"],
        }
    return manifest, joint, interactions, unrecognized, summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cancer_policy_graph.impact",
        description="筛查补助组合批量影响分析（不决定个人待遇，不作诊断）",
    )
    parser.add_argument("fixture")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        data = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"无法读取输入: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        manifest, joint, interactions, unrecognized, summary = build_outputs(data, fixture_name=args.fixture)
    except DomainError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)
    for name, payload in zip(OUTPUT_FILES, (manifest, joint, interactions, unrecognized)):
        (outdir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = dict(summary, output=str(outdir))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
