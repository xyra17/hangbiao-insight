"""No model calls: licensing scope, leakage groups and silver receipt contracts."""
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import ai_silver_label as ai  # noqa: E402
import prepare_public_review_pilot as prep  # noqa: E402


def test_public_pilot_drops_gold_excludes_overlap_and_preserves_duplicate_groups(tmp_path):
    archive = tmp_path / 'uci.zip'
    with zipfile.ZipFile(archive, 'w') as package:
        package.writestr('sentiment labelled sentences/amazon_cells_labelled.txt',
                         'Works great today\t1\nWorks great today!\t0\nTerrible battery life\t0\nExcellent phone\t1\nAwful sound quality\t0\n')
    legacy = tmp_path / 'legacy.csv'
    legacy.write_text('title,text\n,Excellent phone\n')
    out = tmp_path / 'pilot'
    manifest = prep.prepare(archive, legacy, out, limit=4)
    rows = ai.load_rows(out / 'reviews.jsonl', out / 'manifest.json')
    assert manifest['source_rows'] == 5 and manifest['exact_or_lexical_overlap_rows_excluded'] == 1
    assert all('label' not in row and row['product_id'] is None and row['created_at'] is None for row in rows)
    dup = [row for row in rows if 'Works great today' in row['text']]
    assert len({row['split'] for row in dup}) == 1
    assert manifest['product_time_split_verified'] is False
    with pytest.raises(ValueError, match='unique'):
        prep.prepare(archive, legacy, out)


def test_redaction_and_lexical_limit():
    assert prep.redact('mail a@b.com or https://x.test phone +1 222 333 4444') == 'mail [EMAIL] or [URL] phone [PHONE_OR_NUMBER]'
    assert prep.similar(prep.shingles('This phone works really well'), prep.shingles('This phone works really well'))
    assert not prep.similar(set(), set())


@pytest.fixture
def pilot(tmp_path):
    row = {'review_id': 'a', 'text': 'Great phone', 'product_id': None, 'created_at': None, 'language': 'en', 'source': 'uci331-amazon', 'source_row': 1, 'duplicate_group': 'g1', 'split': 'development'}
    path = tmp_path / 'reviews.jsonl'
    path.write_text(json.dumps(row) + '\n')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'files': {'reviews.jsonl': ai.file_sha(path)}}))
    model = {'provider': 'openai', 'model': 'controlled-gpt', 'input_usd_per_million': '.15', 'output_usd_per_million': '.6', 'region': 'controlled', 'retention_acknowledgment': 'controlled only'}
    out = tmp_path / 'controlled-run'
    config = {'first_pass_models': [model, {**model, 'provider': 'deepseek', 'model': 'controlled-ds'}], 'review_model': model, 'approved': True, 'approved_run_id': out.name, 'approved_output_path': str(out.resolve()), 'input_sha256': ai.file_sha(path), 'manifest_sha256': ai.file_sha(manifest), 'currency': 'USD', 'allowed_fields': ['text'], 'approval_reference': 'controlled test NOT external approval', 'transfer_terms_confirmed': True, 'max_total_usd': '1', 'max_call_usd': '.01', 'max_output_tokens': 700, 'max_request_bytes': 8000}
    return [row], path, manifest, out, config


def label(polarity='positive', status='labeled'):
    return {'status': status, 'polarity': polarity, 'topics': ['function'], 'evidence': [{'start': 0, 'end': 5, 'quote': 'Great'}], 'reason': 'text evidence'}


def response(value, usage=True):
    return {'id': 'controlled-id', 'model': 'controlled-returned-model', 'choices': [{'message': {'content': json.dumps(value)}}], 'usage': {'prompt_tokens': 50, 'completion_tokens': 60} if usage else None}


def test_blind_first_passes_and_receipts(pilot):
    rows, path, manifest, out, config = pilot
    calls = []
    def dispatch(model, msgs, conf):
        calls.append(msgs)
        durable = json.loads((out / f'call-{len(calls):04d}.json').read_text())
        assert durable['status'] == 'dispatch_started_charge_unknown'
        assert 'first_passes' not in msgs[-1]['content']
        assert set(json.loads(msgs[-1]['content'])) == {'text'}
        return response(label())
    summary = ai.run(rows, config, path, manifest, out, dispatch=dispatch, credential_check=False)
    assert summary['calls'] == 2 and summary['first_agreements'] == 1
    assert summary['independence'] == 'two_provider_first_pass' and not summary['accuracy_claim']
    receipt = json.loads((out / 'call-0001.json').read_text())
    assert receipt['reported_model'] == 'controlled-returned-model' and receipt['label_source'] == 'ai_generated/silver'
    with pytest.raises(ValueError, match='unique'):
        ai.run(rows, config, path, manifest, out, dispatch=dispatch, credential_check=False)
    assert len(calls) == 2


