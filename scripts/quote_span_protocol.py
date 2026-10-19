"""Pure exact character anchoring. No fuzzy matching, text normalization or model calls."""
import copy

from ai_silver_label import validate_label

VERSION = 'unique-exact-quote-offsets-v1'


class SpanProtocolError(ValueError):
    pass


def relocate(value, text):
    if not isinstance(value, dict) or not isinstance(value.get('evidence'), list):
        raise SpanProtocolError('invalid_structure')
    candidate = copy.deepcopy(value)
    receipts = []
    seen = set()
    for index, span in enumerate(value['evidence']):
        if not isinstance(span, dict) or set(span) != {'start', 'end', 'quote'}:
            raise SpanProtocolError('invalid_span_structure')
        if type(span['start']) is not int or type(span['end']) is not int:
            raise SpanProtocolError('invalid_original_offset_type')
        quote = span['quote']
        if not isinstance(quote, str) or not quote:
            raise SpanProtocolError('empty_or_invalid_quote')
        if quote in seen:
            raise SpanProtocolError('duplicate_quote_in_evidence')
        seen.add(quote)
        start = text.find(quote)
        if start < 0:
            raise SpanProtocolError('quote_absent_exact_match')
        # Advance by one character: overlapping occurrences also make a quote ambiguous.
        if text.find(quote, start + 1) >= 0:
            raise SpanProtocolError('quote_not_unique')
        end = start + len(quote)
        candidate['evidence'][index] = {'start': start, 'end': end, 'quote': quote}
        receipts.append({'span_index': index, 'quote': quote, 'original_offset': {'start': span['start'], 'end': span['end']},
                         'derived_offset': {'start': start, 'end': end}, 'changed': (span['start'], span['end']) != (start, end),
                         'reason': 'unique_exact_quote_occurrence', 'rule_version': VERSION})
    try:
        validate_label(candidate, text)
    except (ValueError, TypeError, KeyError) as exc:
        raise SpanProtocolError('invalid_non_offset_structure_or_decision') from exc
    # Every field other than start/end must remain byte-for-byte equal as JSON values.
    restored = copy.deepcopy(candidate)
    for index, span in enumerate(value['evidence']):
        restored['evidence'][index]['start'] = span['start']
        restored['evidence'][index]['end'] = span['end']
    if restored != value:
        raise SpanProtocolError('unexpected_non_offset_change')
    return candidate, receipts
