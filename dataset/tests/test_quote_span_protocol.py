import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from quote_span_protocol import SpanProtocolError, relocate  # noqa: E402


def label(quote='battery runs down quickly', start=4, end=28, status='labeled'):
    return {'status': status, 'polarity': 'negative', 'topics': ['durability'], 'reason': 'quoted text',
            'evidence': [{'start': start, 'end': end, 'quote': quote}]}


def test_development_off_by_one_preserves_every_other_field():
    value = label()
    before = copy.deepcopy(value)
    result, trace = relocate(value, 'The battery runs down quickly.')
    assert value == before
    assert result['evidence'][0] == {'start': 4, 'end': 29, 'quote': 'battery runs down quickly'}
    assert trace[0]['original_offset'] == {'start': 4, 'end': 28}
    assert result['topics'] == value['topics'] and result['polarity'] == value['polarity']


@pytest.mark.parametrize('text,quote,reason', [('foo foo', 'foo', 'not_unique'), ('aaa', 'aa', 'not_unique'),
                                             ('Foo', 'foo', 'absent'), ('a  b', 'a b', 'absent'),
                                             ('a b', 'a  b', 'absent'), ('é', 'e\u0301', 'absent'),
                                             ('text', '', 'empty'), ('text', 'missing', 'absent')])
def test_exact_only_boundaries(text, quote, reason):
    with pytest.raises(SpanProtocolError, match=reason):
        relocate(label(quote, 0, 1), text)


def test_unicode_multispan_characters_not_bytes():
    value = label('电池🔋', -100, 999)
    value['evidence'].append({'quote': 'café', 'start': 0, 'end': 2})
    result, receipts = relocate(value, '好 电池🔋 / café!')
    for span in result['evidence']:
        assert '好 电池🔋 / café!'[span['start']:span['end']] == span['quote']
    assert result['evidence'][0]['start'] == 2 and len(receipts) == 2


def test_duplicate_span_quote_is_rejected():
    value = label('only', 0, 4)
    value['evidence'].append(dict(value['evidence'][0]))
    with pytest.raises(SpanProtocolError, match='duplicate_quote'):
        relocate(value, 'only once')


@pytest.mark.parametrize('mutator', [lambda v: v.update(reason=None), lambda v: v.update(extra='bad'),
                                    lambda v: v.update(topics=[{}]), lambda v: v.update(polarity='fake'),
                                    lambda v: v['evidence'][0].update(start=True),
                                    lambda v: v['evidence'][0].update(end=2.0),
                                    lambda v: v['evidence'][0].update(quote=2),
                                    lambda v: v['evidence'][0].pop('end'),
                                    lambda v: v['evidence'][0].update(extra=1)])
def test_non_offset_illegal_structure_not_repaired(mutator):
    value = label('quote', 0, 1)
    mutator(value)
    with pytest.raises(SpanProtocolError):
        relocate(value, 'quote')


@pytest.mark.parametrize('status', ['uncertain', 'abstain'])
def test_no_uncertainty_promotion(status):
    value = label('quote', 0, 1, status)
    result, _ = relocate(value, 'quote')
    assert result['status'] == status and result['polarity'] == value['polarity']


def test_empty_evidence_labeled_rejected_uncertain_retained():
    value = label()
    value['evidence'] = []
    with pytest.raises(SpanProtocolError):
        relocate(value, 'text')
    value['status'] = 'uncertain'
    result, receipts = relocate(value, 'text')
    assert result['status'] == 'uncertain' and not receipts
