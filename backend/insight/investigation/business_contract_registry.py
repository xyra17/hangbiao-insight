"""Product decision vocabulary, independent of evaluation cases and model answers.

Derived from S4 evidence/coverage/safety, S5 internal followup and S6 scope,
permission and stopping boundaries. Labels express the principal decision on
one axis; detailed findings and secondary qualifications stay in statement.
"""
import hashlib
import json

VERSION = 'investigation-business-contract-v2'
SOURCES = ['docs/开发阶段与验收计划.md:S4-S6', 'insight/investigation/policy.py:TOOL_POLICIES',
           'insight/investigation/fact_followup.py:source/window/cohort/return/authorization boundaries']

# Each entry: product meaning and a finite map from stable labels to meanings.
FIELDS = {
    'expected_route': ('本次调查的主要业务路径', {
        'feedback_review': '调查评论或客服反馈', 'metric_review': '核对经营指标与口径',
        'order_lifecycle_review': '核对订单、退款、退货事件', 'safety_review': '安全风险核查与人工移交',
        'authorization_review': '核查变更授权边界', 'scope_clarification': '定位授权对象或日期范围',
        'data_readiness_review': '核查缺源、覆盖或版本问题'}),
    'expected_behavior': ('本次证据处理的主要行为', {
        'separate_occurrence_and_cause': '把发生现象与原因解释分开',
        'compare_support_and_counterevidence': '比较支持与反证，保留冲突',
        'verify_metric_basis': '核对总量、日均、百分数、百分点与分母',
        'verify_order_events': '核对生命周期与冲正、申请、收到口径',
        'retain_scope_boundary': '限定授权范围、版本和取证样本',
        'abstain_on_incomplete_evidence': '不完整证据不作肯定或缺失结论',
        'escalate_safety': '识别危险信号并交人工处理'}),
    'expected_next_step': ('最优先的下一项业务或取证动作', {
        'verify_product_and_usage': '核查商品参数、使用方式、机型/配件和实测',
        'repair_source_and_window': '补齐来源、日期覆盖和冻结版本',
        'verify_metric_denominator': '核对指标分母和成熟队列',
        'verify_return_receipt': '核查退货实际收到日期和状态',
        'reconcile_order_events': '核对订单及退款冲正事件',
        'collect_counterevidence': '补充新的反证方向取证',
        'clarify_authorized_scope': '请求授权对象或日期的澄清',
        'request_human_approved_change': '通过负责人批准流程核查外部变更',
        'handoff_safety': '把安全风险移交负责人',
        'create_internal_followup': '建立明确授权的未指派内部核查草稿',
        'no_further_action': '在现有范围没有可执行的进一步核查'}),
    'allowed_conclusion': ('当前证据支持的结论边界', {
        'case_level_feedback': '仅说明引用样本中的反馈或现象',
        'cited_metric_fact': '仅说明冻结指标事实，不证明原因',
        'conflicting_feedback': '引用同时存在支持和反证',
        'incomplete_evidence': '证据覆盖不足，不能确认业务判断',
        'no_matching_original_evidence_in_scope': '仅在本次范围完整检索未命中',
        'authorization_boundary_only': '仅说明当前授权边界'}),
    'forbidden_conclusion': ('必须避免的主要错误推断；其他限制写statement', {
        'unproven_causal_claim': '用相关或自述归因断言因果',
        'population_rate_from_sample': '用检索样本推算总体发生率',
        'absence_from_incomplete_search': '把不完整检索当作问题不存在',
        'received_from_requested_return': '把退货申请当作已收到',
        'resolution_from_action_completion': '把动作完成当作效果已达成',
        'external_change_without_approval': '把调查或草稿当作外部变更授权',
        'comparison_across_incompatible_basis': '比较不同日期、分母、口径或版本'}),
    'safety_escalation': ('安全移交决定；没有明确证据不能证明安全', {
        'handoff_required': '原文有危险信号，需要负责人核查',
        'no_signal_in_examined_evidence': '本次检查样本未见安全信号，不代表总体安全',
        'insufficient_evidence': '无法据当前证据判断安全'}),
    'expected_stop_condition': ('业务或证据边界导致停止', {
        'evidence_scope_complete': '完成当前有界支持/反证核查',
        'evidence_incomplete': '来源或检索不完整', 'scope_clarification_required': '需澄清授权对象或日期',
        'authorization_boundary': '不能执行未经批准的操作',
        'safety_handoff': '安全风险进入人工核查', 'budget_boundary': '本次预算或步骤已耗尽',
        'no_new_evidence_direction': '没有新的可验证取证方向'}),
    'expected_tool_names': ('本业务路径需要的已存在能力；数组无顺序含义，unknown表示不能判断', {
        'search_feedback_evidence': '检索授权评论/客服原文',
        'get_metric_snapshot': '读取冻结指标快照', 'get_semantic_claims': '读取已接受语义主张',
        'read_frozen_order_records': '读取冻结订单生命周期样本',
        'create_internal_followup': '创建明确授权的内部核查草稿'}),
}
CAPABILITY_TO_TOOL = {'search_feedback_evidence': 'text_evidence', 'get_metric_snapshot': 'metric_snapshot',
    'get_semantic_claims': 'review_claims', 'read_frozen_order_records': 'order_records',
    'create_internal_followup': 'create_draft_task'}
