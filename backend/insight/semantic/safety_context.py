"""Conservative safety context gate; uncertain language requires human review."""
import re


def safety_context(text, span):
    start = text.lower().find(span.lower())
    if start < 0 or not span:
        return "missing"
    left = max(text.rfind(mark, 0, start) for mark in (".", "!", "?", "\n", "。")) + 1
    ends = [text.find(mark, start + len(span)) for mark in (".", "!", "?", "\n", "。")]
    end = min((n for n in ends if n >= 0), default=len(text))
    sentence = text[left:end].lower()
    if re.search(r"\b(no|not|never|without|doesn't|didn't|isn't)\b|没有|并未|不会|不烫|不热", sentence):
        return "negated"
    if re.search(r"\b(if|might|may|could|would|worried|hope)\b|假如|如果|担心|可能", sentence):
        return "hypothetical"
    if re.search(r"\b(ignore|instruction|system prompt|create task|change price)\b|忽略指令|修改价格", sentence):
        return "instruction_like"
    return "asserted"
