"""Occurrence-bound contexts; mixed documents retain every original span."""
import re


def occurrence_contexts(text, span):
    if not span:
        return []
    pattern = re.escape(span)
    if re.fullmatch(r"[a-z0-9 ]+", span, re.I):
        pattern = r"(?<!\w)" + pattern + r"(?!\w)"
    boundaries = list(re.finditer(r"[.!?;\n。；]|\bbut\b|然而|但是|但", text, re.I))
    result = []
    for match in re.finditer(pattern, text, re.I):
        left = max((b.end() for b in boundaries if b.end() <= match.start()), default=0)
        end = min((b.start() for b in boundaries if b.start() >= match.end()), default=len(text))
        sentence = text[left:end].lower()
        if re.search(r"\b(no|not|never|without|denies|denied|doesn't|didn't|isn't)\b|没有|并未|不会|不烫|不热", sentence):
            context = "negated"
        elif re.search(r"\b(if|might|may|could|would|worried|hope|hypothetical)\b|假如|如果|担心|可能", sentence):
            context = "hypothetical"
        elif re.search(r"\b(ignore|instruction|system prompt|create task|change price)\b|忽略指令|修改价格", sentence):
            context = "instruction_like"
        else:
            context = "asserted"
        result.append({"start": match.start(), "end": match.end(), "sentence_start": left,
                       "sentence_end": end, "text": text[left:end], "context": context})
    return result


def safety_context(text, span):
    contexts = {row["context"] for row in occurrence_contexts(text, span)}
    # An actual assertion requires review even when another occurrence denies it.
    return next((kind for kind in ("asserted", "hypothetical", "negated", "instruction_like") if kind in contexts), "missing")


def record_contexts(*, title="", text="", terms=()):
    """Preserve original offsets separately for title and body, never concatenate."""
    return [{**context, "source_field": field}
            for field, value in (("title", title), ("text", text))
            for term in terms for context in occurrence_contexts(value, term)]
