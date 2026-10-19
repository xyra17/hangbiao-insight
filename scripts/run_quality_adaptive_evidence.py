"""Reconcile actual adaptive-quality evidence with scoped historical import evidence."""
import argparse
import copy
import json
import os
import re
import subprocess
import sys
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

ALLOWED_CHANGES = {
    'scripts/ai_silver_label.py', 'scripts/prepare_public_review_pilot.py',
    'scripts/score_silver_predictions.py', 'dataset/tests/test_public_silver_pilot.py',
    'backend/tests_v2/test_model_version_receipts.py',
    'backend/insight/investigation/model_limits.py', 'backend/insight/investigation/budget.py',
    'backend/tests_v2/test_model_cost_readiness.py', 'scripts/prepare_quality_readiness.py',
    'dataset/tests/test_quality_readiness_package.py',
    'backend/insight/investigation/execution_config.py', 'backend/tests_v2/test_core_execution_contract.py',
    'backend/tests_v2/test_s4_s6_runtime_acceptance.py', 'backend/app/semantic/factory.py', 'backend/app/agent/planner.py',
    'scripts/verify_quality_execution_binding.py', 'dataset/tests/test_execution_binding_audit.py',
    'backend/insight/investigation/scope.py', 'backend/insight/investigation/freeze.py',
    'backend/insight/derived.py', 'backend/insight/serializers.py',
    'backend/tests_v2/test_investigation_date_scope.py',
    'backend/tests_v2/test_s1_boundaries.py',
    'backend/insight/investigation/graph.py', 'backend/insight/investigation/planner.py',
    'backend/insight/investigation/fact_followup.py', 'backend/insight/investigation/output.py',
    'backend/insight/management/commands/seed_metric_evidence_acceptance.py',
    'frontend/e2e/metric_evidence.spec.ts',
    'backend/insight/investigation/citations.py', 'backend/insight/semantic/relevance.py',
    'backend/app/agent/output_contract.py', 'backend/tests_v2/test_semantic_relevance.py',
    'backend/tests_v2/test_metric_citations.py',
    'backend/insight/management/commands/seed_investigation_evidence_acceptance.py',
    'backend/insight/semantic/retrieval.py', 'backend/insight/semantic/safety_context.py',
    'backend/tests_v2/test_derived_refresh.py', 'backend/tests_v2/test_evidence_adaptive_quality.py',
    'backend/tests_v2/test_investigation.py', 'backend/tests_v2/test_investigation_recovery.py',
    'backend/tests_v2/test_retrieval_strategy_experiment.py', 'backend/tests_v2/test_semantic.py',
    'dataset/tests/test_independent_label_workpack.py', 'dataset/tests/test_quality_state_mapping.py',
    'dataset/tests/test_quality_adaptive_evidence.py',
    'frontend/e2e/investigation_evidence.spec.ts', 'frontend/src/workbench/InvestigationEvidence.test.tsx',
    'frontend/src/workbench/InvestigationEvidence.tsx', 'scripts/prepare_independent_label_workpack.py',
    'scripts/reconcile_release_evidence.py', 'scripts/run_release_gate_followup.py',
    'scripts/verify_existing_original_retrieval.py', 'scripts/run_quality_adaptive_evidence.py',
    'scripts/probe_restored_api.py', 'scripts/run_release_image_rollback.py',
    'backend/insight/metrics/occurrence.py', 'backend/insight/investigation/api.py', 'backend/insight/investigation/orders.py', 'backend/insight/investigation/policy.py',
    'backend/insight/investigation/access.py', 'backend/insight/investigation/evaluation.py',
    'backend/tests_v2/test_occurrence_workplan.py', 'backend/tests_v2/test_investigation_agent_contract.py',
}


