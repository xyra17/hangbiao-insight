from pathlib import Path  # 导入 Path，用于处理文件路径

import re  # 导入 re，用于关键词正则匹配

import numpy as np  # 导入 numpy，用于数值计算

import pandas as pd  # 导入 pandas，用于数据处理


# ============================================================
# 1. 基础目录
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前脚本所在目录

REVIEW_FILE = BASE_DIR / "data" / "01_标准化评论数据" / "评论数据集_v1.parquet"  # 真实 Review Dataset

STABLE_WINDOW_FILE = (  # 04 阶段稳定窗口

    BASE_DIR

    / "outputs"

    / "03_稳定时间窗"

    / "01_全部稳定窗口.csv"

)  # 稳定窗口路径结束

OUTPUT_DIR = (  # 09C 输出目录

    BASE_DIR

    / "outputs"

    / "09_诊断案例"

    / "语义稳定性筛选"

)  # 输出目录结束

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 创建输出目录


# ============================================================
# 2. 参数
# ============================================================

BASELINE_MONTHS = 3  # 使用当前月之前 3 个月作为 Baseline

MIN_CURRENT_REVIEWS = 80  # 当前月至少 80 条评论

MIN_BASELINE_REVIEWS = 180  # 前三个月合计至少 180 条评论

TOP_K = 3  # 一次选出 Top 3 候选，不再逐个试

AUDIT_CURRENT_SAMPLE = 20  # 每个候选当前月抽 20 条

AUDIT_BASELINE_SAMPLE = 20  # 每个候选 Baseline 抽 20 条

RANDOM_SEED = 20260911  # 固定随机种子


# ============================================================
# 3. 核心商品
# ============================================================

CORE_PRODUCTS = {  # 三个核心商品

    "B07P9S6R9Y": "Yootech_Wireless_Charger",  # 无线充

    "B0BYJ8ZVWH": "AILKIN_USB_Wall_Charger",  # 墙充

    "B07H2V5YLH": "Ailun_Screen_Protector",  # 钢化膜

}  # 商品定义结束


# ============================================================
# 4. 已使用 / 应排除月份
# ============================================================

EXCLUDED_MONTHS = {  # 已经用于其他 Case 或已经验证失败的月份

    ("B07P9S6R9Y", "2019-12"),  # 已拒绝 No-Explanation Case

    ("B07P9S6R9Y", "2020-06"),  # Explainable Negative

    ("B07P9S6R9Y", "2022-02"),  # Mixed Conflict

    ("B0BYJ8ZVWH", "2022-07"),  # Positive Recovery

    ("B0BYJ8ZVWH", "2022-08"),  # Negative Reversal

    ("B07H2V5YLH", "2022-11"),  # Positive Recovery

    ("B07H2V5YLH", "2022-12"),  # Positive Recovery

}  # 排除月份结束


# ============================================================
# 5. Topic Proxy 关键词
#
# 注意：
# 这些规则只用于候选筛选。
# 不是正式 Topic Label。
# ============================================================

