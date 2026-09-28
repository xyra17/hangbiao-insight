from pathlib import Path  # 导入 Path，用于处理目录和文件路径

import pandas as pd  # 导入 pandas，用于读取、整理和保存 Review 数据


# ============================================================
# 1. 基础目录
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前脚本所在目录

INPUT_DIR = BASE_DIR / "outputs" / "07_黄金评论构建"  # 07A 输出目录

PRELABEL_FILE = INPUT_DIR / "01_规则预标长表_v1.csv"  # 07A 生成的 Long Format 预标文件

QUEUE_FILE = INPUT_DIR / "02_人工复核队列_v1.csv"  # 07A 生成的 Review Queue

OUTPUT_DIR = BASE_DIR / "outputs" / "07_黄金评论构建" / "人工复核批次"  # 定义复核批次输出目录

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 如果输出目录不存在则创建


# ============================================================
# 2. 批次参数
# ============================================================

BATCH_SIZE = 50  # 每个复核批次最多放 50 条 Review

MAX_FINAL_LABELS = 4  # 每条 Review 最多预留 4 个正式 Topic Claim


# ============================================================
# 3. 读取 07A 数据
# ============================================================

print("正在读取 07A 预标结果……")  # 输出当前步骤

prelabels = pd.read_csv(PRELABEL_FILE)  # 读取 Long Format Rule Prelabel

queue = pd.read_csv(QUEUE_FILE)  # 读取 Review Queue

print(f"Prelabel 行数：{len(prelabels):,}")  # 输出预标总行数

print(f"Review 数量：{len(queue):,}")  # 输出 Review 总数


# ============================================================
# 4. 只保留真正命中的 Rule Candidate
# ============================================================

matched = prelabels[  # 开始筛选

    prelabels["annotation_status"] == "prelabel_only"  # 只保留真正规则命中的候选标签

].copy()  # 创建独立 DataFrame


# ============================================================
# 5. 确保关键字段不会因为 CSV 空值变成 NaN
# ============================================================

for column in [  # 遍历候选标签关键字段

    "primary_topic",  # 一级 Topic

    "secondary_topic",  # 二级 Topic

    "polarity",  # Polarity

    "evidence_span",  # Evidence

]:  # 字段列表结束

    matched[column] = matched[column].fillna("").astype(str)  # 空值转为空字符串


# ============================================================
# 6. 检测“同一个 Topic 同时出现正负 Polarity”的冲突
# ============================================================

conflict_review_ids = set()  # 用于保存存在冲突的 review_id


if not matched.empty:  # 如果确实存在候选标签

    conflict_check = (  # 开始构建冲突检查表

        matched  # 使用规则命中的标签

        .groupby(  # 按 Review + Topic 汇总

            [

                "review_id",  # Review ID

                "primary_topic",  # 一级 Topic

                "secondary_topic",  # 二级 Topic

            ]

        )["polarity"]  # 查看 polarity

        .nunique()  # 统计同一 Topic 出现多少种 Polarity

        .reset_index(name="polarity_count")  # 转回 DataFrame

    )  # 冲突检查结束


    conflict_rows = conflict_check[  # 筛出同一 Topic 同时出现多种 Polarity 的情况

        conflict_check["polarity_count"] > 1

    ].copy()  # 创建副本


    conflict_review_ids = set(  # 把冲突 Review ID 转成集合

        conflict_rows["review_id"].tolist()

    )  # 转换完成


# ============================================================
# 7. 定义明显的“品类规则误标风险”
# ============================================================

CATEGORY_INVALID_TOPICS = {  # 只定义非常明显、不应该出现在某品类中的标签

    "Ailun_Screen_Protector": {  # 钢化膜明显不应该出现充电类标签

        "charging_success",

        "charging_speed",

        "wall_socket_fit",

        "dual_port_multi_device",

    },

    "Yootech_Wireless_Charger": {  # 无线充明显不应该出现贴膜专属标签

        "protection_effectiveness",

        "bubbles_adhesion",

        "installation_accessories",

    },

    "AILKIN_USB_Wall_Charger": {  # 墙充明显不应该出现贴膜专属标签

        "protection_effectiveness",

        "bubbles_adhesion",

        "installation_accessories",

    },

}  # 品类排除规则结束


# ============================================================
# 8. 判断某条 Review 是否存在明显品类误标
# ============================================================

