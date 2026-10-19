"""Conservative operational next steps from tool facts, never inferred causes."""

import re

VERSION = "fact-followup-v6"
NEGATED_REQUEST = r"不(?:计算|比较|核对|检查|统计)|无需|不需要|不要|\b(?:do not|don't|without calculating|without comparing|not(?!\s+only))\b"


def requested_metric_families(query):
    text = query.lower()
    requested = set()
    def cue(chinese, english):
        return any(word in text for word in chinese) or bool(re.search(english, text))

    if cue(("销量", "销售", "流量", "转化"), r"\b(?:sales|sessions?|traffic|conversion)\b"):
        requested.add("daily")
    if cue(("退款", "冲正"), r"\brefund(?:s|ed|ing)?\b"):
        requested.add("refunds")
    return_text = re.sub(r"\breturn\s+(?:value|code|statement|type)\b", "", text)
    if "退货" in text or re.search(r"\b(?:returns|returned|return)\b", return_text):
        requested.add("returns")
    if not requested and cue(("成熟", "分母"), r"\b(?:cohort|mature|denominator)\b"):
        requested.update(("refunds", "returns"))
    return requested



def requested_metric_requests(query):
    """Parse bounded metric basis cues; excluded calculations are not prerequisites."""
    requests = set()
    inherited = set()
    negative = NEGATED_REQUEST
    cohort_cues = r"队列|成熟|比例|分母|风险|退款率|退货率|\b(?:cohort|mature|rates?|risk|denominator)\b"
    occurrence_cues = r"发生|金额|件数|数量|收到|申请|\b(?:occurrence|amounts?|units|counts?|received|requested)\b"
    excluded_cohort = False
    for clause in re.split(r"[，,；;。]|以及|和|\band\b", query.lower()):
        parts = re.split(negative, clause, maxsplit=1)
        positive = parts[0]
        if len(parts) > 1 and re.search(cohort_cues, parts[1]):
            excluded_cohort = True
        families = requested_metric_families(positive)
        explicit_families = families - ({"refunds", "returns"} if not re.search(r"退款|冲正|退货|\b(?:refund\w*|returns?|returned)\b", positive) else set())
        if explicit_families:
            inherited = explicit_families
        else:
            families = inherited if re.search(cohort_cues + "|" + occurrence_cues, positive) else families
        for family in families:
            if family == "daily":
                requests.add((family, "daily"))
                continue
            occurrence = bool(re.search(occurrence_cues, positive))
            cohort = bool(re.search(cohort_cues, positive))
            if occurrence:
                requests.add((family, "occurrence"))
            if cohort or not occurrence:
                requests.add((family, "cohort"))
    if excluded_cohort:
        # Only replace an implicit default; positively requested cohort remains.
        positive_text = " ".join(re.split(negative, clause, maxsplit=1)[0] for clause in re.split(r"[，,；;。]|以及|和|\band\b", query.lower()))
        if not re.search(cohort_cues, positive_text):
            requests = {(family, "occurrence" if mode == "cohort" else mode) for family, mode in requests}
    return sorted(requests)

def followup(evidence, *, query=""):
    searches = [item for item in evidence if item.get("type") == "text_retrieval"]
    snapshots = [item for item in evidence if item.get("type") == "metric_snapshot"]
    if any(item.get("retrieval_complete") is False for item in searches):
        return "narrow_evidence_scope_and_retry", "检索达到候选预算；先缩小已授权日期或主题范围再重试。", "candidate_budget_exhausted"
    requested = requested_metric_families(query)
    for snapshot in snapshots if not requested or "daily" in requested else []:
        daily = snapshot.get("metrics", {}).get("daily", {})
        status = daily.get("status")
        if status in {"stale", "partial", "invalid"}:
            return "repair_daily_source_before_comparison", "日指标来源未满足比较条件；补报或修复来源后重新计算，暂停经营原因判断。", f"daily_{status}"
    if any(item.get("match_status") == "relevant_hits" for item in searches):
        return "verify_original_record_with_source_owner", "将已命中的原文交来源负责人核实个案，核对记录对象及事实；保持因果与总体比例未知。", "scoped_original_hit"
    if searches and all(item.get("match_status") == "no_hits" and item.get("retrieval_complete") is True for item in searches):
        return "request_matching_original_records", "请求同一授权对象及日期范围的相关原始记录；本次未命中不能证明问题不存在。", "complete_search_without_hits"
    if snapshots and all(item.get("status") == "missing" for item in snapshots):
        return "create_current_metric_snapshot", "当前冻结指标快照缺失；先生成同一对象及来源版本的指标快照。", "metric_snapshot_missing"
    return "unknown", "现有结构化事实不足以确定下一项业务动作。", "insufficient_structured_facts"