TOPIC_PATTERNS = {  # 定义几个主要业务语义簇

    "stopped_working": re.compile(  # 停止工作 / 失效

        r"\b("

        r"stopped working|"

        r"stop working|"

        r"quit working|"

        r"quit charging|"

        r"stopped charging|"

        r"no longer works|"

        r"no longer charges|"

        r"died|"

        r"dead|"

        r"broke after|"

        r"failed after"

        r")\b",

        re.I,

    ),  # stopped_working 结束


    "intermittent_failure": re.compile(  # 间歇性失效

        r"\b("

        r"works sometimes|"

        r"sometimes works|"

        r"hit or miss|"

        r"cuts in and out|"

        r"on and off|"

        r"intermittent|"

        r"disconnects|"

        r"keeps disconnecting"

        r")\b",

        re.I,

    ),  # intermittent_failure 结束


    "charging_speed": re.compile(  # 充电速度

        r"\b("

        r"fast charg|"

        r"quick charg|"

        r"slow charg|"

        r"charges slowly|"

        r"charge slowly|"

        r"takes forever to charge|"

        r"charges fast|"

        r"charge fast"

        r")",

        re.I,

    ),  # charging_speed 结束


    "charging_success": re.compile(  # 是否成功充电

        r"\b("

        r"won't charge|"

        r"will not charge|"

        r"doesn't charge|"

        r"does not charge|"

        r"not charging|"

        r"never charged|"

        r"charges my|"

        r"charged my|"

        r"works great"

        r")",

        re.I,

    ),  # charging_success 结束


    "overheating": re.compile(  # 发热

        r"\b("

        r"overheat|"

        r"overheating|"

        r"very hot|"

        r"too hot|"

        r"gets hot|"

        r"get hot"

        r")\b",

        re.I,

    ),  # overheating 结束


    "electrical_safety": re.compile(  # 电气安全

        r"\b("

        r"melted|"

        r"melting|"

        r"caught fire|"

        r"fire|"

        r"burning smell|"

        r"burning scent|"

        r"shorted out|"

        r"short circuit|"

        r"hissing"

        r")\b",

        re.I,

    ),  # electrical_safety 结束


    "compatibility": re.compile(  # 兼容性

        r"\b("

        r"not compatible|"

        r"compatible with|"

        r"doesn't fit|"

        r"does not fit|"

        r"too small|"

        r"too large|"

        r"case on|"

        r"through.*case|"

        r"pop socket"

        r")",

        re.I,

    ),  # compatibility 结束


    "installation_usability": re.compile(  # 安装和易用性

        r"\b("

        r"easy to use|"

        r"easy to install|"

        r"easy install|"

        r"hard to install|"

        r"difficult to install|"

        r"air bubbles|"

        r"no bubbles|"

        r"alignment|"

        r"positioning|"

        r"exact spot"

        r")",

        re.I,

    ),  # installation_usability 结束


    "price_value": re.compile(  # 价格价值

        r"\b("

        r"great value|"

        r"good value|"

        r"worth the money|"

        r"not worth|"

        r"waste of money|"

        r"great price|"

        r"good price"

        r")",

        re.I,

    ),  # price_value 结束


    "durability": re.compile(  # 耐用性

        r"\b("

        r"durable|"

        r"sturdy|"

        r"lasted|"

        r"long time|"

        r"cracked|"

        r"breaks easily|"

        r"broke within|"

        r"short lifespan"

        r")",

        re.I,

    ),  # durability 结束

}  # Topic Proxy 定义结束


# ============================================================
# 6. Future Edit / UPDATE 风险
# ============================================================

UPDATE_PATTERN = re.compile(  # 检测可能的后续编辑文本

    r"\b("

    r"update|"

    r"updated|"

    r"edit:|"

    r"edited|"

    r"months later|"

    r"year later"

    r")\b",

    re.I,

)  # UPDATE_PATTERN 结束


# ============================================================
# 7. 读取真实 Review Dataset
# ============================================================

print("正在读取真实 Review Dataset……")  # 输出步骤

reviews = pd.read_parquet(REVIEW_FILE)  # 读取 Parquet


# ============================================================
# 8. 标准化 product_id
# ============================================================

if "product_id" in reviews.columns:  # 如果已经有正式字段

    reviews["product_id"] = reviews["product_id"].astype(str)  # 转字符串

elif "sku" in reviews.columns:  # 如果仍然使用旧版 sku

    reviews["product_id"] = reviews["sku"].astype(str)  # sku 映射为 product_id

else:  # 如果两个字段都不存在

    raise ValueError("Review Dataset 中没有 product_id 或 sku。")  # 停止


# ============================================================
# 9. 标准化 created_at
# ============================================================

if "created_at" in reviews.columns:  # 如果已经有 created_at

    reviews["created_at"] = pd.to_datetime(  # 转时间

        reviews["created_at"],

        utc=True,

        errors="coerce",

    )  # 转换结束