def passed_count(path):
    text = path.read_text()
    matches = re.findall(r'(\d+) passed', text)
    if not matches or re.search(r'\d+ (?:failed|errors?)|FAILED |ERROR |Traceback ', text):
        raise ValueError('failed or missing regression result: ' + str(path))
    return int(matches[-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runtime-suffix', required=True)
    for name in ['backend-log', 'dataset-log', 'frontend-test-log', 'frontend-build-log', 'browser-log', 'ruff-log',
                 'browser-dir', 'original-probe', 'rollback-dir']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    old_dir, output = args.previous.resolve(), args.output.resolve()
    if old_dir == output or not output.is_relative_to(ROOT / '.local/acceptance-artifacts'):
        parser.error('separate owned output required')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.runtime_suffix):
        parser.error('unique runtime suffix required')
    previous = json.loads((old_dir / 'release_acceptance_manifest.json').read_text())
    old_hashes = json.loads((old_dir / 'source_hashes.json').read_text())
    source, hashes = source_fingerprint()
    changes = sorted(name for name in set(old_hashes) | set(hashes) if old_hashes.get(name) != hashes.get(name))
    if set(changes) - ALLOWED_CHANGES:
        raise ValueError('unreviewed changes: ' + str(set(changes) - ALLOWED_CHANGES))
    references = []
    for name, task in previous['tasks'].items():
        for evidence in task.get('evidence', []):
            if digest(evidence['path']) != evidence['sha256']:
                raise ValueError('frozen previous evidence changed: ' + evidence['path'])
            references.append(evidence)
        if task['status'] == 'passed' and name not in {'inventory', 'validate04'} and (reason := verified_assessment(task, previous['source_sha256'], name)):
            raise ValueError('previous assessment invalid: ' + name + ':' + reason)
    counts = {name: passed_count(getattr(args, name + '_log')) for name in ['backend', 'dataset', 'browser']}
    frontend_log = args.frontend_test_log.read_text()
    unit = re.search(r'Tests\s+(\d+) passed', frontend_log)
    if not unit or 'failed' in frontend_log or 'built in' not in args.frontend_build_log.read_text() or 'All checks passed!' not in args.ruff_log.read_text():
        raise ValueError('frontend/build/lint not passed')
    counts['frontend'] = int(unit[1])
    output.mkdir(parents=True, exist_ok=True)
    commands = []

    def command(name, flags, valid_exits=(0,)):
        argv = [sys.executable, str(ROOT / 'dataset' / name), *map(str, flags)]
        log = output / (name + '.log')
        with log.open('w') as stream:
            result = subprocess.run(argv, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False)
        commands.append({'command': argv, 'exit_code': result.returncode, 'valid_execution_exits': valid_exits})
        if result.returncode not in valid_exits:
            raise ValueError('evaluation command error: ' + name)

    pack = ROOT / 'dataset/data/07_S4_S6独立评测_v1/validation'
    runtime = pack / f'real_paired_investigation_runtime_{args.runtime_suffix}.json'
    retrieval = pack / f'real_retrieval_runtime_{args.runtime_suffix}.json'
    adapted, scoring, bundle, component = [output / name for name in ['adapted-final.json', 'paired-score-final.json', 'quality-bundle-final.json', 'quality-gate-final.json']]
    template = output / 'independent-adjudications-current.csv'
    command('adapt_investigation_outputs.py', ['--runtime-report', runtime, '--write-template', template, '--output', adapted])
    command('score_paired_investigations.py', ['--runtime-report', runtime, '--adapted', adapted, '--output', scoring], (0, 1))
    command('quality_gate.py', ['--retrieval', retrieval, '--paired', runtime, '--adapted', adapted, '--scoring', scoring,
                              '--bundle-output', bundle, '--output', component], (0, 1))
    audit = output / 'capability-audit-final.json'
    command('audit_investigation_capabilities.py', ['--runtime-report', runtime, '--output', audit])
    save(output / 'evaluation-commands.json', commands)
    quality = recompute_quality(output)
    capabilities = json.loads(audit.read_text())
    probe = json.loads((args.original_probe / 'result.json').read_text())
    rollback = json.loads((args.rollback_dir / 'result.json').read_text())
    candidate = json.loads((output / 'candidate/candidate.json').read_text())
    if candidate['status'] != 'passed' or any(candidate['source_hashes'].get(name) != value for name, value in hashes.items()):
        raise ValueError('stale candidate source')
    manifest = initialize(output, previous['as_of'])
    impact = {'status': 'passed', 'previous_source_sha256': previous['source_sha256'], 'source_sha256': source,
              'changed_files': changes, 'verified_previous_evidence_references': references,
              'reexecuted': ['complete backend with current runtime', 'frontend unit/build', 'complete actual API browser with request-versus-actual dates and occurrence/subtask evidence',
                             'bound adapter/scoring/component recomputation', 'current retrieval over prior 600-original import',
                             'new-image versus prior-image nonempty clone rollback', 'dataset tools and source lint'],
              'reuse_boundary': 'Original importer/parser, daily/cohort financial formulas, action rules, database schema, dependency locks and input facts unchanged by hash. Explicit request dates now resolve in shop timezone and freeze into inputs; graph reads, semantic claims, citation validation and refresh inheritance changed. Current date-specific CSV/API/worker tests and full regression cover these changes; unsupported metric windows are blocked without financial recomputation. Order samples prioritize observed links and report unresolved/truncated associations; historical association rows are marked separately. Occurrence empty-window return convention now retains the observed source contract; numeric sums and event eligibility are unchanged and current complete backend/CSV regressions cover this metadata correction. Investigation order-source policy, denial receipt durability and read visibility changed; covered by current complete backend and dedicated raw-source revocation/recovery regressions. Prior complete business imports/oracles/action windows/lifecycle/recovery inputs retained as historical; no new package import or target capacity claim.',
              'browser_boundary': 'Prior three business-package views retain historical scope; renderer backward compatibility and current occurrence-basis/subtask behavior covered by fresh unit and three-width actual-API browser. No claim of reexecuted package imports or snapshots.'}
    save(output / 'impact-review.json', impact)
    for name, task in previous['tasks'].items():
        manifest['tasks'][name] = copy.deepcopy(task)
        if task['status'] != 'passed' or name in {'integration', 'security', 'originals01', 'local_recovery', 'candidate'}:
            continue
        manifest['tasks'][name]['historical_runtime_source_sha256'] = previous['source_sha256']
        if name in {'inventory', 'validate04'}:
            continue
        old_assessment = Path(task['assessment']['path'])
        report = json.loads(old_assessment.read_text())
        report.update(source_sha256=source, reuse_provenance={'original_assessment': str(old_assessment), 'sha256': digest(old_assessment),
                       'original_source_sha256': previous['source_sha256'], 'historical_runtime_not_rerun': True,
                       'impact_review': str(output / 'impact-review.json')})
        path = output / (name + '-assessment.json')
        save(path, report)
        manifest['tasks'][name]['assessment'] = {'path': str(path), 'sha256': digest(path)}
        manifest['tasks'][name]['evidence'].append({'path': str(output / 'impact-review.json'), 'sha256': digest(output / 'impact-review.json')})

    def bind(name, checks, actual, paths, metrics=None):
        status = 'passed' if checks and all(value is True for value in checks.values()) else 'failed'
        evidence = [{'path': str(path.resolve()), 'sha256': digest(path)} for path in paths]
        assessment = output / (name + '-assessment.json')
        save(assessment, {'status': status, 'source_sha256': source, 'source_nature': 'local_synthetic_or_engineering',
                          'checks': checks, 'metrics': metrics or {}, 'actual': actual, 'evidence': evidence})
        manifest['tasks'][name].update(status=status, actual=actual, evidence=evidence,
                                      assessment={'path': str(assessment), 'sha256': digest(assessment)})

    bind('default_quality', quality_checks(quality), quality, [runtime, retrieval, adapted, scoring, bundle, component, template, audit],
         {'test_recall_at_10': quality['retrieval_quality']['test']['metrics']['recall_at_10'],
          'citation_validity': quality['retrieval_quality']['test']['metrics']['citation_validity'],
          'tool_difference_pairs': capabilities['tool_or_parameter_difference_pairs'],
          'evidence_driven_pairs': capabilities['evidence_driven_parameter_difference_pairs']})
    logs = [getattr(args, name + '_log') for name in ['backend', 'dataset', 'browser', 'frontend_test', 'frontend_build', 'ruff']]
    cleanup = json.loads((args.browser_dir / 'cleanup.json').read_text())
    bind('integration', {'current_backend': counts['backend'] > 0, 'current_data_tools': counts['dataset'] > 0,
                        'current_frontend': counts['frontend'] > 0, 'actual_browser': counts['browser'] > 0,
                        'owned_browser_cleanup': cleanup['owned_service_survivors'] == 0}, counts, logs + [args.browser_dir / 'cleanup.json'])
    bind('originals01', {'current_original_api': probe['status'] == 'passed', 'no_reimport': probe['no_reimport'] is True,
                        'historical_600_source_unchanged': probe['historical_selected_rows'] == 600}, probe,
         [args.original_probe / name for name in ['result.json', 'retrieval.json', 'cleanup.json']] + [old_dir / 'package-04/originals-lineage.json'])
    secret_scan = output / 'source-secret-scan.json'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/release_source_security_scan.py'), '--output', str(secret_scan)], cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError('source secret scan failed')
    security_old = json.loads(Path(previous['tasks']['security']['assessment']['path']).read_text())
    bind('security', {**security_old['checks'], 'current_source_secret_scan': json.loads(secret_scan.read_text())['status'] == 'passed'},
         {'historical_dependency_locks_unchanged': True, 'current_security_runtime_in_full_backend': counts['backend']},
         [Path(e['path']) for e in previous['tasks']['security']['evidence']] + [secret_scan, args.backend_log, args.ruff_log])
    bind('local_recovery', {'current_lock_budget_recovery_regression': counts['backend'] > 0,
                           'current_image_local_rollback': rollback['status'] == 'passed'},
         {'historical_backup_fault_retention_inputs': previous['tasks']['local_recovery']['actual'], 'current_rollback': rollback},
         [Path(e['path']) for e in previous['tasks']['local_recovery']['evidence']] + [args.backend_log, args.rollback_dir / 'result.json', args.rollback_dir / 'cleanup.json'])
    bind('candidate', {'source_images_verified': candidate['status'] == 'passed'}, candidate['images'],
         [output / 'candidate/candidate.json', output / 'candidate/source.tar.gz'])
    actual_planner = json.loads(runtime.read_text()).get('planner_provider')
    actual_relevance = {row['algorithm_manifest']['relevance_enabled'] for row in json.loads(retrieval.read_text())['per_query'].values()}
    if actual_planner != 'deterministic' or actual_relevance != {False}:
        raise ValueError('this scoped run must be actual default fallback; use a separate approved model/label run')
    runtime_config = json.loads(runtime.read_text()).get('execution_configuration')
    if not runtime_config or runtime_config.get('configuration_execution_status') != 'matched':
        raise ValueError('missing or mismatched observed runtime configuration; current shell environment is not runtime evidence')
    config = {'execution_configuration': runtime_config, 'evaluated_planner': actual_planner,
              'relevance_enabled': next(iter(actual_relevance)), 'aggregation_process_environment_not_runtime': {key: os.getenv(key, default) for key, default in
                    [('INVESTIGATION_PLANNER_PROVIDER', 'deterministic'), ('EVIDENCE_RELEVANCE_ENABLED', '0'), ('INSIGHT_ENABLED_MODEL_PROVIDERS', '')]},
              'real_provider_run': False, 'production_configuration_approved': False, 'dependency': 'E2/E3',
              'images': candidate['images'], 'runtime_sha256': digest(runtime), 'source_sha256': source,
              'scope': 'default fallback only; real semantic/planner requires separate authorized configuration and same-run labels'}
    save(output / 'configuration-binding.json', config)
    manifest['tasks']['real_quality']['evidence'].extend({'path': str(path), 'sha256': digest(path)} for path in [template, output / 'configuration-binding.json'])
    matrix_input = old_dir / 'requirements-matrix-current.json'
    if not matrix_input.exists():
        matrix_input = old_dir / 'requirements-matrix.json'
    matrix = json.loads(matrix_input.read_text())
    matrix['inherited_matrix_path'] = str(matrix_input)
    matrix['current_scope'] = {'source_sha256': source, 'historical_package_run': str(old_dir), 'current_quality': quality,
                               'current_engineering_counts': counts, 'real_signoffs': 'not_established'}
    save(output / 'requirements-matrix.json', matrix)
    manifest['tasks']['frac']['evidence'].append({'path': str(output / 'requirements-matrix.json'), 'sha256': digest(output / 'requirements-matrix.json')})
    save(output / 'release_acceptance_manifest.json', manifest)
    code = gate(manifest, output)
    states = {}
    for task in manifest['tasks'].values():
        states[task['status']] = states.get(task['status'], 0) + 1
    save(output / 'delivery-summary.json', {'status': 'not_ready' if code else 'release_ready', 'task_status_counts': states,
            'source_sha256': source, 'tests': counts, 'quality': quality, 'capabilities': capabilities, 'images': candidate['images'],
            'historical_package_imports_not_rerun': True, 'real_model_or_target_or_pilot_claim': False})
    print(json.dumps({'status': 'not_ready' if code else 'release_ready', 'task_status_counts': states, 'tests': counts}))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
