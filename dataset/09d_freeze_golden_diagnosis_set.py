from pathlib import Path  # 导入 Path，用于处理文件路径

import json  # 导入 json，用于保存 Manifest

import pandas as pd  # 导入 pandas，用于读取和保存数据


# ============================================================
# 1. 基础路径
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前脚本目录

INPUT_DIR = (  # 定义 Diagnosis Case 输入目录

    BASE_DIR

    / "outputs"

    / "09_诊断案例"

)  # 输入目录结束

OUTPUT_DIR = (  # 定义最终 Golden Diagnosis 输出目录

    BASE_DIR

    / "outputs"

    / "10_黄金诊断集"

)  # 输出目录结束

OUTPUT_DIR.mkdir(  # 创建输出目录

    parents=True,

    exist_ok=True,

)  # 创建结束


# ============================================================
# 2. 输入文件
# ============================================================

CASE_FILE = (  # 09 阶段完整 Candidate

    INPUT_DIR

    / "01_诊断案例候选_v1.csv"

)  # Case 文件结束

VALIDATION_FILE = (  # 09 阶段 Validation

    INPUT_DIR

    / "04_诊断案例校验_v1.csv"

)  # Validation 文件结束


# ============================================================
# 3. 输出文件
# ============================================================

GOLDEN_CASE_FILE = (  # 最终 Golden Diagnosis Case

    OUTPUT_DIR

    / "01_黄金诊断案例_v1_0.csv"

)  # Golden Case 路径结束

GOLDEN_EXPECTATION_FILE = (  # 每个 Case 的预期诊断行为

    OUTPUT_DIR

    / "02_黄金诊断预期_v1_0.csv"

)  # Expectation 文件结束

MANIFEST_FILE = (  # 数据版本 Manifest

    OUTPUT_DIR

    / "03_黄金诊断清单_v1_0.json"

)  # Manifest 路径结束

VALIDATION_OUTPUT = (  # 最终冻结校验

    OUTPUT_DIR

    / "04_黄金诊断校验_v1_0.csv"

)  # Validation 输出结束


# ============================================================
# 4. 读取 Diagnosis Candidate
# ============================================================

print("正在读取 Diagnosis Case Candidate……")  # 输出当前步骤

case_df = pd.read_csv(  # 读取 09 阶段 Case

    CASE_FILE

)  # 读取结束

case_df["current_month"] = pd.to_datetime(  # 标准化月份

    case_df["current_month"],

    errors="coerce",

)  # 时间转换结束


# ============================================================
# 5. 检查 09 Validation
# ============================================================

validation_df = pd.read_csv(  # 读取 09 Validation

    VALIDATION_FILE

)  # 读取结束

failed_validation = validation_df[  # 找失败项

    validation_df["passed"] != True

].copy()  # 筛选结束

if not failed_validation.empty:  # 如果存在失败 Validation

    raise ValueError(  # 不允许冻结

        "09 Validation 仍存在失败项，不能冻结 Golden Diagnosis Set。"

    )  # 错误结束


# ============================================================
# 6. 基础 Case 数量检查
# ============================================================

if len(case_df) != 7:  # 当前设计应该有 7 个 Case Month

    raise ValueError(  # 数量不一致则停止

        f"预期 7 个 Case Month，实际 {len(case_df)}。"

    )  # 错误结束

if case_df["case_id"].duplicated().any():  # 检查 Case ID 是否重复

    raise ValueError(  # 重复则停止

        "发现重复 case_id。"

    )  # 错误结束


# ============================================================
# 7. 固定已经通过 09C 审计的 No-Explanation Case
# ============================================================

APPROVED_NOEXP_CASE_ID = (  # 09C 最终选择

    "SYN-NOEXP-001__2019-06"

)  # NoExp Case ID 结束

noexp_df = case_df[  # 获取 NoExp Case

    case_df["scenario_type"]

    == "no_review_explanation"

].copy()  # 筛选结束

if len(noexp_df) != 1:  # 必须只有 1 个 NoExp

    raise ValueError(  # 否则停止

        f"预期 1 个 No-Explanation Case，实际 {len(noexp_df)}。"

    )  # 错误结束

actual_noexp_case_id = (  # 获取当前 Case ID

    noexp_df.iloc[0]["case_id"]

)  # 获取结束

if actual_noexp_case_id != APPROVED_NOEXP_CASE_ID:  # 检查是否为 09C 选中的月份

    raise ValueError(  # 如果不是则禁止冻结

        f"No-Explanation Case 不一致："

        f"当前={actual_noexp_case_id}，"

        f"09C批准={APPROVED_NOEXP_CASE_ID}"

    )  # 错误结束