elif "timestamp" in reviews.columns:  # 如果只有原始 timestamp

    reviews["created_at"] = pd.to_datetime(  # 毫秒时间戳转日期

        reviews["timestamp"],

        unit="ms",

        utc=True,

        errors="coerce",

    )  # 转换结束

else:  # 时间字段缺失

    raise ValueError("Review Dataset 中没有 created_at 或 timestamp。")  # 停止


# ============================================================
# 10. 只保留三个核心商品
# ============================================================

reviews = reviews[  # 筛选核心商品

    reviews["product_id"].isin(CORE_PRODUCTS.keys())

].copy()  # 创建副本


# ============================================================
# 11. 创建 month
# ============================================================

reviews["month"] = (  # 创建月粒度时间

    reviews["created_at"]

    .dt.tz_localize(None)

    .dt.to_period("M")

    .dt.to_timestamp()

)  # month 创建结束


# ============================================================
# 12. 合并 title + text
# ============================================================

reviews["title"] = reviews["title"].fillna("").astype(str)  # 标题空值处理

reviews["text"] = reviews["text"].fillna("").astype(str)  # 正文空值处理

reviews["full_text"] = (  # 合并用于语义 Proxy 的文本

    reviews["title"]

    + " "

    + reviews["text"]

)  # full_text 结束


# ============================================================
# 13. Rating Bucket
# ============================================================

reviews["is_negative_star"] = (reviews["rating"] <= 2).astype(int)  # 1~2 星

reviews["is_positive_star"] = (reviews["rating"] >= 4).astype(int)  # 4~5 星


# ============================================================
# 14. 生成 Topic Proxy
# ============================================================

for topic_name, pattern in TOPIC_PATTERNS.items():  # 遍历 Topic Proxy

    reviews[f"proxy_{topic_name}"] = (  # 创建布尔命中字段

        reviews["full_text"]

        .str.contains(

            pattern,

            regex=True,

            na=False,

        )

        .astype(int)

    )  # Proxy 字段结束


# ============================================================
# 15. 标记 Future Update 风险
# ============================================================

reviews["has_update_marker"] = (  # 标记文本是否含 UPDATE

    reviews["full_text"]

    .str.contains(

        UPDATE_PATTERN,

        regex=True,

        na=False,

    )

    .astype(int)

)  # UPDATE 标记结束


# ============================================================
# 16. 读取 Stable Window
# ============================================================

stable_df = pd.read_csv(STABLE_WINDOW_FILE)  # 读取稳定窗口


# ============================================================
# 17. 兼容商品字段
# ============================================================

if "product_id" in stable_df.columns:  # 如果已有 product_id

    stable_product_column = "product_id"  # 使用 product_id

else:  # 否则

    stable_product_column = "sku"  # 使用旧版 sku


stable_df["product_id"] = (  # 创建统一 product_id

    stable_df[stable_product_column]

    .astype(str)

)  # product_id 结束


# ============================================================
# 18. 标准化月份
# ============================================================

stable_df["current_month"] = pd.to_datetime(  # 转成时间

    stable_df["current_month"],

    errors="coerce",

)  # 转换结束


# ============================================================
# 19. 只保留 stable
# ============================================================

stable_df = stable_df[  # 只保留 Review 稳定窗口

    stable_df["signal_type"] == "stable"

].copy()  # 创建副本


stable_df = stable_df[  # 只保留三个核心商品

    stable_df["product_id"].isin(CORE_PRODUCTS.keys())

].copy()  # 创建副本


# ============================================================
# 20. 候选评分函数
# ============================================================

