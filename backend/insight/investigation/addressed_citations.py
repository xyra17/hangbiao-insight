"""Select server-owned original references; never repair or normalize model quotes."""
import copy
import hashlib
import json

VERSION = 'investigation-addressed-citations-v1'


def address_observations(observations):
    view = copy.deepcopy(observations)
    bindings = {}
    by_value = {}

    def reference(citation):
        key = json.dumps(citation, sort_keys=True, ensure_ascii=False)
        if key not in by_value:
            ref = 'ref_' + str(len(bindings) + 1)
            by_value[key] = ref
            bindings[ref] = copy.deepcopy(citation)
        return by_value[key]

    for observation in view:
        data = observation.get('data', {})
        if data.get('type') == 'text_retrieval':
            for item in data.get('items', []):
                refs = {}
                if item.get('match_kind') == 'relevant_hit':
                    for field in ('title', 'text'):
                        original = item.get(field)
                        if isinstance(original, str) and original.strip():
                            refs[field] = reference({'fact_id': item['fact_id'], 'quote': original, 'source_field': field})
                item['citation_refs'] = refs
        elif data.get('type') == 'metric_snapshot':
            catalog = data.get('metric_evidence', {})
            catalog['atoms'] = [{'citation_ref': reference(atom), 'atom': atom} for atom in catalog.get('atoms', [])]
    return view, bindings


def citation_schema(bindings):
    # No evidence means an empty array, with no selectable fake source.
    if not bindings:
        return {'type': 'array', 'maxItems': 0, 'items': {'type': 'object',
            'additionalProperties': False, 'properties': {'citation_ref': {'type': 'string'}},
            'required': ['citation_ref']}}
    return {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
        'properties': {'citation_ref': {'type': 'string', 'enum': list(bindings)}},
        'required': ['citation_ref']}}


def bind_answer(answer, bindings):
    if not isinstance(answer, dict) or not isinstance(answer.get('citations'), list):
        raise ValueError('addressed_terminal_required')
    result = copy.deepcopy(answer)
    citations = []
    receipts = []
    for item in answer['citations']:
        if not isinstance(item, dict) or set(item) != {'citation_ref'} or item['citation_ref'] not in bindings:
            raise ValueError('citation_reference_not_in_authorized_request')
        citation = copy.deepcopy(bindings[item['citation_ref']])
        citations.append(citation)
        receipts.append({'citation_ref': item['citation_ref'], 'original_sha256': hashlib.sha256(
            json.dumps(citation, sort_keys=True, ensure_ascii=False).encode()).hexdigest(), 'citation': citation})
    result['citations'] = citations
    return result, receipts