def structured_followup(evidence):
    """Consume calculator states; never derive a financial amount or cohort rate."""
    for snapshot in [entry for entry in evidence if entry.get("type") == "metric_snapshot"]:
        metrics = snapshot.get("metrics", {})
        daily = metrics.get("daily", {})
        cohort = metrics.get("cohort", {})
        occurrence = metrics.get("occurrence", {})
        comparable = metrics.get("comparability", {})
        findings = []

        def add(rule, condition, next_step, message, paths):
            if condition:
                citations = [atom for atom in snapshot.get("metric_evidence", {}).get("atoms", []) if any(atom["path"].startswith(path) for path in paths)][:8]
                findings.append({"rule_id": rule, "next_step": next_step, "instruction": message,
                                 "input_paths": paths, "citations": citations,
                                 "stop_condition": "stop_until_" + rule + "_resolved",
                                 "allowed_statement": "仅可陈述所引用的状态、窗口或分母事实。",
                                 "forbidden_statement": "不能据此证明经营原因、整体缺陷率或批准外部变更。"})

        baseline, current = daily.get("baseline", {}), daily.get("current", {})
        add("currency_mismatch", baseline.get("currency") and current.get("currency") and baseline["currency"] != current["currency"],
            "reconcile_amounts_by_currency", "两窗币种不同；按币种分别对账，未获批准汇率口径前不合并金额。", ["daily.baseline.currency", "daily.current.currency"])
        refunds = occurrence.get("refunds", {})
        currencies = [set(refunds.get(window, {}).get("amount_by_currency", {})) for window in ("baseline", "current")]
        add("multiple_refund_currencies", any(len(keys) > 1 for keys in currencies) or all(currencies) and currencies[0] != currencies[1],
            "reconcile_amounts_by_currency", "退款金额存在多个币种或两窗币种不同；分别核对原交易，不跨币种相加。", ["occurrence.refunds.baseline.amount_by_currency", "occurrence.refunds.current.amount_by_currency"])
        add("mapping_not_comparable", bool(comparable.get("structure_changes")), "reconcile_product_mapping_before_comparison",
            "比较窗内ASIN/SKU父子映射发生变化；核对冻结映射与粒度，不能将不同对象的Sessions相加。", ["comparability.status", "comparability.structure_changes"])
        add("overlapping_operations", bool(comparable.get("operations")), "review_overlapping_operations",
            "比较窗存在运营事件；核对事件与两窗重叠，保留指标事实但不能声称运营事件就是原因。", ["comparability.status", "comparability.operations"])
        for kind in ("refunds", "returns"):
            section = cohort.get(kind, {})
            first, second = section.get("baseline", {}), section.get("current", {})
            prefix = f"cohort.{kind}"
            add("refund_links_incomplete", kind == "refunds" and (section.get("unmatched_records", 0) > 0 or any((chain.get("classification") in {"unmatched", "before_shipment"} or chain.get("chain_status") == "reversal_without_positive_anchor") for chain in section.get("refund_chains", []))),
                "reconcile_order_refund_reversal_links", "退款或冲正记录缺少有效发货订单链；按同订单行、交易链与币种补齐关联，不能把孤立负数证明为完整冲正。", [prefix + ".unmatched_records", prefix + ".refund_chains"])
            add("shipment_coverage_incomplete", any(window.get("unverified_first_shipment_orders", 0) > 0 for window in (first, second)),
                "restore_first_shipment_coverage", "部分订单首发货时间覆盖无法核实；补齐发货来源，再计算成熟队列。", [prefix + ".baseline.unverified_first_shipment_orders", prefix + ".current.unverified_first_shipment_orders"])
            add("observation_incomplete", second.get("denominator") == 0 and (section.get("observing_orders", 0) > 0 or second.get("immature_orders", 0) > 0),
                "wait_for_complete_observation_window", "当前队列尚无完整成熟分母且仍有观察中订单；等待既定观察期和事件覆盖，不推断风险下降。", [prefix + ".observing_orders", prefix + ".current.denominator", prefix + ".current.immature_orders", "cohort.observation_days"])
            add("cohort_low_sample", section.get("observation", {}).get("status") == "low_sample", "review_cases_without_population_inference",
                "计算器已标记成熟队列样本不足；逐个核查已发生订单，不能判断整体风险上升或下降。", [prefix + ".observation", prefix + ".baseline.denominator", prefix + ".current.denominator"])
            add("cohort_source_incomplete", section.get("status") in {"missing", "partial", "stale", "invalid"}, "restore_cohort_source_coverage",
                "成熟队列来源缺失、缺日、过期或口径不符；修复订单与事件覆盖，先停止队列风险比较。", [prefix + ".status", prefix + ".reason"])
        returns = occurrence.get("returns", {})
        bases = [returns.get(window, {}).get("event_basis") for window in ("baseline", "current")]
        add("return_basis_mixed", returns.get("status") == "invalid" or all(bases) and len(set(bases)) > 1,
            "separate_requested_and_received_return_sources", "退货申请与收到的口径不可直接比较；拆分来源及两窗口径再核查。", ["occurrence.returns.status", "occurrence.returns.reason", "occurrence.returns.baseline.event_basis", "occurrence.returns.current.event_basis"])
        add("return_receipt_unverified", "requested" in bases and not any(item.get("kind") == "received_return_evidence" for item in evidence),
            "verify_return_receipt_before_received_claim", "当前退货发生量采用申请口径；核对收到日期和状态，申请不能等同仓库已收到。", ["occurrence.returns.baseline.event_basis", "occurrence.returns.current.event_basis"])
        if daily.get("status") in {"missing", "partial", "stale", "invalid"}:
            add("daily_source_incomplete", True, "repair_daily_source_before_comparison", "日指标缺源、缺日、过期或粒度不符；补齐来源与窗口，暂停原因比较。", ["daily.status", "daily.reason", "daily.baseline.complete", "daily.current.complete"])
        if findings:
            return {"version": VERSION, "origin": "deterministic_tool_facts", **findings[0], "findings": findings}
    authorization = next((entry for entry in evidence if entry.get("type") == "authorization_boundary" and entry.get("external_change_requested")), None)
    if authorization and not authorization.get("external_write_authorized", False):
        return {"version": VERSION, "origin": "deterministic_tool_facts", "rule_id": "external_approval_required",
                "next_step": "request_human_approved_change_process", "instruction": "当前仅授权调查或内部草稿；外部经营变更必须由负责人通过批准流程确认。",
                "input_paths": ["authorization_boundary.external_write_authorized"], "citations": [],
                "stop_condition": "stop_before_external_change", "allowed_statement": "当前工具不提供外部变更权限。",
                "forbidden_statement": "调查成功或草稿创建不等于变更批准。", "findings": []}
    return None