# ============================================================
# 8. 定义每个 Golden Case 的预期行为
#
# 注意：
# 这里定义的是评测 Ground Truth / Expected Behavior，
# 不是现实世界因果 Ground Truth。
# ============================================================

EXPECTATIONS = {  # Case Expected Behavior 开始

    "SYN-EXPLAIN-001__2020-06": {  # Case 1

        "expected_anomaly": "negative_business_anomaly",  # 经营负向异常

        "expected_diagnosis_mode": "review_supported",  # 评论支持诊断

        "expected_review_conclusion": "supporting_evidence_found",  # 应找到支持证据

        "expected_agent_behavior": (

            "Identify order decline and refund deterioration; "

            "investigate negative review changes; "

            "surface relevant issue evidence; "

            "avoid causal wording."

        ),  # Agent 预期行为

        "expected_stop_condition": (

            "Stop after sufficient consistent review evidence is found "

            "and unsupported causal claims are avoided."

        ),  # 停止条件

    },  # Case 1 结束


    "SYN-MIXED-001__2022-02": {  # Case 2

        "expected_anomaly": "mixed_business_signal",  # 混合经营信号

        "expected_diagnosis_mode": "partial_review_support",  # 评论只能解释一部分

        "expected_review_conclusion": "partial_support_only",  # 只能部分支持

        "expected_agent_behavior": (

            "Recognize that orders increased while refund rate worsened; "

            "use reviews only to support quality/refund-side risk; "

            "do not claim reviews explain the order increase."

        ),  # Agent 行为

        "expected_stop_condition": (

            "Stop with a partial explanation and explicitly preserve "

            "the unexplained order-growth component."

        ),  # 停止条件

    },  # Case 2 结束


    "SYN-POSREC-001__2022-07": {  # Case 3

        "expected_anomaly": "positive_business_shift",  # 正向变化

        "expected_diagnosis_mode": "positive_review_support",  # 正向评论支持

        "expected_review_conclusion": "positive_supporting_evidence",  # 正向证据

        "expected_agent_behavior": (

            "Detect improving order/refund metrics; "

            "investigate positive review shifts; "

            "surface positive customer signals instead of searching only for complaints."

        ),  # Agent 行为

        "expected_stop_condition": (

            "Stop when positive review evidence is sufficient and "

            "correlation is not presented as causation."

        ),  # 停止条件

    },  # Case 3 结束


    "SYN-REVERSAL-001__2022-08": {  # Case 4

        "expected_anomaly": "negative_reversal",  # 正向后突然转负

        "expected_diagnosis_mode": "review_supported_reversal",  # Review 支持反转

        "expected_review_conclusion": "negative_shift_after_positive_period",  # 负向反转

        "expected_agent_behavior": (

            "Compare the current month with the immediately preceding positive period; "

            "detect the reversal; "

            "investigate newly worsening review signals."

        ),  # Agent 行为

        "expected_stop_condition": (

            "Stop after the reversal and supporting review evidence are established."

        ),  # 停止条件

    },  # Case 4 结束


    "SYN-POSREC-002__2022-11": {  # Case 5

        "expected_anomaly": "positive_recovery",  # 正向恢复

        "expected_diagnosis_mode": "positive_review_support",  # Review 正向支持

        "expected_review_conclusion": "positive_supporting_evidence",  # 正向证据

        "expected_agent_behavior": (

            "Recognize order growth and lower refund rate; "

            "inspect positive review changes and product-experience improvements."

        ),  # Agent 行为

        "expected_stop_condition": (

            "Stop after enough positive evidence is collected."

        ),  # 停止条件

    },  # Case 5 结束


    "SYN-POSREC-002__2022-12": {  # Case 6

        "expected_anomaly": "positive_continuation_with_mixed_metric",  # 持续改善但指标不完全一致

        "expected_diagnosis_mode": "positive_review_support_with_metric_caution",  # 正向证据但需谨慎

        "expected_review_conclusion": "positive_signal_continues",  # 评论侧正向趋势继续

        "expected_agent_behavior": (

            "Recognize continued order growth and positive review movement; "

            "also notice refund rate is roughly flat/slightly worse versus the three-month baseline; "

            "do not describe every business metric as improving."

        ),  # Agent 行为

        "expected_stop_condition": (

            "Stop with a qualified positive-continuation conclusion, "

            "preserving the refund-rate caveat."

        ),  # 停止条件

    },  # Case 6 结束


    "SYN-NOEXP-001__2019-06": {  # Case 7

        "expected_anomaly": "business_anomaly_without_review_explanation",  # 经营异常但评论无明显变化

        "expected_diagnosis_mode": "abstain_review_attribution",  # 应拒绝强行评论归因

        "expected_review_conclusion": "no_sufficient_review_explanation",  # 无足够评论解释

        "expected_agent_behavior": (

            "Detect the synthetic business anomaly; "

            "inspect review signals; "

            "recognize that review semantics remain comparatively stable; "

            "explicitly state that reviews do not sufficiently explain the anomaly; "

            "suggest investigating non-review causes."

        ),  # Agent 行为

        "expected_stop_condition": (

            "Stop and abstain from review attribution when evidence remains insufficient."

        ),  # 停止条件

    },  # Case 7 结束

}  # EXPECTATIONS 定义结束