def calculate_candidate_features(product_id, current_month):  # 定义候选稳定性计算函数

    current_df = reviews[  # 当前月 Review

        (

            reviews["product_id"] == product_id

        )

        &

        (

            reviews["month"] == current_month

        )

    ].copy()  # 当前月结束


    baseline_start = (  # Baseline 起点

        current_month.to_period("M")

        - BASELINE_MONTHS

    ).to_timestamp()  # 转换结束


    baseline_end = (  # Baseline 终点

        current_month.to_period("M")

        - 1

    ).to_timestamp()  # 转换结束


    baseline_df = reviews[  # 获取前三个月 Review

        (

            reviews["product_id"] == product_id

        )

        &

        (

            reviews["month"] >= baseline_start

        )

        &

        (

            reviews["month"] <= baseline_end

        )

    ].copy()  # Baseline 结束


    if len(current_df) < MIN_CURRENT_REVIEWS:  # 当前月评论过少

        return None  # 放弃候选


    if len(baseline_df) < MIN_BASELINE_REVIEWS:  # Baseline 评论过少

        return None  # 放弃候选


    result = {  # 初始化结果

        "product_id": product_id,  # 商品 ID

        "product_name": CORE_PRODUCTS[product_id],  # 商品名称

        "current_month": current_month,  # 当前月份

        "baseline_start": baseline_start,  # Baseline 开始

        "baseline_end": baseline_end,  # Baseline 结束

        "current_review_count": len(current_df),  # 当前评论数

        "baseline_review_count": len(baseline_df),  # Baseline 评论数

    }  # 初始化结束


    current_rating = current_df["rating"].mean()  # 当前平均评分

    baseline_rating = baseline_df["rating"].mean()  # Baseline 平均评分

    result["rating_delta"] = current_rating - baseline_rating  # 评分变化


    current_neg = current_df["is_negative_star"].mean()  # 当前负星比例

    baseline_neg = baseline_df["is_negative_star"].mean()  # Baseline 负星比例

    result["negative_star_delta"] = current_neg - baseline_neg  # 负星变化


    current_pos = current_df["is_positive_star"].mean()  # 当前正星比例

    baseline_pos = baseline_df["is_positive_star"].mean()  # Baseline 正星比例

    result["positive_star_delta"] = current_pos - baseline_pos  # 正星变化


    topic_deltas = []  # 保存 Topic Proxy 变化


    for topic_name in TOPIC_PATTERNS.keys():  # 遍历 Topic Proxy

        column_name = f"proxy_{topic_name}"  # 构造字段名


        baseline_ratio = baseline_df[column_name].mean()  # Baseline Topic Ratio

        current_ratio = current_df[column_name].mean()  # Current Topic Ratio

        delta = current_ratio - baseline_ratio  # Topic Ratio 变化


        result[f"baseline_{topic_name}_ratio"] = baseline_ratio  # 保存 Baseline

        result[f"current_{topic_name}_ratio"] = current_ratio  # 保存 Current

        result[f"{topic_name}_delta"] = delta  # 保存 Delta


        topic_deltas.append(abs(delta))  # 保存绝对变化


    result["mean_abs_topic_delta"] = float(  # 平均 Topic 变化

        np.mean(topic_deltas)

    )  # 平均值结束


    result["max_abs_topic_delta"] = float(  # 最大 Topic 变化

        np.max(topic_deltas)

    )  # 最大值结束


    result["current_update_ratio"] = (  # 当前月 UPDATE 比例

        current_df["has_update_marker"].mean()

    )  # Current Update Ratio 结束


    result["baseline_update_ratio"] = (  # Baseline UPDATE 比例

        baseline_df["has_update_marker"].mean()

    )  # Baseline Update Ratio 结束


    # ========================================================
    # Semantic Stability Score
    #
    # 分数越低越稳定。
    # ========================================================

    result["semantic_stability_score"] = (  # 综合稳定分数

        abs(result["rating_delta"]) * 0.20  # 评分变化

        + abs(result["negative_star_delta"]) * 1.50  # 负星比例变化

        + abs(result["positive_star_delta"]) * 1.00  # 正星比例变化

        + result["mean_abs_topic_delta"] * 3.00  # Topic 平均变化

        + result["max_abs_topic_delta"] * 2.00  # 最大 Topic 变化

        + result["current_update_ratio"] * 0.50  # 时间穿越风险惩罚

    )  # 稳定分数结束


    return result  # 返回候选特征


