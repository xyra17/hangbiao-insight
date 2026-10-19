"""Read-only execution/configuration audit. Never initialize a frozen release run."""
import argparse
import hashlib
import json
from pathlib import Path


def audit(runtime, retrieval):
    # Pure module: no Django, environment lookup, provider initialization or report mutation.
    import importlib.util
    spec = importlib.util.spec_from_file_location('execution_contract', Path(__file__).resolve().parents[1] / 'backend/insight/investigation/execution_config.py')
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    binding = runtime.get('execution_configuration')
    errors = []
    recomputed = None
    identity = runtime.get('execution_identity')
    if not binding or any(key not in binding for key in contract.CONFIGURATION_KEYS):
        errors.append('missing_observed_execution_binding_legacy_report')
    else:
        config = {key: binding[key] for key in contract.CONFIGURATION_KEYS}
        config.update({key: binding[key] for key in contract.OPTIONAL_CONFIGURATION_KEYS if key in binding})
        recomputed = contract.bind_observed_execution(config, runtime.get('trajectories', []), retrieval.get('per_query', {}))
        if retrieval.get('execution_configuration') != binding:
            errors.append('retrieval_runtime_configuration_binding_differs')
        for key, value in recomputed.items():
            if binding.get(key) != value or type(binding.get(key)) is not type(value):
                errors.append('declared_execution_differs_from_observations:' + key)
        if recomputed['configuration_execution_status'] != 'matched':
            errors.extend(recomputed['errors'])
        if not identity or identity != retrieval.get('execution_identity'):
            errors.append('missing_or_different_shared_execution_identity')
        else:
            try:
                from uuid import UUID
                UUID(identity['run_id'])
                assert identity['schema_version'] == 'execution-identity-v1'
                assert len(identity['input_manifest_sha256']) == 64
                expected = contract.execution_identity(config, runtime.get('trajectories', []), retrieval.get('per_query', {}), identity['input_manifest_sha256'], run_id=identity['run_id'])
                if expected != identity:
                    errors.append('execution_identity_content_hash_mismatch')
                for report in [runtime, retrieval]:
                    if report.get('runtime_corpus_manifest_sha256') != identity['input_manifest_sha256']:
                        errors.append('input_manifest_not_bound_to_report')
                    if 'input_manifest' in report and contract.content_digest(report['input_manifest']) != identity['input_manifest_sha256']:
                        errors.append('input_manifest_content_hash_mismatch')
            except (AssertionError, KeyError, TypeError, ValueError):
                errors.append('invalid_execution_identity')
    return {'status': 'failed' if errors else 'passed', 'errors': errors,
            'execution_configuration': binding, 'recomputed_execution_configuration': recomputed,
            'execution_identity': identity, 'quality_accepted': False,
            'scope': 'Read-only observed-report consistency and shared run/input/configuration/content binding; not provider attestation or business quality'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--retrieval', type=Path, required=True)
    args = parser.parse_args()
    result = audit(json.loads(args.runtime.read_text()), json.loads(args.retrieval.read_text()))
    result['bindings'] = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in [('runtime_sha256', args.runtime), ('retrieval_sha256', args.retrieval)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
