"""Read-only checks of known CSV revision identities; never writes facts."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand

from insight.models import FactRetraction, OrderLine, RefundTransaction, ReturnLifecycle, SalesTrafficDaily

from ._acceptance import require_isolated_acceptance_environment


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--state", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)

    def handle(self, *args, **options):
        require_isolated_acceptance_environment()
        state = json.loads(options["state"].read_text())
        sales = SalesTrafficDaily.objects.filter(source_id=state["sources"]["sales_traffic"], child_asin="B07H2V5YLH", business_date="2018-11-05").order_by("revision")
        returns = ReturnLifecycle.objects.filter(source_id=state["sources"]["returns"], record_id="RT-TRANSITION-B07H2V5YLH").order_by("revision")
        refund = RefundTransaction.objects.filter(source_id=state["sources"]["refunds"], record_id="RF-LATE-ARRIVAL-B07H2V5YLH").latest("revision")
        candidate = next(v["batch_id"] for k,v in state["files"].items() if k.endswith("order_retraction_candidate.csv"))
        result = {"latest_sales_sessions": sales.last().sessions, "sales_history": list(sales.values("revision", "sessions", "batch_id", "raw_row_id")),
                  "return_status": returns.last().status, "return_history_count": returns.count(), "return_history": list(returns.values("revision", "status", "business_date", "batch_id", "raw_row_id")),
                  "late_refund_amount": str(refund.amount), "late_refund_original_line": refund.order_line_id,
                  "retraction_count": FactRetraction.objects.filter(batch_id=candidate).count(), "retracted_original_retained": OrderLine.objects.filter(batch_id=candidate).exists()}
        options["output"].write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        self.stdout.write("Read-only lifecycle audit completed.")