def test_disagreement_review_and_unknown_cost(pilot):
    rows, path, manifest, out, config = pilot
    calls = []
    def dispatch(model, msgs, conf):
        calls.append(msgs)
        assert ('first_passes' in msgs[-1]['content']) == (len(calls) == 3)
        return response(label('negative' if len(calls) == 2 else 'positive'), usage=len(calls) != 1)
    summary = ai.run(rows, config, path, manifest, out, dispatch=dispatch, credential_check=False)
    assert summary['calls'] == 3 and summary['first_agreements'] == 0 and summary['actual_cost_usd'] is None


@pytest.mark.parametrize('mutate', [lambda c: c.update(approved=False), lambda c: c.update(approved_run_id='old'), lambda c: c.update(input_sha256='wrong'), lambda c: c.update(max_total_usd='.0001'), lambda c: c.update(max_call_usd='NaN'), lambda c: c.update(allowed_fields=['text', 'user_id']), lambda c: c.update(transfer_terms_confirmed=False)])
def test_missing_or_bad_approval_never_dispatches(pilot, mutate):
    rows, path, manifest, out, config = pilot
    mutate(config)
    with pytest.raises((ValueError, ArithmeticError)):
        ai.run(rows, config, path, manifest, out, dispatch=lambda *args: pytest.fail('dispatch forbidden'), credential_check=False)
    assert not out.exists()


def test_invalid_span_and_gold_fields_rejected(pilot):
    rows, path, manifest, out, config = pilot
    value = label()
    value['evidence'][0]['quote'] = 'invented'
    with pytest.raises(ValueError, match='span'):
        ai.validate_label(value, rows[0]['text'])
    rows[0]['expected'] = 'positive'
    path.write_text(json.dumps(rows[0]) + '\n')
    manifest.write_text(json.dumps({'files': {'reviews.jsonl': ai.file_sha(path)}}))
    with pytest.raises(ValueError, match='unexpected'):
        ai.load_rows(path, manifest)


def test_single_model_repeated_is_not_dual_model(pilot):
    rows, _, _, _, config = pilot
    config['first_pass_models'][1] = config['first_pass_models'][0]
    assert ai.plan(rows, config)['independence'] == 'single_model_repeated_pass'


def test_lost_receipt_no_automatic_retry(pilot):
    rows, path, manifest, out, config = pilot
    calls = []
    def dispatch(*args):
        calls.append(1)
        raise TimeoutError('controlled')
    with pytest.raises(RuntimeError, match='provider_blocked_no_automatic_retry'):
        ai.run(rows, config, path, manifest, out, dispatch=dispatch, credential_check=False)
    assert len(calls) == 1
    assert json.loads((out / 'call-0001.json').read_text())['error_type'] == 'TimeoutError'
    ledger = json.loads((out / 'budget-ledger.json').read_text())
    assert ledger['dispatch_allowances_consumed'] == 1 and ledger['reserved_upper_usd'] != '0'


def test_silver_score_cannot_satisfy_human_gate(pilot):
    from score_silver_predictions import score
    rows, _, _, _, _ = pilot
    input_sha = ai.digest({'text': rows[0]['text']})
    silver = {'labels': [{'review_id': 'a', 'input_sha256': input_sha, 'split': 'development', 'label_source': 'ai_generated/silver', 'label': label()}]}
    prediction = {'agent_model': 'controlled-other', 'execution_report_sha256': 'a' * 64, 'predictions': [{'review_id': 'a', 'input_sha256': input_sha, 'label': label()}]}
    result = score(rows, silver, prediction)
    assert result['status'] == 'silver_comparison_only' and not result['original_human_gate_satisfied']
    assert result['splits']['development']['coverage'] == 1
    assert result['splits']['holdout']['topic_macro_f1_vs_silver'] is None
    prediction['predictions'][0]['input_sha256'] = 'wrong'
    with pytest.raises(ValueError, match='binding'):
        score(rows, silver, prediction)
