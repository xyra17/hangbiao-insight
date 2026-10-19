"""Independent development examples, not frozen evaluation questions."""
import pytest

from insight.investigation.graph import evidence_driven_counter_query
from insight.semantic.retrieval import query_groups
from tests_v2.test_semantic import review_report  # noqa: F401


def search(client, shop, query, limit=10):
    response = client.post('/api/v1/evidence-search/', {
        'shop': str(shop.pk), 'child_asin': 'B000TEST01', 'query': query,
        'start': '2026-09-01', 'end': '2026-09-02', 'limit': limit,
    }, format='json')
    assert response.status_code == 200
    return response.data


@pytest.mark.django_db
@pytest.mark.parametrize('review_report', [[
    {'id': 'POSITIVE', 'text': 'The unit overheated.'},
    {'id': 'NEGATIVE', 'text': 'No overheating occurred; the temperature was normal.'},
]], indirect=True)
def test_generated_counter_query_can_retrieve_existing_negative_evidence(review_report, client):  # noqa: F811
    users, shops = review_report
    client.force_authenticate(users['a'])
    first = search(client, shops[0], 'overheating')
    assert {'POSITIVE', 'NEGATIVE'} <= {x['record_id'] for x in first['items']}
    query = evidence_driven_counter_query('overheating', [first])
    following = search(client, shops[0], query)
    print('FOLLOW_UP_QUERY', query, 'GROUPS', query_groups(query), 'HITS', [x['record_id'] for x in following['items']])
    assert 'NEGATIVE' in {x['record_id'] for x in following['items']}


@pytest.mark.django_db
@pytest.mark.parametrize('review_report', [[
    {'id': f'NEGATIVE-{n}', 'text': 'No overheating during charging. No overheating after use. No overheating overnight.'} for n in range(12)
] + [{'id': 'ACTUAL', 'text': 'The device experienced overheating during a prolonged supervised charging session on Tuesday afternoon.'}]], indirect=True)
def test_assertion_filter_does_not_drop_valid_record_beyond_candidate_limit(review_report, client):  # noqa: F811
    users, shops = review_report
    client.force_authenticate(users['a'])
    wide = search(client, shops[0], 'confirmed overheating', 50)
    small = search(client, shops[0], 'confirmed overheating', 10)
    print('ASSERTION_FILTER', 'wide', [x['record_id'] for x in wide['items']], 'small', [x['record_id'] for x in small['items']])
    assert 'ACTUAL' in {x['record_id'] for x in wide['items']}
    assert 'ACTUAL' in {x['record_id'] for x in small['items']}


@pytest.mark.django_db
@pytest.mark.parametrize('review_report', [[
    {'id': 'MIXED', 'text': 'No overheating occurred on Monday. Overheating occurred on Tuesday.'},
]], indirect=True)
def test_later_asserted_occurrence_is_not_suppressed_by_first_negated_occurrence(review_report, client):  # noqa: F811
    users, shops = review_report
    client.force_authenticate(users['a'])
    result = search(client, shops[0], 'confirmed overheating')
    print('REPEATED_TERM', result['match_status'], [x['record_id'] for x in result['items']])
    assert 'MIXED' in {x['record_id'] for x in result['items']}


@pytest.mark.parametrize('text,term', [
    ('No overheating on Monday. Overheating on Tuesday.', 'overheating'),
    ('Overheating on Tuesday. No overheating on Monday.', 'overheating'),
    ('周一没有过热。周二实际发生过热。', '过热'),
    ('Could it overheat? It did overheat during testing.', 'overheat'),
])
def test_every_occurrence_keeps_its_sentence_and_offsets(text, term):
    from insight.semantic.safety_context import occurrence_contexts, safety_context
    spans = occurrence_contexts(text, term)
    assert len(spans) == 2
    assert {row['context'] for row in spans} >= {'asserted'}
    assert safety_context(text, term) == 'asserted'
    assert all(text[row['start']:row['end']].lower() == term.lower() for row in spans)


@pytest.mark.django_db
@pytest.mark.parametrize('review_report', [[
    {'id': 'POSITIVE', 'text': 'The unit overheated.'},
    {'id': 'NEGATIVE', 'text': 'No overheating occurred; the temperature was normal.'},
]], indirect=True)
def test_structured_counter_intent_returns_actual_negative_evidence(review_report, client):  # noqa: F811
    from insight.investigation.graph import evidence_driven_counter_search
    users, shops = review_report
    client.force_authenticate(users['a'])
    initial = search(client, shops[0], 'overheating')
    intent = evidence_driven_counter_search('overheating', [initial])
    assert intent['query'] == 'overheating' and intent['evidence_context'] == 'negated'
    response = client.post('/api/v1/evidence-search/', {
        'shop': str(shops[0].pk), 'child_asin': 'B000TEST01', 'query': intent['query'],
        'evidence_context': intent['evidence_context'], 'start': '2026-09-01', 'end': '2026-09-02',
    }, format='json')
    assert response.status_code == 200
    assert [hit['record_id'] for hit in response.data['items']] == ['NEGATIVE']
    assert response.data['items'][0]['text'] == 'No overheating occurred; the temperature was normal.'


@pytest.mark.django_db
@pytest.mark.parametrize('review_report', [[
    {'id': f'NEGATIVE-{n}', 'text': 'No overheating. No overheating. No overheating.'} for n in range(25)
] + [{'id': 'ACTUAL', 'text': 'An actual overheating incident during supervised use.'}]], indirect=True)
def test_cost_cutoff_is_incomplete_instead_of_false_no_hits(review_report, client, monkeypatch):  # noqa: F811
    from insight.semantic import retrieval
    monkeypatch.setattr(retrieval, 'CONTEXT_CANDIDATE_BUDGET', 20)
    users, shops = review_report
    client.force_authenticate(users['a'])
    result = search(client, shops[0], 'confirmed overheating')
    assert result['retrieval_complete'] is False
    assert result['candidate_count'] == 20
    assert result['match_status'] == 'incomplete'