# ============================================================
# 9. 检查所有 Case 都有 Expected Behavior
# ============================================================

actual_case_ids = set(  # 当前数据所有 Case ID

    case_df["case_id"].tolist()

)  # Set 结束

expected_case_ids = set(  # Expected Behavior 所有 Case ID

    EXPECTATIONS.keys()

)  # Set 结束

if actual_case_ids != expected_case_ids:  # 如果两边不一致

    missing_expectations = (  # 找未定义的 Case

        actual_case_ids

        - expected_case_ids

    )  # Missing 结束

    extra_expectations = (  # 找多余定义

        expected_case_ids

        - actual_case_ids

    )  # Extra 结束

    raise ValueError(  # 停止程序

        f"Case Expectation 不一致："

        f"missing={missing_expectations}, "

        f"extra={extra_expectations}"

    )  # 错误结束


# ============================================================
# 10. 将 Expected Behavior 合并进 Golden Case
# ============================================================

expectation_rows = []  # 保存 Expected Behavior 行

for case_id, expectation in EXPECTATIONS.items():  # 遍历定义

    expectation_rows.append({  # 创建一行

        "case_id": case_id,  # Case ID

        **expectation,  # 展开所有预期字段

    })  # 添加结束

expectation_df = pd.DataFrame(  # 转成 DataFrame

    expectation_rows

)  # 转换结束

golden_df = case_df.merge(  # 将 Expected Behavior 合并进 Case

    expectation_df,

    on="case_id",

    how="left",

    validate="one_to_one",

)  # 合并结束


# ============================================================
# 11. 正式冻结状态
# ============================================================

golden_df["golden_version"] = (  # Golden Dataset 版本

    "golden-diagnosis-v1.0"

)  # 版本结束

golden_df["golden_status"] = (  # 正式冻结

    "frozen"

)  # 状态结束

golden_df["frozen_date"] = (  # 冻结日期

    "2026-09-11"

)  # 日期结束

golden_df["business_ground_truth_type"] = (  # 明确 Business 数据属性

    "synthetic_evaluation_fixture"

)  # 类型结束

golden_df["review_ground_truth_type"] = (  # 明确 Review 数据属性

    "real_reviews_plus_reviewed_semantic_evidence"

)  # 类型结束

golden_df["causal_ground_truth"] = (  # 明确不存在现实因果 Ground Truth

    False

)  # 因果 Ground Truth 结束


# ============================================================
# 12. 修正原 Candidate 状态
# ============================================================

golden_df["case_status"] = "golden_frozen"  # 全部 Case 正式冻结


# ============================================================
# 13. 保存最终 Golden Diagnosis Case
# ============================================================

golden_df.to_csv(  # 保存完整 Golden Dataset

    GOLDEN_CASE_FILE,

    index=False,

    encoding="utf-8-sig",

)  # 保存结束


# ============================================================
# 14. 单独保存 Expected Behavior
# ============================================================

expectation_df["golden_version"] = (  # 增加版本信息

    "golden-diagnosis-v1.0"

)  # 版本结束

expectation_df.to_csv(  # 保存 Expected Behavior

    GOLDEN_EXPECTATION_FILE,

    index=False,

    encoding="utf-8-sig",

)  # 保存结束


# ============================================================
# 15. 构建 Manifest
# ============================================================

manifest = {  # Manifest 开始

    "dataset_name": "Golden Diagnosis Set",

    "version": "golden-diagnosis-v1.0",

    "status": "frozen",

    "frozen_date": "2026-09-11",

    "case_month_count": 7,

    "logical_scenario_count": int(

        golden_df["scenario_id"].nunique()

    ),

    "business_source": "synthetic",

    "review_source": "real_amazon_reviews",

    "golden_review_version": "golden_review_labels-v1.0",

    "taxonomy_version": "taxonomy-v1.1",

    "no_explanation_case": {

        "case_id": APPROVED_NOEXP_CASE_ID,

        "product_id": "B0BYJ8ZVWH",

        "month": "2019-06",

        "semantic_audit": "09C Top3 semantic audit",

        "audit_result": "SELECT",

    },

    "causality_policy": (

        "Review evidence may support, contradict, or fail to explain "

        "a synthetic business anomaly. Correlation must not be described as causation."

    ),

    "files": {

        "golden_cases": GOLDEN_CASE_FILE.name,

        "expectations": GOLDEN_EXPECTATION_FILE.name,

    },

}  # Manifest 结束

