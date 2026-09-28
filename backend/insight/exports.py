"""Authenticated, uncached downloads generated from frozen report material."""
import csv
import io
import json
from html import escape

from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import AuditEntry
from .services import audit


def download(content, content_type, filename):
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store, private"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return response


def report_download(request, report, file_type):
    if file_type not in {"json", "html"}:
        raise ValidationError("报告支持 HTML 或 JSON 导出。")
    snapshot, semantic = report.metric_snapshot, report.semantic_run
    document = {
        "exported_at": timezone.now().isoformat(), "report_id": str(report.pk),
        "report_version": report.report_version, "publication_key": report.publication_key,
        "previous_report": str(report.previous_report_id) if report.previous_report_id else None,
        "input_versions": report.input_versions,
        "created_at": report.created_at.isoformat(), "status": report.status,
        "shop_id": str(report.shop_id), "marketplace": report.shop.marketplace,
        "timezone": report.shop.timezone, "child_asin": report.child_asin,
        "product_id": str(report.product_id) if report.product_id else None,
        "data_cutoff": snapshot.local_date.isoformat() if snapshot else None,
        "metric_source_manifest": snapshot.source_manifest if snapshot else {},
        "semantic_source_manifest": semantic.source_manifest if semantic else {},
        "rule_version": snapshot.rule_version if snapshot else None,
        "prompt_version": semantic.prompt_version if semantic else None,
        "taxonomy_version": semantic.taxonomy_version if semantic else None,
        "payload": report.payload,
    }
    text = json.dumps(document, ensure_ascii=False, indent=2)
    if file_type == "html":
        summary = escape(str(report.payload.get("summary", "")))
        evidence = "".join(
            f'<li>{escape(str(claim.get("evidence_span", "")))} '
            f'（评论：{escape(str(claim.get("review_id", "未标记")))}）</li>'
            for claim in report.payload.get("claims", [])
        )
        signals = "".join(f'<li>{escape(str(signal.get("title", signal.get("metric", "待核查"))))}</li>' for signal in report.payload.get("signals", []))
        limitations = "".join(f'<li>{escape(str(item))}</li>' for item in report.payload.get("limitations", []) + report.payload.get("gaps", []))
        text = (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>商品调查报告</title><style>body{max-width:960px;margin:40px auto;padding:0 24px;'
            'font:16px/1.6 sans-serif;color:#222}pre{white-space:pre-wrap;overflow-wrap:anywhere;'
            'background:#f5f5f5;padding:20px}</style><body>'
            f'<h1>{escape(report.child_asin)} 调查报告 · v{report.report_version}</h1><p>{summary}</p>'
            f'<h2>异常信号</h2><ul>{signals or "<li>暂无已发布信号</li>"}</ul>'
            f'<h2>评论证据</h2><ul>{evidence or "<li>暂无通过校验的评论证据</li>"}</ul>'
            f'<h2>限制与待补数据</h2><ul>{limitations or "<li>请结合数据覆盖与业务变化核查。</li>"}</ul>'
            f'<details><summary>数据版本、引用与完整材料（核对用）</summary><pre>{escape(text)}</pre></details></body></html>'
        )
    audit(request.user, report.shop, "report.exported", report.pk,
          after={"format": file_type, "report_version": report.report_version}, request_id=request.request_id)
    return download(text, "text/html; charset=utf-8" if file_type == "html" else "application/json; charset=utf-8", f"report-{report.pk}-v{report.report_version}.{file_type}")


def safe_csv(value):
    text = "" if value is None else str(value)
    # Spreadsheet formula parsing also ignores leading whitespace and a BOM.
    if text.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def events_download(request, queryset):
    records = list(queryset.select_related("shop__workspace"))
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["事件ID", "店铺", "站点", "子ASIN", "商品ID", "异常类型", "标题", "状态", "优先级", "数据截至日期", "事件版本", "导出时间"])
    timestamp = timezone.now().isoformat()
    for event in records:
        writer.writerow([safe_csv(value) for value in [event.pk, event.shop.name, event.shop.marketplace, event.child_asin, event.product_id, event.family, event.title, event.status, event.priority, event.last_cutoff, event.version, timestamp]])
    # One audit per workspace prevents a workspace administrator from seeing another workspace's IDs.
    for workspace in {event.shop.workspace_id for event in records}:
        AuditEntry.objects.create(actor=request.user, workspace_id=workspace, action="events.exported",
            target_id="events", after={"format": "csv", "event_versions": {str(event.pk): event.version for event in records if event.shop.workspace_id == workspace}}, request_id=request.request_id)
    return download("\ufeff" + output.getvalue(), "text/csv; charset=utf-8", "events.csv")
