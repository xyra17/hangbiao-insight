"""Execution gate only; no expected answers, qrels or semantic grading."""


def representative_gate(trace, *, paid, date_scope=None, registry_sha256=None):
    reasons = []
    evidence = trace.get('state_evidence') or trace.get('result', {}).get('evidence', [])
    texts = [row for row in evidence if row.get('type') == 'text_retrieval']
    items = [item for row in texts for item in row.get('items', []) if item.get('match_kind') == 'relevant_hit']
    if trace.get('status') != 'completed' or trace.get('job', {}).get('status') != 'completed':
        reasons.append('noncompleted_representative_investigation')
    if not items:
        reasons.append('no_original_evidence_for_nontrivial_gate')
    if not any(row.get('phase') == 'after_text' and row.get('execution_origin') == 'provider_response' for row in trace.get('decisions', [])):
        reasons.append('no_real_after_evidence_planner_decision')
    if not any(row.get('relevance_judgment', {}).get('response_received') is True and
               row.get('relevance_judgment', {}).get('execution_origin') == 'provider_response' for row in texts):
        reasons.append('no_real_relevance_response')
    if any(row.get('candidate_stage', {}).get('semantic_candidates', {}).get('status') == 'unsupported' for row in texts):
        reasons.append('semantic_index_unavailable')
    if any(call.get('authorization') != 'allowed' or call.get('status') != 'completed' for call in trace.get('tools', [])):
        reasons.append('tool_authorization_or_execution_incomplete')
    terminal = [d for d in trace.get('decisions', []) if d.get('conclusion') and d.get('execution_origin') == 'provider_response']
    if not terminal:
        reasons.append('no_real_terminal_proposal')
    if date_scope:
        scope = trace.get('result', {}).get('request_scope') or {}
        if any(scope.get(k) != date_scope[k] for k in ('start', 'end')):
            reasons.append('terminal_date_scope_mismatch')
        if trace.get('requested_child_asin_scope') != trace.get('resolved_child_asin_scope'):
            reasons.append('terminal_product_scope_mismatch')
    result = trace.get('result', {})
    contract = result.get('business_contract') or {}
    if trace.get('business_contract_version') == 'investigation-business-contract-v2' or contract.get('schema_version') == 'investigation-business-contract-v2':
        from insight.investigation.business_contract_registry import VERSION, fingerprint, validate
        expected_registry = registry_sha256 or fingerprint()
        if result.get('business_contract_registry_sha256') != expected_registry:
            reasons.append('terminal_business_registry_hash_mismatch')
        if contract.get('schema_version') != VERSION:
            reasons.append('terminal_business_contract_version_mismatch')
        else:
            for field in ('expected_route', 'expected_behavior', 'expected_next_step', 'allowed_conclusion',
                          'forbidden_conclusion', 'safety_escalation', 'expected_stop_condition', 'expected_tool_names'):
                try:
                    validate(field, contract.get(field))
                except ValueError:
                    reasons.append('terminal_business_contract_invalid:' + field)
        if contract.get('allowed_conclusion') in {None, 'unknown', 'incomplete_evidence'}:
            reasons.append('nontrivial_gate_has_no_evidence_bound_proposal')
    conclusion = result.get('conclusion') or {}
    citations = conclusion.get('citations', [])
    if not citations:
        reasons.append('no_terminal_citations')
    else:
        from insight.investigation.citations import validate_citations
        try:
            validate_citations(citations, texts)
        except ValueError:
            reasons.append('terminal_citation_not_bound_to_original')
    return {'status': 'passed' if paid and not reasons else 'failed' if paid else 'offline_not_real_execution_proof',
            'scenario_id': trace['scenario_id'], 'execution_gate_passed': paid and not reasons,
            'quality_accepted': False, 'reasons': reasons,
            'scope': 'real planner, original evidence, relevance, after-evidence decision and cited terminal; not semantic correctness'}
