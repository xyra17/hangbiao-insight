"""Metadata-only independent labeling handoff. Never read labels or assign blind splits."""
import argparse
import csv
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(index, inventory, output):
    audit = json.loads(inventory.read_text())
    if sha(index) != audit['candidate_index']['sha256']:
        raise ValueError('candidate index hash mismatch')
    if output.exists():
        raise ValueError('unique new package directory required')
    with index.open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    products = Counter(row['canonical_product'] for row in rows)
    parent = {product: product for product in products}

    def find(product):
        while parent[product] != product:
            product = parent[product]
        return product

    clusters = defaultdict(set)
    for row in rows:
        clusters[row['normalized_content_sha256']].add(row['canonical_product'])
    for members in clusters.values():
        members = sorted(members)
        for member in members[1:]:
            parent[find(member)] = find(members[0])
    components = defaultdict(list)
    for product in products:
        components[find(product)].append(product)
    output.mkdir(parents=True, mode=0o700)
    templates = {
        'roles.csv': ['role', 'person_id', 'appointed_by', 'appointed_at', 'independence_attested'],
        'split_approval.csv': ['canonical_product', 'exact_duplicate_component', 'proposed_split', 'time_cutoff', 'exposure_reviewed_by', 'near_duplicate_reviewed_by', 'custodian_approval'],
        'retrieval_queries.csv': ['query_id', 'query', 'language', 'split', 'canonical_product', 'input_manifest_sha256', 'human_author'],
        'qrels_reviewer_a.csv': ['query_id', 'document_id', 'relevance', 'no_answer', 'source_field', 'quote', 'reviewer', 'reviewed_at', 'label_source'],
        'qrels_reviewer_b.csv': ['query_id', 'document_id', 'relevance', 'no_answer', 'source_field', 'quote', 'reviewer', 'reviewed_at', 'label_source'],
        'qrels_adjudication.csv': ['query_id', 'document_id', 'final_relevance', 'no_answer', 'reviewer_a', 'reviewer_b', 'adjudicator', 'reason', 'reviewed_at', 'input_manifest_sha256'],
        'semantic_labels.csv': ['document_id', 'split', 'input_sha256', 'primary_topic', 'secondary_topic', 'polarity', 'severity', 'evidence_span', 'reviewer', 'reviewed_at', 'label_source'],
        'source_handoff.csv': ['source_kind', 'data_owner', 'finance_owner', 'file_reference', 'timezone', 'currency', 'grain', 'coverage_start', 'coverage_end', 'revision_void_rules', 'transfer_permission'],
        'target_handoff.csv': ['owner', 'target_reference', 'cores', 'memory_gb', 'ssd_verified', 'tls_reference', 'account_policy', 'backup_location', 'rpo_seconds', 'rto_seconds', 'retention_days', 'on_call_owner'],
        'pilot_handoff.csv': ['owner', 'operator_a', 'operator_b', 'asin_count', 'start_date', 'end_date', 'event_count', 'baseline_time_includes_preparation', 'signoff_reference'],
    }
    for name, fields in templates.items():
        with (output / name).open('x', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            if name == 'roles.csv':
                for role in ['data_owner', 'model_budget_owner', 'security_transfer_owner', 'reviewer_a', 'reviewer_b', 'adjudicator', 'holdout_custodian', 'target_ops_owner', 'pilot_owner']:
                    writer.writerow({'role': role})
            if name == 'source_handoff.csv':
                for kind in ['sales_traffic', 'orders', 'refunds', 'returns', 'reviews', 'support', 'inventory', 'ratings', 'operations']:
                    writer.writerow({'source_kind': kind})
    protected = output / 'holdout-custodian'
    protected.mkdir(mode=0o700)
    (protected / 'README.md').write_text('No holdout labels or raw records have been placed here.\nCustodian must choose unexposed product/time/duplicate groups, freeze manifests, and store labels outside the agent workspace with separate permissions. Mode 0700 alone does not isolate labels from the same OS user.\n')
    permission = {'status': 'pending_human_authorization', 'approved': False, 'provider': 'deepseek', 'implemented_model_not_approved': 'deepseek-v4-flash',
                  'approved_model_version': None, 'processing_region': None, 'retention_terms': None, 'transmission_scope': None,
                  'currency': None, 'input_rate_per_million': None, 'output_rate_per_million': None,
                  'single_call_budget': None, 'per_investigation_budget': None, 'evaluation_total_budget': None,
                  'secret_reference_only_no_secret_value': None, 'approved_by': None, 'approval_reference': None,
                  'production_publish_approved': False}
    (output / 'model-approval.json').write_text(json.dumps(permission, ensure_ascii=False, indent=2) + '\n')
    result = {'status': 'blocked_external_labels_and_approval', 'candidate_rows': len(rows), 'candidate_products': dict(products),
              'exact_duplicate_product_components': sorted(sorted(members) for members in components.values()),
              'component_count': len(components), 'minimum_additional_independent_components': max(0, 3 - len(components)),
              'cross_product_exact_duplicate_clusters': sum(len(members) > 1 for members in clusters.values()),
              'candidate_date_range': [min(row['created_at'][:10] for row in rows), max(row['created_at'][:10] for row in rows)],
              'split_assigned': False, 'fresh_holdout_established': False, 'near_duplicate_and_exposure_review': 'pending_human',
              'independent_human_labels_created': 0, 'raw_text_exported': False,
              'inputs': {'candidate_index': {'path': str(index.resolve()), 'sha256': sha(index)}, 'eligibility_inventory': {'path': str(inventory.resolve()), 'sha256': sha(inventory)}},
              'requirements': ['at least three disjoint product/time/duplicate components for development/validation/holdout',
                               'exact duplicate components cannot cross splits; semantic near duplicates reviewed before freeze',
                               'A/B independent first labels without model output; different reviewer IDs; separate adjudicator',
                               'holdout labels outside agent-accessible workspace under a different permission principal',
                               'new 384-field investigation adjudications generated only from the actual new runtime trace',
                               'seen legacy gold/test only regression; no answer-based tuning or blind-test claim'],
              'templates': {path.name: sha(path) for path in output.iterdir() if path.is_file()}}
    (output / 'manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    os.chmod(output / 'model-approval.json', 0o600)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-index', type=Path, required=True)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.candidate_index, args.inventory, args.output)
    print(json.dumps({key: result[key] for key in ['status', 'candidate_rows', 'component_count', 'minimum_additional_independent_components']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
