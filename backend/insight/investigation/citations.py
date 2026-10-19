"""Typed, immutable evidence references. Validation never recomputes ratios."""
import hashlib
import json

VERSION = "investigation-citations-v2"
METRIC_FIELDS = ("type", "snapshot_id", "snapshot_fingerprint", "source_manifest_sha256", "shop_id", "child_asin",
                 "product_id", "path", "window", "start", "end", "value", "unit", "denominator")
METRIC_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {key: {"type": ["string", "null"] if key in {"product_id", "start", "end", "denominator"} else "string"} for key in METRIC_FIELDS},
    "required": list(METRIC_FIELDS),
}
METRIC_SCHEMA["properties"]["type"] = {"type": "string", "enum": ["metric"]}
TEXT_SCHEMA = {"type": "object", "additionalProperties": False,
               "properties": {"fact_id": {"type": "string"}, "quote": {"type": "string"},
                              "source_field": {"type": "string", "enum": ["title", "text"]}}, "required": ["fact_id", "quote", "source_field"]}
CITATION_SCHEMA = {"anyOf": [TEXT_SCHEMA, METRIC_SCHEMA]}
EXCLUDED_KEYS = {"raw_row_ids", "order_ids", "order_evidence", "signals", "wilson_95", "difference_interval_95"}


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def scalar(value):
    return "null" if value is None else str(value).lower() if isinstance(value, bool) else str(value)


def metric_catalog(*, snapshot_id, snapshot_fingerprint, source_manifest, shop_id, child_asin, product_id, metrics):
    identity = {"type": "metric", "snapshot_id": str(snapshot_id), "snapshot_fingerprint": snapshot_fingerprint,
                "source_manifest_sha256": sha(source_manifest), "shop_id": str(shop_id), "child_asin": child_asin,
                "product_id": str(product_id) if product_id else None}
    atoms = []

    def walk(node, path="", window="snapshot", start=None, end=None, currency=None, denominator=None):
        if len(atoms) >= 512:
            return
        if isinstance(node, dict):
            start, end = node.get("start", node.get("date", start)), node.get("end", node.get("date", end))
            currency = node.get("currency", currency)
            denominator = node.get("denominator", node.get("sessions", denominator))
            for key, value in node.items():
                if key not in EXCLUDED_KEYS:
                    walk(value, f"{path}.{key}" if path else key, key if key in {"baseline", "current"} else window,
                         start, end, currency, denominator)
        elif isinstance(node, list):
            for index, value in enumerate(node[:20]):
                walk(value, f"{path}.{index}", window, start, end, currency, denominator)
        elif isinstance(node, (str, int, float, bool)) or node is None:
            key = path.rsplit(".", 1)[-1]
            unit = "fact"
            divisor = None
            if key == "rate":
                unit, divisor = "fraction", scalar(denominator) if denominator is not None else None
            elif "percent" in key or key.endswith("_pct") or key == "buybox_baseline_daily_mean":
                unit, divisor = "percent", scalar(denominator) if denominator is not None else None
            elif key.endswith("_pp"):
                unit = "percentage_point"
            elif key in {"amount", "net_amount", "sales_amount"}:
                unit = currency or "unknown_currency"
            elif ".amount_by_currency." in path:
                unit = key
            elif key in {"rating", "mean_rating"}:
                unit = "stars"
            elif key == "sessions":
                unit = "sessions"
            elif key in {"ordered_units", "available", "total", "units"}:
                unit = "units"
            elif "days" in key:
                unit = "days"
            elif key in {"event_count", "reversal_count", "unclassified_records"}:
                unit = "records"
            elif key in {"denominator", "numerator"} or "orders" in key:
                unit = "orders"
            atoms.append({**identity, "path": path, "window": window, "start": start, "end": end,
                          "value": scalar(node), "unit": unit, "denominator": divisor})

    walk(metrics)
    return {"version": VERSION, "provenance": metrics.get("fixture_provenance", "calculator_frozen_snapshot"), "identity": identity, "atoms": atoms, "catalog_complete": len(atoms) < 512}