def detect_category_mismatch(review_id, product_name):  # 定义品类误标检查函数

    review_candidates = matched[  # 获取当前 Review 的全部 Rule Candidate

        matched["review_id"] == review_id

    ].copy()  # 创建副本


    invalid_topics = CATEGORY_INVALID_TOPICS.get(  # 获取该商品禁止出现的标签

        product_name,

        set(),

    )  # 获取完成


    if not invalid_topics:  # 如果当前商品没有定义禁止标签

        return False  # 返回无风险


    candidate_topics = set(  # 获取当前 Review 的所有二级 Topic

        review_candidates["secondary_topic"].tolist()

    )  # 转为集合


    return len(candidate_topics & invalid_topics) > 0  # 如果存在交集，则认为有明显误标风险


# ============================================================
# 9. 把规则候选压缩成 Review-level Candidate Slot
# ============================================================

candidate_map = {}  # 用于保存每条 Review 的候选标签列表


for review_id, review_candidates in matched.groupby("review_id"):  # 按 Review 分组

    review_candidates = review_candidates.sort_values(  # 按原来的 label_index 排序

        "label_index"

    )  # 排序结束


    candidates = []  # 创建当前 Review 候选列表


    for _, row in review_candidates.iterrows():  # 遍历当前 Review 的候选标签

        candidates.append({  # 保存候选标签

            "primary_topic": row["primary_topic"],  # 一级 Topic

            "secondary_topic": row["secondary_topic"],  # 二级 Topic

            "polarity": row["polarity"],  # Polarity

            "evidence_span": row["evidence_span"],  # 规则命中的 Evidence

        })  # 当前候选保存结束


    candidate_map[review_id] = candidates[:4]  # 最多保留前 4 个候选标签


# ============================================================
# 10. 创建 Review-level Master 表
# ============================================================

review_rows = []  # 用于保存最终 Review-level 数据


for _, review in queue.iterrows():  # 遍历全部 360 条 Review

    review_id = review["review_id"]  # 获取当前 Review ID

    product_name = review["product_name"]  # 获取商品名称

    candidates = candidate_map.get(  # 获取当前 Review Rule Candidate

        review_id,

        [],

    )  # 如果没有命中则为空列表


    # ========================================================
    # 11. 判断风险类型
    # ========================================================

    rule_conflict = review_id in conflict_review_ids  # 是否存在同 Topic 正负冲突

    category_mismatch = detect_category_mismatch(  # 是否存在明显品类误标

        review_id,

        product_name,

    )


    # ========================================================
    # 12. 确定复核优先级
    # ========================================================

    if rule_conflict:  # 优先级最高：同 Topic 正负冲突

        priority = "P0-A_conflict"

        priority_rank = 1

        priority_reason = "同一 Topic 被规则同时命中不同 Polarity，需要人工判断语义范围。"


    elif category_mismatch:  # 第二优先：明显品类不适用

        priority = "P0-B_category_mismatch"

        priority_rank = 2

        priority_reason = "Rule Candidate 与商品品类明显不匹配，疑似通用短语导致误标。"


    elif review["review_queue"] == "multi_candidate":  # 第三优先：多候选

        priority = "P0-C_multi_candidate"

        priority_rank = 3

        priority_reason = "规则命中多个 Topic，需要判断是否真实 Multi-label、是否遗漏或重复。"


    elif review["review_queue"] == "no_candidate":  # 第四优先：完全没有候选

        priority = "P0-D_no_candidate"

        priority_rank = 4

        priority_reason = "规则没有覆盖，但评论可能存在真实业务语义，需要完整语义判断。"


    else:  # 最后处理单一候选

        priority = "P1_single_candidate"

        priority_rank = 5

        priority_reason = "只有一个 Rule Candidate，相对简单，但进入 Golden Set 前仍需复核。"


    # ========================================================
    # 13. 创建 Review-level 基础字段
    # ========================================================

    row = {  # 创建当前 Review 的复核记录

        "annotation_id": review["annotation_id"],  # Annotation ID

        "review_id": review_id,  # Review ID

        "product_id": review["product_id"],  # 商品 ID

        "product_name": product_name,  # 商品名称

        "rating": review["rating"],  # Amazon 星级

        "title": review["title"],  # 评论标题

        "text": review["text"],  # 评论正文

        "sample_scope": review["sample_scope"],  # focus / background

        "window_signal": review["window_signal"],  # 评论窗口信号

        "rating_bucket": review["rating_bucket"],  # 星级抽样 Bucket

        "rule_candidate_count": len(candidates),  # 实际 Rule Candidate 数量

        "rule_conflict": rule_conflict,  # 是否存在 Rule Polarity 冲突

        "category_mismatch_risk": category_mismatch,  # 是否存在品类误标风险

        "review_priority": priority,  # 复核优先级

        "priority_rank": priority_rank,  # 用于程序排序

        "priority_reason": priority_reason,  # 为什么进入该优先级

    }  # 基础字段结束


    # ========================================================
    # 14. 展开最多 4 个 Rule Candidate
    # ========================================================

    for candidate_index in range(1, 5):  # 创建 Candidate 1～4

        if candidate_index <= len(candidates):  # 如果当前 Candidate 存在

            candidate = candidates[candidate_index - 1]  # 获取对应候选标签


            row[f"candidate_{candidate_index}_primary"] = candidate["primary_topic"]

            row[f"candidate_{candidate_index}_secondary"] = candidate["secondary_topic"]

            row[f"candidate_{candidate_index}_polarity"] = candidate["polarity"]

            row[f"candidate_{candidate_index}_evidence"] = candidate["evidence_span"]


        else:  # 如果当前 Candidate Slot 没有内容

            row[f"candidate_{candidate_index}_primary"] = ""

            row[f"candidate_{candidate_index}_secondary"] = ""

            row[f"candidate_{candidate_index}_polarity"] = ""

            row[f"candidate_{candidate_index}_evidence"] = ""


    # ========================================================
    # 15. 预留最终人工 / 语义复核标签
    # ========================================================

    for final_index in range(1, MAX_FINAL_LABELS + 1):  # 创建最终 Label 1～4

        row[f"final_{final_index}_primary"] = ""  # 最终一级 Topic

        row[f"final_{final_index}_secondary"] = ""  # 最终二级 Topic

        row[f"final_{final_index}_polarity"] = ""  # 最终 Polarity

        row[f"final_{final_index}_evidence"] = ""  # 最终 Evidence Span


    # ========================================================
    # 16. 预留 Review-level 复核字段
    # ========================================================

    row["review_status"] = "pending"  # 默认等待复核

    row["annotation_confidence"] = ""  # high / medium / low

    row["needs_adjudication"] = ""  # 是否需要进一步仲裁

    row["annotator"] = ""  # 标注者

    row["review_notes"] = ""  # 边界备注


    review_rows.append(row)  # 保存当前 Review