# ============================================================
# 21. 遍历 Stable Window
# ============================================================

candidate_rows = []  # 保存所有候选


for _, stable_row in stable_df.iterrows():  # 遍历 stable window

    product_id = str(stable_row["product_id"])  # 商品 ID

    current_month = stable_row["current_month"]  # 当前月份


    month_key = (  # 创建排除判断 Key

        product_id,

        current_month.strftime("%Y-%m"),

    )  # Key 结束


    if month_key in EXCLUDED_MONTHS:  # 如果属于已使用月份

        continue  # 跳过


    features = calculate_candidate_features(  # 计算语义稳定性

        product_id,

        current_month,

    )  # 计算结束


    if features is None:  # 如果样本量不足

        continue  # 跳过


    candidate_rows.append(features)  # 保存候选


candidate_df = pd.DataFrame(candidate_rows)  # 转成 DataFrame


if candidate_df.empty:  # 如果没有候选

    raise ValueError("没有找到符合要求的 No-Explanation Candidate。")  # 停止


# ============================================================
# 22. 排序
# ============================================================

candidate_df = candidate_df.sort_values(  # 按稳定分数排序

    [

        "semantic_stability_score",

        "current_review_count",

    ],

    ascending=[

        True,  # 分数越低越好

        False,  # 评论越多越好

    ],

).reset_index(drop=True)  # 重建索引


candidate_df["rank"] = (  # 创建排名

    np.arange(len(candidate_df))

    + 1

)  # 排名结束


# ============================================================
# 23. 选择 Top 3
# ============================================================

top_df = candidate_df.head(TOP_K).copy()  # 取 Top 3


# ============================================================
# 24. Rating Bucket
# ============================================================

def rating_bucket(rating):  # 定义星级分层

    if rating <= 2:  # 1-2 星

        return "negative"

    if rating == 3:  # 3 星

        return "neutral"

    return "positive"  # 4-5 星


reviews["rating_bucket"] = (  # 创建 Bucket

    reviews["rating"]

    .apply(rating_bucket)

)  # Bucket 创建结束


# ============================================================
# 25. 快速分层抽样函数
# ============================================================

def stratified_sample(source_df, sample_size, seed):  # 定义分层抽样

    proportions = {  # 星级抽样比例

        "negative": 0.35,

        "neutral": 0.15,

        "positive": 0.50,

    }  # 比例结束


    sampled_parts = []  # 保存抽样结果

    used_ids = set()  # 保存已选 ID


    for index, (bucket, proportion) in enumerate(  # 遍历 Bucket

        proportions.items()

    ):

        target = round(sample_size * proportion)  # 当前 Bucket 目标数


        bucket_df = source_df[  # 筛选 Bucket

            source_df["rating_bucket"] == bucket

        ].copy()  # 创建副本


        n = min(target, len(bucket_df))  # 实际抽样数量


        if n <= 0:  # 没有样本

            continue  # 跳过


        part = bucket_df.sample(  # 随机抽样

            n=n,

            random_state=seed + index,

        ).copy()  # 抽样结束


        sampled_parts.append(part)  # 保存结果

        used_ids.update(part["review_id"].tolist())  # 保存 Review ID


    if sampled_parts:  # 如果已有样本

        sampled_df = pd.concat(  # 合并

            sampled_parts,

            ignore_index=True,

        )  # 合并结束

    else:  # 没有样本

        sampled_df = source_df.head(0).copy()  # 空表


    remaining_count = sample_size - len(sampled_df)  # 还差多少条


    if remaining_count > 0:  # 如果数量不足

        remaining_df = source_df[  # 获取未抽样 Review

            ~source_df["review_id"].isin(used_ids)

        ].copy()  # 创建副本


        fill_n = min(remaining_count, len(remaining_df))  # 实际补充数


        if fill_n > 0:  # 如果还能补充

            fill_df = remaining_df.sample(  # 随机补充

                n=fill_n,

                random_state=seed + 100,

            ).copy()  # 补充结束


            sampled_df = pd.concat(  # 合并

                [

                    sampled_df,

                    fill_df,

                ],

                ignore_index=True,

            )  # 合并结束


    return sampled_df  # 返回抽样


