"""Bind redundant planner scope metadata to server-owned scope, never widen it."""
import re

from .scope import request_date_text, resolve_scope

VERSION = 'planner-query-scope-binding-v1'
ISO_RANGE = r'(?<!\d)(20\d{2}-\d{2}-\d{2})\s*(?:\.\.|至|到|—|–|~|to|through)\s*(20\d{2}-\d{2}-\d{2})(?!\d)'
ASIN = r'\bASIN\s*[:：=]?\s*([A-Za-z0-9]{10})(?![A-Za-z0-9])'


def bind_text_query(query, scope):
    original = query
    if not isinstance(query, str):
        raise ValueError('invalid_text_evidence_query')
    masked = request_date_text(query)
    removed = []
    ranges = list(re.finditer(ISO_RANGE, masked, re.IGNORECASE))
    parsed_text = re.sub(ISO_RANGE, lambda m: m.group(1) + '至' + m.group(2), query, flags=re.IGNORECASE)
    resolved = resolve_scope(parsed_text, today=scope.get('end') or '2000-01-01')
    if resolved['status'] != 'default':
        if (resolved['status'] != 'explicit' or len(ranges) != 1
                or resolved['start'] != scope.get('start') or resolved['end'] != scope.get('end')):
            raise ValueError('planner_text_query_must_not_override_frozen_dates')
        match = ranges[0]
        removed.append({'kind': 'same_frozen_date_range', 'start': match.start(), 'end': match.end(), 'value': query[match.start():match.end()]})
    for match in re.finditer(ASIN, masked, re.IGNORECASE):
        if match.group(1).upper() != str(scope.get('asin', '')).upper():
            raise ValueError('planner_text_query_must_not_override_frozen_object')
        removed.append({'kind': 'same_frozen_asin', 'start': match.start(), 'end': match.end(), 'value': query[match.start():match.end()]})
    if not removed:
        return query, None
    result = query
    for part in sorted(removed, key=lambda v: v['start'], reverse=True):
        result = result[:part['start']] + ' ' + result[part['end']:]
    result = re.sub(r'\s+', ' ', result).strip()
    if len(result) < 2 or resolve_scope(result, today=scope.get('end') or '2000-01-01')['status'] != 'default':
        raise ValueError('planner_query_has_no_unambiguous_content')
    return result, {'version': VERSION, 'original_query': original, 'content_query': result,
        'frozen_scope': {k: scope.get(k) for k in ('shop', 'asin', 'product_id', 'start', 'end')},
        'removed_exact_metadata': removed, 'scope_changed': False, 'content_inferred': False}