def focused_structured_followup(query, evidence):
    """A missing unrelated source cannot block the metric actually requested."""
    result = structured_followup(evidence)
    if not result or not result.get("findings"):
        return result
    requests = requested_metric_requests(query)
    requested = {family for family, _ in requests}
    if not requested:
        # Generic metric/currency questions have not selected a narrower family.
        return result

    def relevant(finding):
        paths = finding["input_paths"]
        if any(path.startswith("comparability.") for path in paths):
            return True
        families = set()
        for path in paths:
            if path.startswith("daily."):
                families.add("daily")
            for kind in ("refunds", "returns"):
                if path.startswith((f"cohort.{kind}.", f"occurrence.{kind}.")):
                    families.add(kind)
        modes = {"occurrence" for path in paths if path.startswith("occurrence.")} | {"cohort" for path in paths if path.startswith("cohort.")} | {"daily" for path in paths if path.startswith("daily.")}
        return (not families or bool(families & requested)) and (not modes or any((family, mode) in requests for family in families for mode in modes))

    findings = [finding for finding in result["findings"] if relevant(finding)]
    return {**result, **findings[0], "findings": findings, "requested_metric_families": sorted(requested)} if findings else None


BLOCKING_RULES = {"currency_mismatch", "multiple_refund_currencies", "mapping_not_comparable",
                  "refund_links_incomplete", "shipment_coverage_incomplete", "observation_incomplete",
                  "cohort_source_incomplete", "cohort_low_sample", "return_basis_mixed", "daily_source_incomplete"}