# ============================================================
# 26. 为 Top 3 一次性构建 Audit Dataset
# ============================================================

audit_parts = []  # 保存三个候选的 Audit


for candidate_index, candidate in top_df.iterrows():  # 遍历 Top 3

    product_id = candidate["product_id"]  # 商品 ID

    current_month = candidate["current_month"]  # 当前月

    baseline_start = candidate["baseline_start"]  # Baseline 起点

    baseline_end = candidate["baseline_end"]  # Baseline 终点


    current_reviews = reviews[  # 当前月全部 Review

        (

            reviews["product_id"] == product_id

        )

        &

        (

            reviews["month"] == current_month

        )

    ].copy()  # 当前 Review 结束


    baseline_reviews = reviews[  # Baseline 全部 Review

        (

            reviews["product_id"] == product_id

        )

        &

        (

            reviews["month"] >= baseline_start

        )

        &

        (

            reviews["month"] <= baseline_end

        )

    ].copy()  # Baseline 结束


    current_sample = stratified_sample(  # 当前月抽 20 条

        current_reviews,

        AUDIT_CURRENT_SAMPLE,

        RANDOM_SEED + candidate_index * 1000,

    )  # 当前抽样结束


    baseline_sample = stratified_sample(  # Baseline 抽 20 条

        baseline_reviews,

        AUDIT_BASELINE_SAMPLE,

        RANDOM_SEED + candidate_index * 1000 + 500,

    )  # Baseline 抽样结束


    current_sample["audit_window"] = "current"  # 标记 Current

    baseline_sample["audit_window"] = "baseline"  # 标记 Baseline


    candidate_audit = pd.concat(  # 合并当前候选的 40 条

        [

            baseline_sample,

            current_sample,

        ],

        ignore_index=True,

    )  # 合并结束


    candidate_audit["candidate_rank"] = int(  # 保存候选排名

        candidate["rank"]

    )  # 排名结束


    candidate_audit["candidate_id"] = (  # 创建候选 ID

        "NOEXP-CAND-"

        + str(int(candidate["rank"])).zfill(2)

    )  # ID 结束


    candidate_audit["candidate_month"] = current_month  # 保存 Candidate Month

    candidate_audit["semantic_stability_score"] = (  # 保存自动筛选分数

        candidate["semantic_stability_score"]

    )  # 分数结束


    audit_parts.append(candidate_audit)  # 保存候选 Audit


audit_df = pd.concat(  # 合并 Top 3 共 120 条

    audit_parts,

    ignore_index=True,

)  # 合并结束


# ============================================================
# 27. 整理 Audit 字段
# ============================================================

audit_columns = [  # 定义最终 Audit 字段

    "candidate_id",

    "candidate_rank",

    "candidate_month",

    "semantic_stability_score",

    "audit_window",

    "review_id",

    "product_id",

    "created_at",

    "rating",

    "rating_bucket",

    "title",

    "text",

]  # 字段结束


audit_df = audit_df[audit_columns].copy()  # 只保留需要字段


audit_df["audit_primary_topic"] = ""  # 预留人工 Topic

audit_df["audit_secondary_topic"] = ""  # 预留二级 Topic

audit_df["audit_polarity"] = ""  # 预留 Polarity

audit_df["audit_evidence"] = ""  # 预留 Evidence

audit_df["semantic_shift_found"] = ""  # 是否发现语义变化

audit_df["audit_notes"] = ""  # 预留备注


# ============================================================
# 28. 输出文件
# ============================================================

ranking_output = OUTPUT_DIR / "01_无解释候选排序_v2.csv"  # 全部候选排名