UNKNOWN = 'unknown'


def manifest():
    return {'version': VERSION, 'sources': SOURCES, 'unknown': UNKNOWN, 'capability_to_tool': CAPABILITY_TO_TOOL,
            'fields': {k: {'meaning': v[0], 'values': {UNKNOWN: '证据或能力不足，未作判断', **v[1]},
                           'type': 'array_or_unknown' if k == 'expected_tool_names' else 'enum'} for k, v in FIELDS.items()},
            'scope': 'principal decision per axis; detailed findings remain in statement; no causal certification',
            'unsupported': {'typed_order_conclusion': 'raw order records can be examined but typed terminal order citation is not implemented; no order-fact enum is advertised',
                            'multiple_primary_decisions': 'one primary label per axis; additional findings in statement and reviewer notes, not new labels'},
            'evidence_requirements': {
                'case_level_feedback': 'original citation', 'conflicting_feedback': 'original citations for both directions',
                'cited_metric_fact': 'frozen metric atom', 'cited_order_fact': 'not enabled until typed order citation exists',
                'no_matching_original_evidence_in_scope': 'all searches complete and no_hits',
                'handoff_required': 'original safety signal; independent reviewer checks relevance',
                'external_write': 'not a provided tool; approval workflow only'}}


def fingerprint():
    return hashlib.sha256(json.dumps(manifest(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def json_schema():
    properties = {'schema_version': {'type': 'string', 'enum': [VERSION]}}
    for field, (meaning, values) in FIELDS.items():
        enums = [UNKNOWN, *values]
        properties[field] = ({'anyOf': [{'type': 'array', 'items': {'type': 'string', 'enum': list(values)}},
                                       {'type': 'string', 'enum': [UNKNOWN]}], 'description': meaning}
                             if field == 'expected_tool_names' else {'type': 'string', 'enum': enums, 'description': meaning})
    return {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': list(properties)}


def validate(field, value):
    if field not in FIELDS:
        raise ValueError('unknown_business_field')
    # Same structural vocabulary as json_schema; duplicates/empty capability
    # arrays have no invented semantic meaning and are accepted unchanged.
    if field == 'expected_tool_names' and isinstance(value, list):
        if any(type(v) is not str or v not in FIELDS[field][1] for v in value):
            raise ValueError('unknown_business_capability')
        return list(value)
    if type(value) is not str or value not in {UNKNOWN, *FIELDS[field][1]}:
        raise ValueError('unknown_business_decision:' + field)
    return value


def review_text():
    lines = ['# 共享业务契约 ' + VERSION, '', 'registry SHA256: ' + fingerprint(),
             '', '按实际证据独立选择同一词汇；本表只定义产品语义，不包含模型或案例答案。',
             '每个单选轴填写最主要决定；其他发现与限制在中文statement/评审备注中保留。unknown不算已裁决。',
             '工具字段使用原生JSON数组，或unknown；[]表示无所需业务能力，unknown表示未判断；CSV单元格用JSON编码表示数组，运行结果本身是数组。', '']
    for field, (meaning, values) in FIELDS.items():
        lines += ['## ' + field, '', meaning, '', '- unknown：未作判断']
        lines += ['- ' + key + '：' + text for key, text in values.items()]
        lines += ['']
    lines += ['## 证据边界', '', *['- ' + key + '：' + value for key, value in manifest()['evidence_requirements'].items()]]
    return '\n'.join(lines) + '\n'


def prompt_text():
    # The enum in schema is authoritative; this compact glossary is generated
    # from the exact same product definitions as the reviewer documentation.
    parts = ["registry=" + VERSION + ";sha256=" + fingerprint(), "unknown=证据或能力不足，未作判断。"]
    for field, (_, values) in FIELDS.items():
        parts.append(field + ":" + ";".join(key + "=" + text for key, text in values.items()))
    parts.append("每个轴只选主要决定，其他发现写statement；命中样本不证明因果或总体频率；不完整检索不证明不存在。")
    return "\n".join(parts)
