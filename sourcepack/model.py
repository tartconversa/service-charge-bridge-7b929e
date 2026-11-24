"""业务事实建模（纯函数，不落库）。

输入为当前生效的来源行字典与人工决策，输出可核对的事实结构。
本模块不判定治疗必要性或医疗效果，只记录证据、归属与争议。
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from .money import allocate, to_cents

D = Decimal

# 兑现裁决状态
ACCEPTED = "accepted"   # 已核实兑现：证据 confirmed、适配、容量与互斥全部通过
DISPUTED = "disputed"   # 有冲突或硬伤，保留待人工处理，不选先录者
PENDING = "pending"     # 结构无误但执行证据尚未 confirmed

# 人工决策取值
MANUAL_STATUSES = {ACCEPTED, DISPUTED, PENDING}


def num(value):
    """来源字符串数值转 Decimal；空值返回 None。"""
    if value is None or value == "":
        return None
    return D(str(value))


def dt(value):
    if value in (None, ""):
        return None
    return datetime.datetime.fromisoformat(value)


def build_domain(rows):
    """把 current_rows 的字符串行转成带类型的领域对象。"""
    domain = {}
    for name, records in rows.items():
        typed = []
        for row in records:
            item = dict(row)
            for column in ("units", "relative_price", "unit_ratio", "price", "amount"):
                if column in item:
                    item[column] = num(item[column])
            for column in ("sold_at", "at"):
                if column in item:
                    item[column] = dt(item[column])
            typed.append(item)
        domain[name] = typed
    # 索引
    domain["packages_by_id"] = {r["package_id"]: r for r in domain["packages"]}
    domain["slots_by_id"] = {r["slot_id"]: r for r in domain["slots"]}
    domain["executions_by_id"] = {r["execution_id"]: r for r in domain["executions"]}
    domain["requests_by_id"] = {r["request_id"]: r for r in domain["requests"]}
    domain["links_by_id"] = {r["link_id"]: r for r in domain["fulfillment_links"]}
    domain["refunds_by_id"] = {r["refund_id"]: r for r in domain["refunds"]}
    # 替代规则索引：(原类, 实际类) -> unit_ratio
    domain["sub_map"] = {
        (r["slot_service"], r["actual_service"]): r["unit_ratio"]
        for r in domain["substitutions"]
    }
    return domain


def _service_fit(slot, execution, sub_map):
    """返回 (适配类型, 消耗折算率)。折算率=每实际单位消耗的原权益单位。"""
    if slot["service_code"] == execution["service_code"]:
        return "direct", D("1")
    ratio = sub_map.get((slot["service_code"], execution["service_code"]))
    if ratio is not None:
        return "substitution", ratio
    return "none", None


def adjudicate(domain, decisions=None):
    """逐条裁决来源兑现引用。

    decisions 为已确认对账轮中的人工决策列表；返回
    ``{link_id: 事实字典}``，所有来源引用都保留，不存在静默丢弃。
    """
    decisions = {d["link_id"]: d for d in (decisions or [])}
    slots = domain["slots_by_id"]
    executions = domain["executions_by_id"]
    packages = domain["packages_by_id"]
    sub_map = domain["sub_map"]

    links = []
    for src in sorted(domain["fulfillment_links"], key=lambda r: r["link_id"]):
        link_id = src["link_id"]
        decision = decisions.get(link_id)
        effective_slot_id = (
            decision["new_slot_id"]
            if decision and decision.get("new_slot_id")
            else src["slot_id"]
        )
        slot = slots.get(effective_slot_id)
        execution = executions.get(src["execution_id"])
        hard_reasons, fit_type, consumed = [], "none", None

        if slot is None or execution is None:
            hard_reasons.append("missing_reference")
        else:
            package = packages.get(slot["package_id"])
            if package is not None and package["person"] != execution["person"]:
                hard_reasons.append("person_mismatch")
            fit_type, ratio = _service_fit(slot, execution, sub_map)
            if fit_type == "none":
                hard_reasons.append("service_mismatch")
            else:
                # link.units 以权益单位计；折算成执行单位做执行侧容量检查
                consumed = src["units"] / ratio if ratio else src["units"]
            if package is not None and execution["at"] is not None and execution["at"] < package["sold_at"]:
                hard_reasons.append("execution_before_sale")

        fact = {
            "link_id": link_id,
            "execution_id": src["execution_id"],
            "source_slot_id": src["slot_id"],
            "effective_slot_id": effective_slot_id,
            "units": src["units"],
            "execution_consumed_units": consumed if consumed is not None else D("0"),
            "fit_type": fit_type,
            "hard_reasons": hard_reasons,
            "soft_reasons": [],
            "decision": decision,
        }
        links.append(fact)

    candidates = [f for f in links if not f["hard_reasons"]]

    # 权益侧容量：同一 slot 的候选引用按业务键升序，超额者争议
    by_slot = {}
    for fact in candidates:
        by_slot.setdefault(fact["effective_slot_id"], []).append(fact)
    for slot_id, group in by_slot.items():
        capacity = slots[slot_id]["units"]
        used = D("0")
        for fact in sorted(group, key=lambda f: f["link_id"]):
            used += fact["units"]
            if used > capacity:
                fact["hard_reasons"].append("slot_capacity_exceeded")

    # 执行侧容量：一次执行的真实单位不能被引用超用
    by_exec = {}
    for fact in candidates:
        by_exec.setdefault(fact["execution_id"], []).append(fact)
    for execution_id, group in by_exec.items():
        capacity = executions[execution_id]["units"]
        used = D("0")
        for fact in sorted(group, key=lambda f: f["link_id"]):
            used += fact["execution_consumed_units"]
            if used > capacity:
                fact["hard_reasons"].append("execution_capacity_exceeded")

    # 互斥组：同组一次执行最多兑一项（组可跨包），冲突全部争议
    by_group_exec = {}
    for fact in candidates:
        group = slots[fact["effective_slot_id"]].get("exclusive_group")
        if group:
            by_group_exec.setdefault((group, fact["execution_id"]), []).append(fact)
    for (group, execution_id), group_links in by_group_exec.items():
        if len(group_links) > 1:
            for fact in group_links:
                fact["hard_reasons"].append(
                    f"exclusive_conflict:{group}"
                )

    # 汇总状态；人工决策只能在不破坏硬规则的前提下改判
    for fact in links:
        decision = fact["decision"]
        if fact["hard_reasons"]:
            fact["status"] = DISPUTED
            if decision and decision["decision"] == ACCEPTED:
                fact["soft_reasons"].append("manual_accept_blocked_by_rule")
        else:
            execution = executions.get(fact["execution_id"])
            evidence_pending = bool(execution and execution["status"] != "confirmed")
            if decision and decision["decision"] in MANUAL_STATUSES:
                if decision["decision"] == ACCEPTED and evidence_pending:
                    # 来源口径：只有 confirmed 才计已核实，人工不能替代证据
                    fact["status"] = PENDING
                    fact["soft_reasons"].append("manual_accept_blocked_pending_evidence")
                else:
                    fact["status"] = decision["decision"]
                    fact["soft_reasons"].append("manual_decision:" + decision["decision"])
                    if evidence_pending and fact["status"] != PENDING:
                        fact["soft_reasons"].append("execution_pending")
            elif evidence_pending:
                fact["status"] = PENDING
                fact["soft_reasons"].append("execution_pending")
            else:
                fact["status"] = ACCEPTED
        fact["reasons"] = fact["hard_reasons"] + fact["soft_reasons"]
    return {f["link_id"]: f for f in links}


def _slot_claims(link_facts, slot_id):
    accepted = pending = disputed = D("0")
    for fact in link_facts.values():
        if fact["effective_slot_id"] != slot_id:
            continue
        if fact["status"] == ACCEPTED:
            accepted += fact["units"]
        elif fact["status"] == PENDING:
            pending += fact["units"]
        else:
            disputed += fact["units"]
    return accepted, pending, disputed


def classify_refunds(domain, link_facts):
    """退款按权益撤回范围分类：干净撤回 vs 触及已履行的争议。

    同一 slot 多笔退款按业务时点（at、refund_id）次序消耗可用权益。
    返回 (refund_facts, slot_refund_units)。
    """
    slot_accepted = {
        slot_id: _slot_claims(link_facts, slot_id)[0]
        for slot_id in domain["slots_by_id"]
    }
    slot_refund_units = {
        slot_id: {"clean": D("0"), "disputed": D("0")}
        for slot_id in domain["slots_by_id"]
    }
    refund_facts = []
    ordered = sorted(domain["refunds"], key=lambda r: (r["at"], r["refund_id"]))
    for refund in ordered:
        slot_id = refund["slot_id"]
        slot = domain["slots_by_id"][slot_id]
        available = slot["units"] - slot_accepted[slot_id]
        already = sum(slot_refund_units[slot_id].values(), D("0"))
        clean = max(D("0"), min(refund["units"], available - already))
        disputed = refund["units"] - clean
        slot_refund_units[slot_id]["clean"] += clean
        slot_refund_units[slot_id]["disputed"] += disputed
        flags = []
        if disputed > 0:
            flags.append("touches_fulfilled_entitlement")
        if _slot_claims(link_facts, slot_id)[1] + _slot_claims(link_facts, slot_id)[2] > 0:
            flags.append("slot_has_unresolved_claims")
        if refund["units"] > slot["units"]:
            flags.append("refund_exceeds_slot_units")
        refund_facts.append(
            {
                "refund_id": refund["refund_id"],
                "slot_id": slot_id,
                "units": refund["units"],
                "actual_amount_cents": to_cents(refund["amount"]),
                "at": refund["at"],
                "clean_units": clean,
                "disputed_units": disputed,
                "review_flags": flags,
            }
        )
    return refund_facts, slot_refund_units


def compute_positions(domain, link_facts):
    """合同固定分母分摊 + 退款分类，返回 slot/package 级归属事实。

    价格归属严格按销售时相对价权重；部分执行时未分部分留在包内，
    不对已执行项重新归一。
    """
    # 第一阶段：全包合同价分到各 slot（固定分母 = 全包总权重）
    slot_contract = {}
    for package in domain["packages"]:
        pkg_slots = [s for s in domain["slots"] if s["package_id"] == package["package_id"]]
        price_cents = to_cents(package["price"])
        weights = [(s["slot_id"], s["relative_price"] * s["units"]) for s in pkg_slots]
        slot_contract.update(allocate(price_cents, weights))

    refund_facts, slot_refund_units = classify_refunds(domain, link_facts)

    slot_positions = []
    for slot in domain["slots"]:
        slot_id = slot["slot_id"]
        accepted_units, pending_units, disputed_units = _slot_claims(link_facts, slot_id)
        refund_units = slot_refund_units[slot_id]
        contract = slot_contract[slot_id]
        # 第二阶段：slot 合同值按四类单位基数分摊，尾差归最大基数项
        comp = allocate(
            contract,
            [
                ((slot_id, "verified"), accepted_units),
                ((slot_id, "clean_refund"), refund_units["clean"]),
                ((slot_id, "disputed_refund"), refund_units["disputed"]),
                (
                    (slot_id, "unfulfilled"),
                    max(
                        D("0"),
                        slot["units"] - accepted_units
                        - refund_units["clean"] - refund_units["disputed"],
                    ),
                ),
            ],
        )
        # 备忘：未决引用（pending/disputed）中尚未被退款或已核实占用的单位。
        # 这是口径外的提示额，不参与四类合同成分的恒等式，避免重复计单位。
        occupied = accepted_units + refund_units["clean"] + refund_units["disputed"]
        unresolved_units = pending_units + disputed_units
        memo_base = max(D("0"), min(unresolved_units, slot["units"] - occupied))
        memo_cents = int(
            (D(contract) * memo_base / slot["units"]).to_integral_value()
        ) if slot["units"] > 0 and memo_base > 0 else 0
        flags = []
        if pending_units > 0:
            flags.append("has_pending_claims")
        if disputed_units > 0:
            flags.append("has_disputed_claims")
        slot_positions.append(
            {
                "slot_id": slot_id,
                "package_id": slot["package_id"],
                "service_code": slot["service_code"],
                "units": slot["units"],
                "relative_price": slot["relative_price"],
                "exclusive_group": slot.get("exclusive_group") or "",
                "contract_cents": contract,
                "accepted_units": accepted_units,
                "pending_claim_units": pending_units,
                "disputed_claim_units": disputed_units,
                "refunded_units": refund_units["clean"] + refund_units["disputed"],
                "clean_refund_units": refund_units["clean"],
                "disputed_refund_units": refund_units["disputed"],
                "verified_contract_cents": comp[(slot_id, "verified")],
                "clean_refund_contract_cents": comp[(slot_id, "clean_refund")],
                "disputed_refund_contract_cents": comp[(slot_id, "disputed_refund")],
                "unfulfilled_contract_cents": comp[(slot_id, "unfulfilled")],
                "unresolved_contract_cents_memo": memo_cents,
                "review_flags": flags,
            }
        )

    # 已兑现收入再分到每条 accepted 引用（同 slot 内按引用单位，恒等可核对）
    slot_pos_by_id = {p["slot_id"]: p for p in slot_positions}
    for fact in link_facts.values():
        fact["revenue_cents"] = 0
    for slot_id, pos in slot_pos_by_id.items():
        accepted_links = [
            f for f in link_facts.values()
            if f["effective_slot_id"] == slot_id and f["status"] == ACCEPTED
        ]
        if not accepted_links:
            continue
        shares = allocate(
            pos["verified_contract_cents"],
            [(f["link_id"], f["units"]) for f in accepted_links],
        )
        for link_id, cents in shares.items():
            link_facts[link_id]["revenue_cents"] = cents

    # 退款行的合同资格额：在 slot 退款成分内按单位二次分摊，保证恒等
    refund_by_id = {r["refund_id"]: r for r in refund_facts}
    for slot_id, units_map in slot_refund_units.items():
        pos = slot_pos_by_id[slot_id]
        rows = [r for r in refund_facts if r["slot_id"] == slot_id]
        for kind, units_key, cents_key in (
            ("clean", "clean_units", "clean_contract_cents"),
            ("disputed", "disputed_units", "disputed_contract_cents"),
        ):
            total_cents = (
                pos["clean_refund_contract_cents"]
                if kind == "clean"
                else pos["disputed_refund_contract_cents"]
            )
            shares = allocate(
                total_cents, [(r["refund_id"], r[units_key]) for r in rows]
            )
            for r in rows:
                r.setdefault("clean_contract_cents", 0)
                r.setdefault("disputed_contract_cents", 0)
                r[cents_key] = shares[r["refund_id"]]
    for r in refund_facts:
        r["entitlement_cents"] = r["clean_contract_cents"] + r["disputed_contract_cents"]
        r["difference_cents"] = r["actual_amount_cents"] - r["entitlement_cents"]

    package_positions = _aggregate_packages(domain, slot_positions)
    return slot_positions, package_positions, refund_facts, refund_by_id


def _aggregate_packages(domain, slot_positions):
    cash_by_pkg = {}
    for cash in domain["cash"]:
        entry = cash_by_pkg.setdefault(
            cash["package_id"],
            {"receive": 0, "refund": 0, "unmatched_refund": 0},
        )
        entry[cash["kind"]] += to_cents(cash["amount"])
    result = []
    for package in domain["packages"]:
        pkg_id = package["package_id"]
        slots = [p for p in slot_positions if p["package_id"] == pkg_id]
        cash_info = cash_by_pkg.get(pkg_id, {"receive": 0, "refund": 0, "unmatched_refund": 0})
        result.append(
            {
                "package_id": pkg_id,
                "person": package["person"],
                "price_cents": to_cents(package["price"]),
                "received_cash_cents": cash_info["receive"],
                "refunded_cash_cents": cash_info["refund"],
                "net_cash_cents": cash_info["receive"] - cash_info["refund"],
                "verified_contract_cents": sum(p["verified_contract_cents"] for p in slots),
                "clean_refund_contract_cents": sum(p["clean_refund_contract_cents"] for p in slots),
                "disputed_refund_contract_cents": sum(p["disputed_refund_contract_cents"] for p in slots),
                "unfulfilled_contract_cents": sum(p["unfulfilled_contract_cents"] for p in slots),
                "unresolved_contract_cents_memo": sum(
                    p["unresolved_contract_cents_memo"] for p in slots
                ),
            }
        )
    return result


def match_cash(domain, refund_facts):
    """现金退款与退款事实单匹配；两表不可相加，只建立核对关系。

    口径：refund_id 直连（来源未提供时）退化为 package/at/amount 唯一匹配；
    无法唯一对应留待核。
    """
    refund_by_id = {r["refund_id"]: r for r in refund_facts}
    slot_pkg = {s["slot_id"]: s["package_id"] for s in domain["slots"]}
    facts = []
    matched_ids = set()
    for cash in sorted(domain["cash"], key=lambda r: r["cash_id"]):
        fact = {
            "cash_id": cash["cash_id"],
            "package_id": cash["package_id"],
            "kind": cash["kind"],
            "amount_cents": to_cents(cash["amount"]),
            "at": cash["at"],
            "match_status": "n_a",
            "matched_refund_id": "",
        }
        if cash["kind"] == "refund":
            candidates = [
                r for r in refund_facts
                if slot_pkg.get(r["slot_id"]) == cash["package_id"]
                and r["actual_amount_cents"] == fact["amount_cents"]
                and r["at"] == cash["at"]
            ]
            if len(candidates) == 1:
                fact["match_status"] = "matched"
                fact["matched_refund_id"] = candidates[0]["refund_id"]
                matched_ids.add(candidates[0]["refund_id"])
            elif len(candidates) > 1:
                fact["match_status"] = "ambiguous"
            else:
                fact["match_status"] = "unmatched"
        facts.append(fact)
    for refund in refund_facts:
        refund["cash_match_status"] = (
            "matched" if refund["refund_id"] in matched_ids else "missing_cash_evidence"
        )
    return facts


def execution_facts(domain, link_facts):
    """科室侧执行事实：服务量只认 confirmed 证据，退款不回冲服务次数。"""
    links_by_exec = {}
    for fact in link_facts.values():
        links_by_exec.setdefault(fact["execution_id"], []).append(fact)
    result = []
    for execution in domain["executions"]:
        exec_id = execution["execution_id"]
        linked = links_by_exec.get(exec_id, [])
        accepted = [f for f in linked if f["status"] == ACCEPTED]
        result.append(
            {
                "execution_id": exec_id,
                "request_id": execution["request_id"],
                "person": execution["person"],
                "service_code": execution["service_code"],
                "team": execution["team"],
                "units": execution["units"],
                "at": execution["at"],
                "service_date": execution["at"].date().isoformat(),
                "status": execution["status"],
                "link_count": len(linked),
                "accepted_link_count": len(accepted),
                "revenue_cents": sum(f["revenue_cents"] for f in accepted),
                "request_state": domain["requests_by_id"]
                .get(execution["request_id"], {})
                .get("state", ""),
            }
        )
    return sorted(result, key=lambda r: (r["service_date"], r["team"], r["execution_id"]))
