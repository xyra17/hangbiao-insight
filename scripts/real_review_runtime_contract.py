"""Validate the declared real-review replay window before any model dispatch."""

from datetime import date


def declared_date_window(plan, corpus, investigations):
    from insight.investigation.scope import resolve_scope

    window = plan.get('date_scope')
    if not isinstance(window, dict) or set(window) != {'start', 'end'}:
        raise ValueError('declared_real_review_date_scope_required')
    start, end = date.fromisoformat(window['start']), date.fromisoformat(window['end'])
    if not 0 <= (end - start).days < 366:
        raise ValueError('real_review_date_scope_out_of_bounds')
    if any(not start <= date.fromisoformat(row['business_date']) <= end for row in corpus):
        raise ValueError('real_review_original_outside_declared_scope')
    for row in investigations:
        resolved = resolve_scope(row['query'], today=end)
        if resolved['status'] != 'explicit' or resolved['start'] != str(start) or resolved['end'] != str(end):
            raise ValueError('real_review_investigation_index_date_scope_mismatch')
    return str(start), str(end)