with MANIFEST_FILE.open(  # 打开 JSON 文件

    "w",

    encoding="utf-8",

) as f:  # 文件打开结束

    json.dump(  # 保存 Manifest

        manifest,

        f,

        ensure_ascii=False,

        indent=2,

    )  # JSON 保存结束


# ============================================================
# 16. 最终 Validation
# ============================================================

validation_rows = [  # 创建最终冻结检查

    {

        "check_name": "golden_case_count",

        "expected": 7,

        "actual": len(golden_df),

        "passed": len(golden_df) == 7,

    },

    {

        "check_name": "unique_case_id",

        "expected": 7,

        "actual": golden_df["case_id"].nunique(),

        "passed": golden_df["case_id"].nunique() == 7,

    },

    {

        "check_name": "all_case_frozen",

        "expected": 7,

        "actual": int(

            (

                golden_df["golden_status"]

                == "frozen"

            ).sum()

        ),

        "passed": bool(

            (

                golden_df["golden_status"]

                == "frozen"

            ).sum()

            == 7

        ),

    },

    {

        "check_name": "noexp_case_matches_09c",

        "expected": APPROVED_NOEXP_CASE_ID,

        "actual": actual_noexp_case_id,

        "passed": actual_noexp_case_id

        == APPROVED_NOEXP_CASE_ID,

    },

    {

        "check_name": "business_source_synthetic",

        "expected": 0,

        "actual": int(

            (

                golden_df["business_source"]

                != "synthetic"

            ).sum()

        ),

        "passed": bool(

            (

                golden_df["business_source"]

                != "synthetic"

            ).sum()

            == 0

        ),

    },

    {

        "check_name": "causal_claim_disabled",

        "expected": 0,

        "actual": int(

            golden_df["causal_claim_allowed"]

            .astype(bool)

            .sum()

        ),

        "passed": bool(

            golden_df["causal_claim_allowed"]

            .astype(bool)

            .sum()

            == 0

        ),

    },

    {

        "check_name": "expectation_complete",

        "expected": 7,

        "actual": int(

            golden_df["expected_agent_behavior"]

            .notna()

            .sum()

        ),

        "passed": bool(

            golden_df["expected_agent_behavior"]

            .notna()

            .sum()

            == 7

        ),

    },

]  # Validation 定义结束

final_validation_df = pd.DataFrame(  # 转成 DataFrame

    validation_rows

)  # 转换结束

final_validation_df.to_csv(  # 保存 Validation

    VALIDATION_OUTPUT,

    index=False,

    encoding="utf-8-sig",

)  # 保存结束


# ============================================================
# 17. 检查是否全部通过
# ============================================================

failed_df = final_validation_df[  # 找失败项

    final_validation_df["passed"] != True

].copy()  # 筛选结束

if not failed_df.empty:  # 如果存在失败

    raise ValueError(  # 不允许宣布冻结成功

        "\nGolden Diagnosis Validation 存在失败项：\n"

        + failed_df.to_string(

            index=False

        )

    )  # 错误结束


# ============================================================
# 18. 控制台输出
# ============================================================

print(  # 输出完成信息

    "\n================ Golden Diagnosis Set V1.0 冻结完成 ================\n"

)  # 标题结束

print(  # 输出 Case 数量

    f"Golden Case Month：{len(golden_df)}"

)  # 输出结束

print(  # 输出逻辑 Scenario 数量

    f"Logical Scenario：{golden_df['scenario_id'].nunique()}"

)  # 输出结束

print(  # 输出 NoExp

    f"No-Explanation：{APPROVED_NOEXP_CASE_ID}"

)  # 输出结束

print("\nScenario 分布：")  # 输出 Scenario

print(  # 打印类型统计

    golden_df["scenario_type"]

    .value_counts()

    .to_string()

)  # 输出结束

print("\n最终 Validation：")  # 输出 Validation

print(  # 打印 Validation

    final_validation_df.to_string(

        index=False

    )

)  # 输出结束

print("\n输出文件：")  # 输出文件

print(GOLDEN_CASE_FILE)  # Golden Case

print(GOLDEN_EXPECTATION_FILE)  # Expected Behavior

print(MANIFEST_FILE)  # Manifest

print(VALIDATION_OUTPUT)  # Validation

print(  # 最终提示

    "\nStage 3 的核心数据与 Golden Diagnosis Set 已完成。"

)  # 提示结束