top_output = OUTPUT_DIR / "02_无解释Top3候选_v2.csv"  # Top 3

audit_output = OUTPUT_DIR / "03_无解释Top3语义审核_v2.csv"  # Top3 审计样本

validation_output = OUTPUT_DIR / "04_无解释筛选校验_v2.csv"  # Validation


candidate_df.to_csv(  # 保存完整 Ranking

    ranking_output,

    index=False,

    encoding="utf-8-sig",

)  # 保存结束


top_df.to_csv(  # 保存 Top 3

    top_output,

    index=False,

    encoding="utf-8-sig",

)  # 保存结束


audit_df.to_csv(  # 保存 120 条 Audit

    audit_output,

    index=False,

    encoding="utf-8-sig",

)  # 保存结束


# ============================================================
# 29. Validation
# ============================================================

validation_df = pd.DataFrame([  # 创建 Validation

    {

        "check_name": "candidate_count_ge_3",

        "expected": 3,

        "actual": len(candidate_df),

        "passed": len(candidate_df) >= 3,

    },

    {

        "check_name": "top3_count",

        "expected": 3,

        "actual": len(top_df),

        "passed": len(top_df) == 3,

    },

    {

        "check_name": "audit_total_count",

        "expected": TOP_K * (

            AUDIT_CURRENT_SAMPLE

            + AUDIT_BASELINE_SAMPLE

        ),

        "actual": len(audit_df),

        "passed": len(audit_df)

        == TOP_K

        * (

            AUDIT_CURRENT_SAMPLE

            + AUDIT_BASELINE_SAMPLE

        ),

    },

    {

        "check_name": "rejected_2019_12_excluded",

        "expected": 0,

        "actual": int(

            (

                (

                    candidate_df["product_id"]

                    == "B07P9S6R9Y"

                )

                &

                (

                    candidate_df["current_month"]

                    == pd.Timestamp("2019-12-01")

                )

            ).sum()

        ),

        "passed": bool(

            (

                (

                    (

                        candidate_df["product_id"]

                        == "B07P9S6R9Y"

                    )

                    &

                    (

                        candidate_df["current_month"]

                        == pd.Timestamp("2019-12-01")

                    )

                ).sum()

                == 0

            )

        ),

    },

])  # Validation 创建结束


validation_df.to_csv(  # 保存 Validation

    validation_output,

    index=False,

    encoding="utf-8-sig",

)  # 保存结束


# ============================================================
# 30. 控制台输出
# ============================================================

print("\n================ 09C No-Explanation 重筛完成 ================\n")  # 标题


print(f"符合条件候选数：{len(candidate_df)}")  # 输出候选数


print("\nTop 3 Candidate：")  # 输出 Top 3


print(  # 打印 Top 3

    top_df[

        [

            "rank",

            "product_name",

            "current_month",

            "current_review_count",

            "baseline_review_count",

            "rating_delta",

            "negative_star_delta",

            "mean_abs_topic_delta",

            "max_abs_topic_delta",

            "semantic_stability_score",

        ]

    ]

    .to_string(

        index=False

    )

)  # 打印结束


print("\nSemantic Audit：")  # Audit 信息

print(

    f"Top 3 × "

    f"({AUDIT_BASELINE_SAMPLE} Baseline + "

    f"{AUDIT_CURRENT_SAMPLE} Current) "

    f"= {len(audit_df)} 条"

)  # Audit 数量结束


print("\nValidation：")  # 输出 Validation

print(

    validation_df.to_string(

        index=False

    )

)  # 输出结束


print("\n输出文件：")  # 输出文件

print(ranking_output)  # Ranking

print(top_output)  # Top3

print(audit_output)  # Audit

print(validation_output)  # Validation


print(

    "\n下一步只需要把 03_无解释Top3语义审核_v2.csv 发给我，"

    "我一次审完三个候选并直接选最终 No-Explanation Month。"

)  # 下一步提示