# ============================================================
# 17. 创建 Master DataFrame
# ============================================================

master_df = pd.DataFrame(review_rows)  # 转换为 DataFrame


# ============================================================
# 18. 按风险优先级排序
# ============================================================

master_df = master_df.sort_values(  # 开始排序

    [

        "priority_rank",  # 先按风险等级

        "product_name",  # 再按商品

        "annotation_id",  # 最后按 Annotation ID

    ],

    ascending=[True, True, True],  # 全部正序

).reset_index(drop=True)  # 重建索引


# ============================================================
# 19. 检查 candidate_count 是否一致
# ============================================================

queue_candidate_map = queue.set_index("review_id")["candidate_count"].to_dict()  # 获取 07A 原候选数量


master_df["candidate_count_check"] = master_df.apply(  # 检查两份文件候选数量是否一致

    lambda row: (

        int(queue_candidate_map.get(row["review_id"], 0))

        == int(row["rule_candidate_count"])

    ),

    axis=1,

)  # 检查结束


mismatch_count = (~master_df["candidate_count_check"]).sum()  # 统计不一致数量


if mismatch_count > 0:  # 如果出现数量不一致

    print(f"警告：有 {mismatch_count} 条 Review 的 Candidate 数量与 07A Queue 不一致。")


# ============================================================
# 20. 保存完整 Review-level Master
# ============================================================

master_output = OUTPUT_DIR / "01_评论复核主表_v1.csv"  # 定义 Master 文件

master_df.to_csv(  # 保存完整复核主表

    master_output,

    index=False,

    encoding="utf-8-sig",

)  # 保存结束


# ============================================================
# 21. 创建固定大小的复核批次
# ============================================================

batch_manifest_rows = []  # 用于保存每个 Batch 的信息


total_reviews = len(master_df)  # Review 总数

batch_number = 1  # 从 Batch 1 开始


