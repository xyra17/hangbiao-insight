from __future__ import annotations

_OVERCLAIM = ("导致", "证明", "主要驱动因素", "根本原因", "就是因为")


def sanitize_final_answer(answer: str | None, *, insufficient_evidence: bool = False) -> tuple[str, bool]:
    if insufficient_evidence:
        return ("当前评论证据不足以解释本次经营异常，系统不做评论归因。建议进一步检查退款原因编码、售后/退货记录、批次质量、价格促销、广告流量、库存/缺货和数据质量。", answer != "当前评论证据不足以解释本次经营异常，系统不做评论归因。建议进一步检查退款原因编码、售后/退货记录、批次质量、价格促销、广告流量、库存/缺货和数据质量。")
    if not answer:
        return ("现有证据仅支持相关信号，不能证明评论是异常的原因或主要驱动因素。", True)
    if any(word in answer for word in _OVERCLAIM):
        return ("评论与经营指标异常在时间上同步变化，并提供了支持性证据；这只能说明相关信号，不能证明评论是异常的原因或主要驱动因素。", True)
    return answer, False
