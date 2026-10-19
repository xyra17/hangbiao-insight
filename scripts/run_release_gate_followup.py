"""Scoped gate repair verification; preserve frozen evidence and reject unrelated source changes."""
import argparse
import copy
import json
import os
import re
from pathlib import Path

from reconcile_release_evidence import quality_checks, recompute_quality
from release_acceptance import (
    ROOT,
    digest,
    gate,
    initialize,
    save,
    source_fingerprint,
    verified_assessment,
)

ALLOWED = {'scripts/reconcile_release_evidence.py', 'scripts/run_release_gate_followup.py',
           'scripts/prepare_independent_label_workpack.py', 'dataset/tests/test_quality_state_mapping.py',
           'dataset/tests/test_independent_label_workpack.py'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--test-log', type=Path, required=True)
    args = parser.parse_args()
    previous, folder = args.previous.resolve(), args.output.resolve()
    if previous == folder or not folder.is_relative_to(ROOT / '.local/acceptance-artifacts'):
        parser.error('use a new local acceptance directory')
    old = json.loads((previous / 'release_acceptance_manifest.json').read_text())
    old_hashes = json.loads((previous / 'source_hashes.json').read_text())
    source, hashes = source_fingerprint()
    changed = sorted(name for name in set(old_hashes) | set(hashes) if old_hashes.get(name) != hashes.get(name))
    if set(changed) - ALLOWED:
        raise ValueError('unreviewed changes require a separate impact review: ' + str(set(changed) - ALLOWED))
    test_log = args.test_log.resolve()
    text = test_log.read_text()
    matches = re.findall(r'(\d+) passed', text)
    if not matches or re.search(r'\d+ failed|ERROR collecting', text):
        raise ValueError('new dataset regression not passed')
    checks = []
    for name, task in old['tasks'].items():
        for evidence in task.get('evidence', []):
            if digest(Path(evidence['path'])) != evidence['sha256']:
                raise ValueError('historical evidence changed: ' + evidence['path'])
            checks.append(evidence)
        if task['status'] == 'passed' and name not in {'inventory', 'validate04'}:
            reason = verified_assessment(task, old['source_sha256'], name)
            if reason:
                raise ValueError('invalid previous assessment: ' + name + ':' + reason)
    manifest = initialize(folder, old['as_of'])
    impact = {'status': 'passed', 'previous_source_sha256': old['source_sha256'], 'current_source_sha256': source,
              'changed_files': changed, 'review': 'Gate reconciliation and offline labeling inventory/tests only. Backend, frontend, runtime evaluation, source-data import, fault paths and configuration unchanged by hash.',
              'reexecuted': ['full dataset tools including quality state propagation', 'bound runtime/adapter/scorer/gold component recomputation', 'current candidate archive/image-source verification'],
              'reused': 'Historical API/worker/browser/backend/recovery/capacity results retain their original source/date/environment; unchanged dependencies reviewed, no new runtime claim.',
              'validated_evidence_references': checks}
    save(folder / 'impact-review.json', impact)
    for name, task in old['tasks'].items():
        manifest['tasks'][name] = copy.deepcopy(task)
        if task['status'] != 'passed' or name in {'candidate', 'default_quality'}:
            continue
        if name in {'inventory', 'validate04'}:
            manifest['tasks'][name]['reuse_review'] = str(folder / 'impact-review.json')
            continue
        prior_path = Path(task['assessment']['path'])
        prior = json.loads(prior_path.read_text())
        report = {**prior, 'source_sha256': source,
                  'reuse_provenance': {'previous_assessment': str(prior_path), 'sha256': digest(prior_path),
                                       'previous_source_sha256': old['source_sha256'], 'runtime_not_rerun': True,
                                       'impact_review': str(folder / 'impact-review.json')}}
        report['checks'] = {**prior['checks'], 'reviewed_runtime_dependencies_unchanged': True}
        if name == 'integration':
            report['checks']['new_dataset_regression_passed'] = True
            report['actual'] = {'historical_unchanged_runtime': prior['actual'], 'new_dataset_tests': int(matches[-1])}
            manifest['tasks'][name]['evidence'].append({'path': str(test_log), 'sha256': digest(test_log)})
        path = folder / (name + '-assessment.json')
        save(path, report)
        manifest['tasks'][name]['assessment'] = {'path': str(path), 'sha256': digest(path)}
        manifest['tasks'][name]['evidence'].append({'path': str(folder / 'impact-review.json'), 'sha256': digest(folder / 'impact-review.json')})
    bundle = json.loads((previous / 'quality-bundle-final.json').read_text())
    config = {
        'status': 'blocked_external', 'dependency': 'E2/E3',
        'local_configuration': {key: os.getenv(key, default) for key, default in [
            ('INVESTIGATION_PLANNER_PROVIDER', 'deterministic'),
            ('EVIDENCE_RELEVANCE_ENABLED', '0'), ('EVIDENCE_RELEVANCE_PROVIDER', ''),
            ('INSIGHT_ENABLED_MODEL_PROVIDERS', '')]},
        'evaluated_runtime_planner': bundle.get('planner_provider'),
        'evaluated_configuration_scope': 'historical default lexical/deterministic fallback only',
        'production_configuration_approved': False,
        'real_provider_run': False,
        'configuration_boundary': 'Enabled production semantic/planner requires its own authorized runtime, matching configuration fingerprint and independent labels. Fallback evidence never certifies that branch.',
        'current_image_digests': json.loads((folder / 'candidate/candidate.json').read_text())['images'],
        'unchanged_runtime_dependency_hashes': {name: value for name, value in hashes.items() if name.startswith(('backend/', 'frontend/'))},
        'local_env_file_sha256': digest(ROOT / '.env.local') if (ROOT / '.env.local').is_file() else None,
        'secret_values_recorded': False,
    }
    save(folder / 'configuration-binding.json', config)
    workpack = folder / 'independent-label-workpack.json'
    label_pack = json.loads(workpack.read_text())
    index = Path(label_pack['candidate_index']['path'])
    if digest(index) != label_pack['candidate_index']['sha256']:
        raise ValueError('label workpack index changed')
    for name, paths in [('real_quality', [folder / 'configuration-binding.json', workpack, index]),
                        ('input_audit', [workpack, index])]:
        manifest['tasks'][name]['evidence'].extend({'path': str(path), 'sha256': digest(path)} for path in paths)
    matrix = json.loads((previous / 'requirements-matrix.json').read_text())
    matrix['current_scope'] = {'source_sha256': source, 'runtime_evidence': str(previous),
                             'runtime_scope': 'unchanged dependencies; historical runtime evidence retained',
                             'current_gate_repair': 'component ready/local passed distinct from real model and full release; actual quality still failed'}
    for row in matrix['rows']:
        row['evidence_run'] = str(previous)
        row['evidence_reuse_review'] = str(folder / 'impact-review.json')
    save(folder / 'requirements-matrix.json', matrix)
    manifest['tasks']['frac']['evidence'].append({'path': str(folder / 'requirements-matrix.json'), 'sha256': digest(folder / 'requirements-matrix.json')})
    development_path = ROOT / '.local/third-review-fixes-20261007/retrieval_strategy_experiment.json'
    development = json.loads(development_path.read_text())
    diagnosis = {'status': 'failed', 'production_algorithm_changed': False,
        'development_evidence': {'path': str(development_path), 'sha256': digest(development_path)},
        'development_results': {name: {key: value for key, value in result.items() if key != 'rows'}
                                for name, result in development['strategies'].items()},
        'root_causes': ['lexical AND constraints cannot distinguish conversational context from mandatory semantic constraints',
                        'dropping unknown constraints improves recall but falsely accepts causal/property claims',
                        'deterministic fallback lacks validated semantic route/conclusion/stop/adaptive planning',
                        '47 next-step rule proposals do not independently establish 384 semantic fields; 337 missing proposals'],
        'rejected_fixes': ['drop all auxiliary tokens: 4/5 no-answer queries false positive',
                          'keep first auxiliary token: 1/5 no-answer queries false positive',
                          'case-ID/gold-specific answers or meaningless parameter changes'],
        'next_steps': ['approved semantic constraint candidate and planner on independent development/validation labels',
                       'freeze configuration before unseen product/time holdout; legacy test is regression only'],
        'quality_failure_remains_local': True, 'real_provider_authorization_and_independent_labels': 'blocked E2'}
    save(folder / 'local-quality-diagnosis.json', diagnosis)
    quality = recompute_quality(previous)
    save(folder / 'quality-component-recomputed.json', quality)
    old_quality = json.loads(Path(old['tasks']['default_quality']['assessment']['path']).read_text())
    local = quality_checks(quality)
    assessment = folder / 'default_quality-assessment.json'
    save(assessment, {'status': 'passed' if all(local.values()) else 'failed', 'source_sha256': source,
                      'source_nature': 'local_synthetic_or_engineering', 'checks': local,
                      'metrics': old_quality['metrics'], 'actual': quality,
                      'runtime_provenance': {'source_sha256': old['source_sha256'], 'runtime_dependencies_unchanged': True}})
    manifest['tasks']['default_quality'].update(status='passed' if all(local.values()) else 'failed', actual=quality,
         assessment={'path': str(assessment), 'sha256': digest(assessment)},
         evidence=old['tasks']['default_quality']['evidence'] + [{'path': str(p), 'sha256': digest(p)} for p in [assessment, folder / 'local-quality-diagnosis.json', development_path]])
    candidate_path = folder / 'candidate/candidate.json'
    candidate = json.loads(candidate_path.read_text())
    if candidate['status'] != 'passed' or any(candidate['source_hashes'].get(name) != value for name, value in hashes.items()):
        raise ValueError('current candidate missing or stale')
    candidate_assessment = folder / 'candidate-assessment.json'
    save(candidate_assessment, {'status': 'passed', 'source_sha256': source, 'checks': {'current_archive_and_images_verified': True},
                               'source_nature': 'local_synthetic_or_engineering', 'actual': candidate['images']})
    manifest['tasks']['candidate'].update(status='passed', actual=candidate['images'],
         assessment={'path': str(candidate_assessment), 'sha256': digest(candidate_assessment)},
         evidence=[{'path': str(p), 'sha256': digest(p)} for p in [candidate_path, folder / 'candidate/source.tar.gz']])
    manifest['previous_run'] = str(previous)
    manifest['verification_scope'] = impact
    save(folder / 'release_acceptance_manifest.json', manifest)
    code = gate(manifest, folder)
    counts = {}
    for task in manifest['tasks'].values():
        counts[task['status']] = counts.get(task['status'], 0) + 1
    save(folder / 'delivery-summary.json', {'status': 'not_ready' if code else 'release_ready', 'task_status_counts': counts,
                                          'source_sha256': source, 'new_dataset_tests': int(matches[-1]),
                                          'local_quality_status': quality['local_quality_status'],
                                          'old_runtime_results_are_historical': True, 'images': candidate['images']})
    print(json.dumps({'status': 'not_ready' if code else 'release_ready', 'task_counts': counts, 'dataset_tests': int(matches[-1])}))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