def validate_citations(citations, evidence):
    if not isinstance(citations, list):
        raise ValueError("citations must be an array")
    texts = {item.get("fact_id"): item for entry in evidence if entry.get("type") == "text_retrieval"
             for item in entry.get("items", []) if item.get("match_kind") == "relevant_hit"}
    atoms = {json.dumps(atom, sort_keys=True): atom for entry in evidence if entry.get("type") == "metric_snapshot"
             for atom in entry.get("metric_evidence", {}).get("atoms", [])}
    explicit_scopes = [entry["request_scope"] for entry in evidence if (entry.get("request_scope") or {}).get("status") == "explicit"]
    requested = explicit_scopes[0] if explicit_scopes else None
    for citation in citations:
        if not isinstance(citation, dict):
            raise ValueError("citation object required")
        if citation.get("type") == "metric":
            if set(citation) != set(METRIC_FIELDS) or json.dumps(citation, sort_keys=True) not in atoms:
                raise ValueError("metric citation differs from frozen authorized atom")
            if requested and not citation["path"].endswith((".status", ".reason", ".start", ".end")):
                if not citation.get("start") or not citation.get("end") or not (requested["start"] <= citation["start"] <= citation["end"] <= requested["end"]):
                    raise ValueError("metric citation outside requested date scope")
            if citation["unit"] in {"fraction", "percent"} and citation["denominator"] is None:
                # Daily mean percentages without a ratio denominator are distinct facts.
                if "unit_session_percent" in citation["path"] or citation["path"].endswith(".rate"):
                    raise ValueError("ratio citation requires the observed denominator")
        else:
            if requested and citation.get("fact_id") in texts and not (requested["start"] <= str(texts[citation["fact_id"]].get("business_date", "")) <= requested["end"]):
                raise ValueError("original citation outside requested date scope")
            field = citation.get("source_field", "text")
            if (set(citation) not in ({"fact_id", "quote"}, {"fact_id", "quote", "source_field"})
                    or field not in {"title", "text"}
                    or not isinstance(citation.get("fact_id"), str)
                    or citation["fact_id"] not in texts
                    or not isinstance(citation.get("quote"), str)
                    or not citation["quote"].strip()
                    or citation["quote"] not in texts[citation["fact_id"]].get(field, "")):
                raise ValueError("invalid original citation")
    return citations


def recheck_metric_evidence(run, evidence):
    """Final DB authority check: frozen snapshot, current source and object grant."""
    from rest_framework.exceptions import ValidationError

    from insight.access import require_role
    from insight.investigation.policy import TOOL_POLICIES
    from insight.metrics.facts import authorize_object, source_manifest
    from insight.models import InvestigationRun, MetricSnapshot

    run = InvestigationRun.objects.get(pk=run.pk)
    for entry in evidence:
        if not entry.get("metric_evidence"):
            continue
        identity = entry["metric_evidence"]["identity"]
        require_role(run.requested_by, run.shop, *TOOL_POLICIES["metric_snapshot"]["roles"])
        authorize_object(run.requested_by, run.shop, run.child_asin, run.product_id)
        snapshot = MetricSnapshot.objects.filter(pk=identity["snapshot_id"], shop=run.shop,
                                                child_asin=run.child_asin, product_id=run.product_id).first()
        if snapshot is None or str(snapshot.pk) != str(run.input_versions.get("metric_snapshot")) or snapshot.source_manifest != run.input_versions.get("source_manifest") or source_manifest(run.shop) != snapshot.source_manifest or snapshot.rule_version != run.input_versions.get("rule_version") or snapshot.payload.get("mapping_versions", {}) != run.input_versions.get("mapping_versions", {}):
            raise ValidationError("指标引用的对象、冻结快照或当前来源版本不匹配。")
        catalog = metric_catalog(snapshot_id=snapshot.pk, snapshot_fingerprint=snapshot.fingerprint,
                                 source_manifest=snapshot.source_manifest, shop_id=snapshot.shop_id,
                                 child_asin=snapshot.child_asin, product_id=snapshot.product_id, metrics=snapshot.payload)
        if catalog != entry["metric_evidence"]:
            raise ValidationError("指标引用与服务端冻结快照不一致。")