for start_index in range(0, total_reviews, BATCH_SIZE):  # 每 50 条切一个 Batch

    end_index = min(  # 计算当前 Batch 结束位置

        start_index + BATCH_SIZE,

        total_reviews,

    )  # 结束位置计算完成


    batch_df = master_df.iloc[  # 截取当前 Batch

        start_index:end_index

    ].copy()  # 创建副本


    batch_filename = f"第{batch_number:02d}批.csv"  # 例如 第01批.csv

    batch_path = OUTPUT_DIR / batch_filename  # 生成完整文件路径


    batch_df.to_csv(  # 保存当前 Batch

        batch_path,

        index=False,

        encoding="utf-8-sig",

    )  # 保存结束


    batch_manifest_rows.append({  # 保存当前 Batch 信息

        "batch_id": f"BATCH-{batch_number:02d}",  # Batch ID

        "filename": batch_filename,  # 文件名

        "start_row": start_index + 1,  # Master 中开始位置

        "end_row": end_index,  # Master 中结束位置

        "review_count": len(batch_df),  # 当前 Batch Review 数

        "first_priority": batch_df["review_priority"].iloc[0],  # 当前 Batch 第一条优先级

        "last_priority": batch_df["review_priority"].iloc[-1],  # 当前 Batch 最后一条优先级

        "status": "pending",  # 当前 Batch 默认待复核

    })  # Manifest 当前行结束


    batch_number += 1  # Batch 编号加一


# ============================================================
# 22. 保存 Batch Manifest
# ============================================================

batch_manifest_df = pd.DataFrame(batch_manifest_rows)  # 创建 Batch Manifest

batch_manifest_output = OUTPUT_DIR / "02_批次清单_v1.csv"  # 定义 Manifest 文件

batch_manifest_df.to_csv(  # 保存 Manifest

    batch_manifest_output,

    index=False,

    encoding="utf-8-sig",

)  # 保存结束


# ============================================================
# 23. 生成 Priority Summary
# ============================================================

priority_summary = (  # 开始统计每种优先级 Review 数

    master_df

    .groupby(  # 按优先级汇总

        [

            "priority_rank",

            "review_priority",

        ]

    )

    .size()  # 统计数量

    .reset_index(name="review_count")  # 转换为 DataFrame

    .sort_values("priority_rank")  # 按风险顺序排序

)  # 汇总结束


priority_summary_output = OUTPUT_DIR / "03_复核优先级汇总_v1.csv"  # 定义统计文件

priority_summary.to_csv(  # 保存优先级统计

    priority_summary_output,

    index=False,

    encoding="utf-8-sig",

)  # 保存结束


# ============================================================
# 24. 再生成一个商品 × Priority 的交叉统计
# ============================================================

product_priority_summary = (  # 开始商品维度汇总

    master_df

    .groupby(  # 按商品与优先级

        [

            "product_name",

            "review_priority",

        ]

    )

    .size()  # 统计数量

    .reset_index(name="review_count")  # 转成 DataFrame

)  # 汇总结束


product_priority_output = OUTPUT_DIR / "04_商品优先级汇总_v1.csv"  # 定义文件

product_priority_summary.to_csv(  # 保存统计

    product_priority_output,

    index=False,

    encoding="utf-8-sig",

)  # 保存完成


# ============================================================
# 25. 控制台输出
# ============================================================

print("\n================ 07B Review Batch 构建完成 ================\n")

print(f"Review 总数：{len(master_df):,}")

print(f"Rule Conflict Review：{master_df['rule_conflict'].sum():,}")

print(f"Category Mismatch Risk：{master_df['category_mismatch_risk'].sum():,}")

print(f"生成 Batch 数量：{len(batch_manifest_df)}")


print("\n复核优先级：")

print(priority_summary.to_string(index=False))


print("\n批次：")

print(batch_manifest_df.to_string(index=False))


print("\n输出文件：")

print(f"Review Master：{master_output}")

print(f"Batch Manifest：{batch_manifest_output}")

print(f"Priority Summary：{priority_summary_output}")

print(f"Product × Priority：{product_priority_output}")

print(f"Batch 目录：{OUTPUT_DIR}")


# ============================================================
# 26. 最终提示
# ============================================================

print(
    "\n下一步不要直接修改 Rule Prelabel。"
)

print(
    "正式复核结果填写在 final_1 ~ final_4 字段中。"
)

print(
    "Rule Candidate 只作为参考，不是标准答案。"
)