FAMILY_LABELS = {"daily": "销量与流量", "refunds": "退款", "returns": "退货"}


def order_lookup_requested(query):
    return bool(re.search(r"逐单|逐个订单|订单明细|\b(?:each|individual|per)[ -]?order\b|\border[ -]line\b", query, re.I))


def metric_workplan(query, evidence):
    """Separate metric prerequisites; text tools cannot claim order-line auditing."""
    requests = requested_metric_requests(query)
    snapshots = [item for item in evidence if item.get("type") == "metric_snapshot"]
    if (not requests and not order_lookup_requested(query)) or not snapshots:
        return None
    snapshot_missing = all(item.get("status") == "missing" for item in snapshots)
    rows = []
    metrics = next((item.get("metrics", {}) for item in snapshots if item.get("metrics")), {})
    from datetime import date

    from .scope import resolve_scope
    scope = next((item.get("request_scope") for item in snapshots if item.get("request_scope")), None) or resolve_scope(query, today=date.today())
    catalog = next((item.get("metric_evidence", {}).get("atoms", []) for item in snapshots if item.get("metric_evidence")), [])
    for family, mode in requests:
        basis_query = {"daily": "sales", "refunds": "refund", "returns": "退货"}[family] + (" amount occurrence" if mode == "occurrence" else " cohort risk" if mode == "cohort" else "")
        basis = focused_structured_followup(basis_query, evidence)
        findings = [item for item in (basis or {}).get("findings", []) if item["rule_id"] in BLOCKING_RULES]
        prefix = "daily" if mode == "daily" else f"{mode}.{family}"
        section = metrics.get("daily", {}) if mode == "daily" else metrics.get(mode, {}).get(family, {})
        if mode == "occurrence":
            findings = [item for item in findings if item["rule_id"] != "multiple_refund_currencies"]
        if not findings and section.get("status") not in {"ready", "complete"}:
            parent = metrics.get(mode, {}) if mode != "daily" else section
            findings = [{"rule_id": "metric_family_not_established", "instruction": section.get("reason") or parent.get("reason") or "该口径缺少完整冻结状态；不能用另一口径的数据代替。", "next_step": "restore_occurrence_source_coverage" if mode == "occurrence" else "provide_complete_metric_family_snapshot"}]
        if snapshot_missing:
            findings = [{"rule_id": "metric_snapshot_missing", "instruction": "缺少冻结指标快照。", "next_step": "create_current_metric_snapshot"}]
        actual_windows = [{"window": window, "start": section.get(window, {}).get("start"), "end": section.get(window, {}).get("end")}
                          for window in ("baseline", "current") if section.get(window, {}).get("start") and section.get(window, {}).get("end")]
        selected_windows = [window["window"] for window in actual_windows if window["start"] == scope.get("start") and window["end"] == scope.get("end")]
        if scope.get("status") == "explicit" and not selected_windows and len(actual_windows) == 2:
            from datetime import timedelta
            first, second = actual_windows
            if first["start"] == scope["start"] and second["end"] == scope["end"] and date.fromisoformat(first["end"]) + timedelta(days=1) == date.fromisoformat(second["start"]):
                selected_windows = ["baseline", "current"]
        basis_windows = selected_windows if scope["status"] == "explicit" else ["baseline", "current"]
        if mode == "occurrence" and family == "returns":
            if len(basis_windows) == 1 and section.get("status") in {"ready", "complete"}:
                findings = [item for item in findings if item["rule_id"] != "return_basis_mixed"]
            if re.search(r"收到|\breceived\b", " ".join(re.split(NEGATED_REQUEST, clause, maxsplit=1)[0] for clause in re.split(r"[，,；;。]", query.lower())), re.I) and any(section.get(window, {}).get("event_basis") == "requested" for window in basis_windows):
                findings.append({"rule_id": "received_basis_not_available", "instruction": "所选窗口只有退货申请口径；收到件数需收到日期和状态来源，不能用申请件数代替。", "next_step": "provide_received_return_source"})
        window_mismatch = scope["status"] == "needs_clarification" or scope["status"] == "explicit" and not selected_windows
        if window_mismatch:
            findings.append({"rule_id": "requested_window_not_supported", "instruction": scope["reason"] if scope["status"] == "needs_clarification" else f"请求窗口{scope['start']}—{scope['end']}与可用冻结窗口不一致；不替换为默认两窗，也不重算任意财务窗口。请明确选择当前可用窗口或补充对应版本。", "next_step": "clarify_requested_window_or_provide_matching_snapshot"})
        citations = [atom for atom in catalog if atom["path"].startswith(prefix + ".")
                     and not (atom.get("denominator") is None and ("unit_session_percent" in atom["path"] or atom["path"].endswith(".rate")))]
        if scope.get("status") != "default":
            citations = [atom for atom in citations if atom["path"].endswith((".status", ".reason")) or (not window_mismatch and atom.get("window") in selected_windows)]
        # Parent cohort state explains absent sub-sections without inventing an atom.
        if not section and mode == "cohort":
            citations += [atom for atom in catalog if atom["path"] in {"cohort.status", "cohort.reason"}]
        rows.append({"family": family, "label": FAMILY_LABELS[family] + ("发生口径" if mode == "occurrence" else "成熟队列" if mode == "cohort" else ""), "mode": mode,
                     "status": "blocked" if findings else "facts_available", "fact_prefix": prefix,
                     "reason": "；".join(item["instruction"] for item in findings) if findings else "已读取该口径冻结事实；金额按币种分列，不计算未请求的队列率或证明原因。" if mode == "occurrence" else "已读取冻结指标事实；仍不能据此证明原因。",
                     "blocking_rules": [item["rule_id"] for item in findings], "next_steps": [item["next_step"] for item in findings],
                     "requested_scope": scope, "actual_windows": actual_windows, "selected_windows": selected_windows, "window_status": "mismatch" if window_mismatch else "matched" if scope["status"] == "explicit" else "default",
                     "allowed_conclusion": "unknown", "citations": citations})
    order_lookup = order_lookup_requested(query)
    if order_lookup:
        rows.append({"family": "order_records", "label": "逐订单核查", "mode": "order_lookup", "status": "unsupported",
                     "reason": "现有调查工具不提供逐订单发货、退款和退货明细读取；评论/客服原文不能代替订单链核验。需授权订单明细工具及相应来源。",
                     "blocking_rules": ["order_detail_tool_unavailable"], "next_steps": ["provide_authorized_order_detail_tool_and_sources"],
                     "allowed_conclusion": "unknown", "citations": []})
    order_evidence = next((item for item in evidence if item.get("type") == "order_records"), None)
    if order_lookup and order_evidence:
        rows[-1].update(status="facts_available" if order_evidence.get("complete") else "blocked",
                        reason="已读取授权冻结记录；仅限定窗口样本，不推断完整交易链或总体风险。" if order_evidence.get("complete") else "部分来源缺失或样本达到上限；保留已读取记录，需补源或缩小窗口。",
                        blocking_rules=[] if order_evidence.get("complete") else ["order_records_incomplete"],
                        next_steps=[] if order_evidence.get("complete") else ["provide_missing_order_sources_or_narrow_scope"])
    if order_lookup:
        actual = (order_evidence or {}).get("scope", {})
        rows[-1]["requested_scope"] = scope
        rows[-1]["actual_windows"] = [{"window": "events", "start": actual.get("start"), "end": actual.get("end")}] if actual else []
        if scope.get("status") == "explicit" and order_evidence and (actual.get("start"), actual.get("end")) != (scope["start"], scope["end"]):
            rows[-1].update(status="blocked", reason="订单事件窗口与冻结请求窗口不一致；保留关联例外标识，不作为请求范围已完成。",
                            blocking_rules=["order_window_mismatch"], next_steps=["restart_with_frozen_requested_window"])
    gaps = [item for item in rows if item["status"] in {"blocked", "unsupported"}]
    available = [item["family"] for item in rows if item["status"] == "facts_available"]
    return {"version": VERSION, "subtasks": rows, "available_metric_families": available,
            "has_gaps": bool(gaps), "order_lookup_requested": order_lookup,
            "instruction": "；".join(item["label"] + "：" + item["reason"] for item in rows),
            "completion_status": "partial" if gaps and available else "blocked" if gaps else "facts_available"}
