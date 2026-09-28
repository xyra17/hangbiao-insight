from pathlib import Path  # 导入 Path，用于统一处理文件和目录路径

import pandas as pd  # 导入 pandas，用于读取、处理和保存表格数据


# ============================================================
# 1. 基础目录
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前 09 脚本所在目录

REVIEW_DATA_DIR = BASE_DIR / "data" / "01_标准化评论数据"  # 标准化评论数据目录

GOLDEN_REVIEW_DIR = BASE_DIR / "data" / "03_黄金评论集"  # 黄金评论数据目录

BUSINESS_OUTPUT_DIR = BASE_DIR / "outputs" / "08_经营指标"  # 定义 08 阶段输出目录

REVIEW_EDA_DIR = BASE_DIR / "outputs" / "02_评论探索分析"  # 定义 03 阶段 Review EDA 输出目录

OUTPUT_DIR = BASE_DIR / "outputs" / "09_诊断案例"  # 定义 09 阶段输出目录

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 如果 09 输出目录不存在则自动创建


# ============================================================
# 2. 输入文件
# ============================================================

REVIEW_FILE = REVIEW_DATA_DIR / "评论数据集_v1.parquet"  # 真实 Amazon Review Dataset

GOLDEN_REVIEW_MASTER_FILE = GOLDEN_REVIEW_DIR / "黄金评论主表_v1_0.csv"  # 冻结后的 360 条 Golden Review Master

GOLDEN_LABEL_FILE = GOLDEN_REVIEW_DIR / "黄金评论标签表_v1_0.csv"  # 冻结后的 657 条 Golden Topic Claim

MONTHLY_BUSINESS_FILE = BUSINESS_OUTPUT_DIR / "01_经营指标月表_v1.csv"  # 08 阶段月度经营指标

SCENARIO_FILE = BUSINESS_OUTPUT_DIR / "02_异常注入方案_v1.csv"  # 08 阶段 Scenario Injection Plan

MONTHLY_REVIEW_FILE = REVIEW_EDA_DIR / "02_评论月度指标.csv"  # 03 阶段真实评论月度统计


# ============================================================
# 3. 输出文件
# ============================================================

CASE_OUTPUT = OUTPUT_DIR / "01_诊断案例候选_v1.csv"  # 完整 Diagnosis Case Candidate

NOEXP_AUDIT_OUTPUT = OUTPUT_DIR / "02_无解释语义审核_v1.csv"  # No-Explanation 语义审计样本

CASE_SUMMARY_OUTPUT = OUTPUT_DIR / "03_诊断案例汇总_v1.csv"  # 精简版 Case Summary

VALIDATION_OUTPUT = OUTPUT_DIR / "04_诊断案例校验_v1.csv"  # 09 阶段质量校验结果


# ============================================================
# 4. 参数
# ============================================================

BASELINE_MONTHS = 3  # 当前月以前 3 个月作为统一 Baseline

NOEXP_CURRENT_SAMPLE = 40  # No-Explanation 当前异常月抽取 40 条真实评论

NOEXP_BASELINE_SAMPLE = 60  # No-Explanation 前三个月一共抽取 60 条真实评论

RANDOM_SEED = 20260911  # 固定随机种子，保证每次抽样结果可复现


# ============================================================
# 5. 检查必要文件是否存在
# ============================================================

REQUIRED_FILES = [  # 定义 09 必须依赖的输入文件

    REVIEW_FILE,  # 原始真实评论数据

    GOLDEN_REVIEW_MASTER_FILE,  # Golden Review Master

    GOLDEN_LABEL_FILE,  # Golden Topic Claim

    MONTHLY_BUSINESS_FILE,  # 月度经营指标

    SCENARIO_FILE,  # 场景设计

    MONTHLY_REVIEW_FILE,  # Review 月度统计

]  # 必须文件列表结束


for required_file in REQUIRED_FILES:  # 逐个检查文件是否存在

    if not required_file.exists():  # 如果某个文件不存在

        raise FileNotFoundError(  # 立即停止程序并提示缺少哪个文件

            f"\n缺少必要文件：\n{required_file}\n"

            f"请先确认文件已经放到对应目录。"

        )  # 错误信息结束


# ============================================================
# 6. 读取 Scenario Plan
# ============================================================

print("正在读取 Scenario Plan……")  # 输出当前执行步骤

scenario_df = pd.read_csv(  # 读取 08 阶段场景文件

    SCENARIO_FILE  # 使用 Scenario 文件路径

)  # Scenario 读取结束


scenario_df["product_id"] = (  # 强制统一 product_id 类型

    scenario_df["product_id"]  # 获取商品 ID

    .astype(str)  # 转成字符串

)  # 类型转换结束


scenario_df["month"] = pd.to_datetime(  # 将 Scenario 月份转换成时间格式

    scenario_df["month"],  # 使用 month 字段

    errors="coerce",  # 无法解析的时间设为 NaT

)  # 时间转换结束


if scenario_df["month"].isna().any():  # 如果存在无法解析的 Scenario 月份

    raise ValueError(  # 停止程序

        "Scenario Plan 中存在无法解析的 month。"

    )  # 错误结束


# ============================================================
# 7. 为每个 Scenario Month 创建唯一 case_id
# ============================================================

scenario_df["case_id"] = (  # 创建唯一 Diagnosis Case ID

    scenario_df["scenario_id"].astype(str)  # 使用 Scenario ID

    + "__"  # 使用双下划线分隔

    + scenario_df["month"].dt.strftime("%Y-%m")  # 加入具体月份

)  # case_id 创建结束


if scenario_df["case_id"].duplicated().any():  # 如果 case_id 仍然重复

    raise ValueError(  # 停止程序

        "Scenario Plan 中存在重复 case_id。"

    )  # 错误结束


# ============================================================
# 8. 读取 Synthetic Business Metric
# ============================================================

business_df = pd.read_csv(  # 读取 08 阶段月度经营指标

    MONTHLY_BUSINESS_FILE  # 使用月度经营指标文件

)  # 读取结束


business_df["product_id"] = (  # 统一商品 ID 类型

    business_df["product_id"]  # 获取 product_id

    .astype(str)  # 转成字符串

)  # 类型转换结束


business_df["month"] = pd.to_datetime(  # 标准化月度指标月份

    business_df["month"],  # 使用 month 字段

    errors="coerce",  # 异常时间设为 NaT

)  # 时间转换结束


if business_df["month"].isna().any():  # 如果经营指标存在非法时间

    raise ValueError(  # 停止程序

        "01_经营指标月表_v1.csv 中存在非法 month。"

    )  # 错误结束


# ============================================================
# 9. 计算 avg_daily_orders
# ============================================================

business_df["days_in_month"] = (  # 获取每个月实际天数

    business_df["month"]  # 使用月份

    .dt.days_in_month  # 计算该月总天数

)  # 月天数计算结束


business_df["avg_daily_orders"] = (  # 计算每月平均每日订单量

    business_df["order_count"]  # 使用该月订单总数

    / business_df["days_in_month"]  # 除以当月天数

)  # 日均订单计算结束


# ============================================================
# 10. 读取真实 Review Monthly Metric
# ============================================================

review_monthly_df = pd.read_csv(  # 读取真实 Amazon Review 月度统计

    MONTHLY_REVIEW_FILE  # 使用 03 阶段月度文件

)  # 读取结束


# ============================================================
# 11. 兼容 product_id / sku 两种旧命名
# ============================================================

if "product_id" in review_monthly_df.columns:  # 如果文件里已经有 product_id

    review_product_column = "product_id"  # 直接使用 product_id

elif "sku" in review_monthly_df.columns:  # 如果旧版本仍然叫 sku

    review_product_column = "sku"  # 使用旧版 sku

else:  # 如果两个字段都不存在

    raise ValueError(  # 停止程序

        "02_评论月度指标.csv 中没有 product_id 或 sku 字段。"

    )  # 错误结束


review_monthly_df["product_id"] = (  # 创建统一内部商品 ID

    review_monthly_df[review_product_column]  # 获取真实字段

    .astype(str)  # 转成字符串

)  # product_id 生成结束


# ============================================================
# 12. 兼容 Review 月份字段
# ============================================================

review_month_column = None  # 初始化 Review 月份字段名


for candidate_column in [  # 遍历可能存在的月份字段

    "month",  # 优先使用标准 month

    "current_month",  # 兼容旧版 current_month

]:  # 候选月份字段结束

    if candidate_column in review_monthly_df.columns:  # 如果当前字段存在

        review_month_column = candidate_column  # 保存实际字段名

        break  # 找到后结束循环


if review_month_column is None:  # 如果两个字段都没找到

    raise ValueError(  # 停止程序

        "02_评论月度指标.csv 中没有找到 month/current_month 字段。"

    )  # 错误结束


review_monthly_df["month"] = pd.to_datetime(  # 创建统一月份字段

    review_monthly_df[review_month_column],  # 使用实际月份列

    errors="coerce",  # 无法解析的时间设为 NaT

)  # 时间转换结束


if review_monthly_df["month"].isna().any():  # 如果存在非法月份

    raise ValueError(  # 停止程序

        "02_评论月度指标.csv 中存在非法月份。"

    )  # 错误结束


# ============================================================
# 13. 读取冻结 Golden Review Master
# ============================================================

golden_review_df = pd.read_csv(  # 读取冻结后的 360 条 Review Master

    GOLDEN_REVIEW_MASTER_FILE  # 使用 Golden Review Master 文件

)  # 读取结束


golden_review_df["review_id"] = (  # 强制 Review ID 为字符串

    golden_review_df["review_id"]  # 获取 Review ID

    .astype(str)  # 转成字符串

)  # 类型转换结束


golden_review_df["product_id"] = (  # 强制 product_id 为字符串

    golden_review_df["product_id"]  # 获取商品 ID

    .astype(str)  # 转成字符串

)  # 类型转换结束


if len(golden_review_df) != 360:  # 检查 Golden Review 数量

    raise ValueError(  # 如果不是 360 条则停止

        f"Golden Review Master 应为 360 条，实际 {len(golden_review_df)} 条。"

    )  # 错误结束


if golden_review_df["review_id"].duplicated().any():  # 检查 Review ID 是否唯一

    raise ValueError(  # 如果存在重复则停止

        "Golden Review Master 中存在重复 review_id。"

    )  # 错误结束


# ============================================================
# 14. 读取冻结 Golden Topic Claim
# ============================================================

golden_label_df = pd.read_csv(  # 读取冻结的 Golden Topic Claim

    GOLDEN_LABEL_FILE  # 使用 Golden Label 文件

)  # 读取结束


golden_label_df["review_id"] = (  # 强制 Review ID 为字符串

    golden_label_df["review_id"]  # 获取 Review ID

    .astype(str)  # 转成字符串

)  # 类型转换结束


golden_label_df["product_id"] = (  # 强制商品 ID 为字符串

    golden_label_df["product_id"]  # 获取商品 ID

    .astype(str)  # 转成字符串

)  # 类型转换结束


if len(golden_label_df) != 657:  # 检查冻结 Golden Claim 数量

    raise ValueError(  # 数量不一致则停止

        f"Golden Topic Claim 应为 657 条，实际 {len(golden_label_df)} 条。"

    )  # 错误结束


# ============================================================
# 15. 从原始 Review Dataset 补回真实 created_at
# ============================================================

print("正在从原始 Review Dataset 补充 created_at……")  # 输出当前步骤


raw_columns = pd.read_parquet(  # 只读取零行附近 schema 并不方便，因此直接读取必要字段

    REVIEW_FILE  # 读取原始 Review Parquet

)  # 原始 Review Dataset 读取结束


# ============================================================
# 16. 检查原始 Review Dataset 的时间字段
# ============================================================

if "created_at" in raw_columns.columns:  # 如果正式 Dataset 已经有 created_at

    raw_review_df = raw_columns.copy()  # 直接使用完整数据

elif "timestamp" in raw_columns.columns:  # 如果只有 Amazon 原始毫秒时间戳

    raw_review_df = raw_columns.copy()  # 创建副本

    raw_review_df["created_at"] = pd.to_datetime(  # 将毫秒时间戳转换为时间

        raw_review_df["timestamp"],  # 使用 timestamp

        unit="ms",  # Amazon Timestamp 为毫秒

        utc=True,  # 转成 UTC

        errors="coerce",  # 异常设为 NaT

    )  # created_at 创建结束

else:  # 如果两个时间字段都不存在

    raise ValueError(  # 停止程序

        "评论数据集_v1.parquet 中既没有 created_at，也没有 timestamp。"

    )  # 错误结束


# ============================================================
# 17. 标准化原始 Review Dataset 字段
# ============================================================

raw_review_df["review_id"] = (  # 统一 Review ID

    raw_review_df["review_id"]  # 使用原始 review_id

    .astype(str)  # 转成字符串

)  # Review ID 结束


if "product_id" in raw_review_df.columns:  # 如果已有正式 product_id

    raw_review_df["product_id"] = (  # 标准化商品 ID

        raw_review_df["product_id"]  # 使用 product_id

        .astype(str)  # 转成字符串

    )  # 类型转换结束

elif "sku" in raw_review_df.columns:  # 如果还是旧版 sku

    raw_review_df["product_id"] = (  # 将旧 sku 映射为内部 product_id

        raw_review_df["sku"]  # 当前 sku 实际存 parent_asin

        .astype(str)  # 转成字符串

    )  # product_id 创建结束

else:  # 如果商品字段不存在

    raise ValueError(  # 停止程序

        "评论数据集_v1.parquet 中没有 product_id 或 sku 字段。"

    )  # 错误结束


raw_review_df["created_at"] = pd.to_datetime(  # 再次统一 created_at

    raw_review_df["created_at"],  # 使用 created_at

    utc=True,  # 强制 UTC

    errors="coerce",  # 异常值转 NaT

)  # 时间标准化结束


# ============================================================
# 18. 检查原始 Review ID 是否唯一
# ============================================================

duplicate_raw_review_ids = int(  # 统计重复 Review ID

    raw_review_df["review_id"]  # 使用 review_id

    .duplicated()  # 判断重复

    .sum()  # 统计数量

)  # 重复数量结束


if duplicate_raw_review_ids > 0:  # 如果存在重复 Review ID

    raise ValueError(  # 停止程序

        f"评论数据集_v1 中发现 {duplicate_raw_review_ids} 个重复 review_id。"

    )  # 错误结束


# ============================================================
# 19. 创建 Review 时间映射表
# ============================================================

review_time_df = raw_review_df[  # 只保留给 Golden Review 补时间需要的字段

    [

        "review_id",  # Review ID

        "created_at",  # 真实 Review 时间

    ]

].copy()  # 创建副本


# ============================================================
# 20. 合并 created_at 到 Golden Review Master
# ============================================================

golden_review_df = golden_review_df.drop(  # 防止以后新版 Master 自带 created_at

    columns=["created_at"],  # 删除可能已有的 created_at

    errors="ignore",  # 没有这个字段也不报错

)  # 删除结束


golden_review_df = golden_review_df.merge(  # 根据稳定 Review ID 补真实时间

    review_time_df,  # Review 时间映射

    on="review_id",  # 使用 review_id 关联

    how="left",  # 360 条 Golden Review 一条也不能丢

    validate="one_to_one",  # 两边必须一一对应

)  # 合并结束


missing_created_at_count = int(  # 统计无法补回时间的 Golden Review

    golden_review_df["created_at"]  # 使用 created_at

    .isna()  # 判断空值

    .sum()  # 统计数量

)  # 统计结束


if missing_created_at_count > 0:  # 如果存在无法关联的 Review

    missing_review_ids = (  # 获取前几个异常 Review ID

        golden_review_df.loc[

            golden_review_df["created_at"].isna(),  # 时间为空

            "review_id",  # 获取 Review ID

        ]

        .head(10)  # 最多展示 10 条

        .tolist()  # 转换成列表

    )  # ID 获取结束


    raise ValueError(  # 停止程序

        f"{missing_created_at_count} 条 Golden Review 无法补回 created_at："

        f"{missing_review_ids}"

    )  # 错误结束


# ============================================================
# 21. 创建 Golden Review Month
# ============================================================

golden_review_df["month"] = (  # 根据真实评论时间创建月粒度字段

    golden_review_df["created_at"]  # 使用真实 created_at

    .dt.tz_localize(None)  # 去掉时区方便与其他月份比较

    .dt.to_period("M")  # 转成月 Period

    .dt.to_timestamp()  # 转回 Timestamp

)  # Golden Review month 创建结束


# ============================================================
# 22. 把 Golden Claim 与真实 Review 时间合并
# ============================================================

golden_claim_df = golden_label_df.merge(  # 给每个 Topic Claim 补 Review 时间与正文

    golden_review_df[  # 只保留需要的 Review 信息

        [

            "review_id",  # Review ID

            "created_at",  # Review 时间

            "month",  # Review 月份

            "title",  # Review 标题

            "text",  # Review 正文

        ]

    ],

    on="review_id",  # 使用 review_id 关联

    how="left",  # 所有 Golden Claim 都必须保留

    validate="many_to_one",  # 一个 Review 可以有多个 Topic Claim

)  # Claim 上下文合并结束


missing_claim_month_count = int(  # 统计没有成功关联月份的 Claim

    golden_claim_df["month"]  # 使用 month

    .isna()  # 判断是否为空

    .sum()  # 统计数量

)  # 统计结束


if missing_claim_month_count > 0:  # 如果存在关联失败

    raise ValueError(  # 停止程序

        f"{missing_claim_month_count} 个 Golden Topic Claim 没有关联到 Review Month。"

    )  # 错误结束


print(  # 输出补时间结果

    f"Golden Review 时间补充完成："

    f"{len(golden_review_df)} 条 Review，"

    f"{len(golden_claim_df)} 个 Topic Claim。"

)  # 输出结束


# ============================================================
# 23. Business Baseline 计算函数
# ============================================================

def get_business_baseline(product_id, current_month):  # 定义经营指标 Baseline 计算函数

    product_df = business_df[  # 获取当前商品所有月度经营指标

        business_df["product_id"] == product_id  # 只保留当前商品

    ].copy()  # 创建副本


    current_df = product_df[  # 获取当前 Scenario Month

        product_df["month"] == current_month  # 匹配当前月份

    ].copy()  # 创建副本


    if len(current_df) != 1:  # 当前月份必须正好一行

        raise ValueError(  # 否则停止

            f"{product_id} | {current_month:%Y-%m} 的 Business Metric 数量异常："

            f"{len(current_df)}"

        )  # 错误结束


    current = current_df.iloc[0]  # 获取当前月份指标


    baseline_start = (  # 计算三个月 Baseline 开始月份

        current_month.to_period("M")  # 当前月份转成 Period

        - BASELINE_MONTHS  # 向前移动 3 个月

    ).to_timestamp()  # 转回 Timestamp


    baseline_end = (  # 计算 Baseline 结束月份

        current_month.to_period("M")  # 当前月份

        - 1  # 上一个月

    ).to_timestamp()  # 转回 Timestamp


    baseline_df = product_df[  # 筛选前三个月经营数据

        (

            product_df["month"] >= baseline_start  # 大于等于开始月份

        )

        &

        (

            product_df["month"] <= baseline_end  # 小于等于结束月份

        )

    ].copy()  # 创建副本


    if len(baseline_df) != BASELINE_MONTHS:  # 必须有完整 3 个月

        raise ValueError(  # 如果不完整则停止

            f"{product_id} | {current_month:%Y-%m} "

            f"Business Baseline 不完整，实际 {len(baseline_df)} 个月。"

        )  # 错误结束


    baseline_avg_daily_orders = (  # 计算前三个月日均订单量平均

        baseline_df["avg_daily_orders"].mean()  # 求三个月平均

    )  # Baseline 日订单结束


    baseline_refund_rate = (  # 使用整体退款数 / 整体订单数计算 Baseline 退款率

        baseline_df["refund_count"].sum()  # 三个月退款总数

        / baseline_df["order_count"].sum()  # 三个月订单总数

    )  # Baseline 退款率结束


    return {  # 返回经营侧 Case Feature

        "business_baseline_start": baseline_start,  # Baseline 开始月

        "business_baseline_end": baseline_end,  # Baseline 结束月

        "baseline_avg_daily_orders": baseline_avg_daily_orders,  # Baseline 日均订单

        "current_avg_daily_orders": current["avg_daily_orders"],  # 当前月日均订单

        "order_change_pct": (  # 计算订单变化比例

            current["avg_daily_orders"]  # 当前月日均订单

            / baseline_avg_daily_orders  # 除以 Baseline

            - 1.0  # 转成变化率

        ),  # 订单变化率结束

        "baseline_refund_rate": baseline_refund_rate,  # Baseline 退款率

        "current_refund_rate": current["refund_rate"],  # 当前月退款率

        "refund_rate_delta": (  # 计算退款率绝对变化

            current["refund_rate"]  # 当前退款率

            - baseline_refund_rate  # 减去 Baseline

        ),  # 退款率变化结束

    }  # 返回结束


# ============================================================
# 24. Review Baseline 计算函数
# ============================================================

def get_review_baseline(product_id, current_month):  # 定义真实 Review Baseline 函数

    product_df = review_monthly_df[  # 获取当前商品月度 Review 指标

        review_monthly_df["product_id"] == product_id  # 只保留目标商品

    ].copy()  # 创建副本


    current_df = product_df[  # 获取 Scenario 当前月 Review 指标

        product_df["month"] == current_month  # 匹配当前月份

    ].copy()  # 创建副本


    if len(current_df) != 1:  # 当前月必须正好一行

        raise ValueError(  # 否则停止

            f"{product_id} | {current_month:%Y-%m} 的 Review Metric 数量异常："

            f"{len(current_df)}"

        )  # 错误结束


    current = current_df.iloc[0]  # 获取当前 Review 指标


    baseline_start = (  # 计算前三个月起始月

        current_month.to_period("M")  # 当前月份

        - BASELINE_MONTHS  # 向前移动三个月

    ).to_timestamp()  # 转回时间


    baseline_end = (  # 计算前三个月结束月

        current_month.to_period("M")  # 当前月份

        - 1  # 上一个月

    ).to_timestamp()  # 转回时间


    baseline_df = product_df[  # 获取 Review Baseline 三个月

        (

            product_df["month"] >= baseline_start  # 大于等于开始月

        )

        &

        (

            product_df["month"] <= baseline_end  # 小于等于结束月

        )

    ].copy()  # 创建副本


    if len(baseline_df) != BASELINE_MONTHS:  # 必须正好三个月

        raise ValueError(  # 否则停止

            f"{product_id} | {current_month:%Y-%m} "

            f"Review Baseline 不完整，实际 {len(baseline_df)} 个月。"

        )  # 错误结束


    result = {  # 初始化 Review Side Feature

        "review_baseline_start": baseline_start,  # Baseline 开始月份

        "review_baseline_end": baseline_end,  # Baseline 结束月份

    }  # 基础结果结束


    review_metrics = [  # 定义需要比较的 Review 月度指标

        "review_count",  # Review 数量

        "avg_rating",  # 平均评分

        "positive_ratio",  # 星级正向比例

        "neutral_ratio",  # 星级中性比例

        "negative_ratio",  # 星级负向比例

    ]  # Review Metric 列表结束


    for metric_name in review_metrics:  # 逐个计算 Review Metric

        if metric_name not in product_df.columns:  # 如果旧版 EDA 没有某字段

            continue  # 直接跳过该字段


        baseline_value = (  # 计算前三个月平均值

            pd.to_numeric(  # 强制转成数值

                baseline_df[metric_name],  # 使用对应指标

                errors="coerce",  # 非法值转 NaN

            )

            .mean()  # 计算三个月平均

        )  # Baseline Value 结束


        current_value = pd.to_numeric(  # 获取当前月指标并转数值

            pd.Series([current[metric_name]]),  # 临时转成 Series

            errors="coerce",  # 异常转 NaN

        ).iloc[0]  # 获取单值


        result[f"baseline_{metric_name}"] = baseline_value  # 保存 Baseline

        result[f"current_{metric_name}"] = current_value  # 保存 Current

        result[f"{metric_name}_delta"] = (  # 计算当前月与 Baseline 差异

            current_value  # 当前值

            - baseline_value  # 减去 Baseline

        )  # Delta 结束


    return result  # 返回 Review Side Feature


# ============================================================
# 25. 获取 Scenario Month 中已有 Golden Evidence
# ============================================================

def get_golden_evidence(product_id, current_month):  # 定义获取人工 Golden Claim Preview 的函数

    evidence_df = golden_claim_df[  # 筛选当前商品当前月份的 Golden Claim

        (

            golden_claim_df["product_id"] == product_id  # 商品一致

        )

        &

        (

            golden_claim_df["month"] == current_month  # 月份一致

        )

    ].copy()  # 创建副本


    if evidence_df.empty:  # 如果当前月刚好没有抽到 Golden Review

        return "", 0  # 返回空 Preview 和 0 条 Claim


    confidence_rank = {  # 定义置信等级排序

        "high": 0,  # 高置信优先

        "medium": 1,  # 中置信其次

        "low": 2,  # 低置信最后

    }  # 排序映射结束


    evidence_df["confidence_rank"] = (  # 创建排序字段

        evidence_df["annotation_confidence"]  # 使用置信等级

        .map(confidence_rank)  # 转成数字

        .fillna(9)  # 未知状态排最后

    )  # 排序字段创建结束


    evidence_df = evidence_df.sort_values(  # 排序 Golden Evidence

        [

            "confidence_rank",  # 先看置信度

            "claim_id",  # 再按 Claim ID

        ]

    )  # 排序结束


    evidence_parts = []  # 创建 Evidence Preview 列表


    for _, evidence_row in evidence_df.head(8).iterrows():  # 最多展示前 8 个 Claim

        evidence_parts.append(  # 将 Topic、Polarity、Evidence 拼接

            (

                f"{evidence_row['secondary_topic']}"

                f"[{evidence_row['polarity']}]"

                f": {evidence_row['evidence_span']}"

            )

        )  # Preview 添加结束


    return (  # 返回 Preview 和完整 Claim 数

        " || ".join(evidence_parts),  # 拼接 Preview

        len(evidence_df),  # 当前月 Golden Claim 总数

    )  # 返回结束


# ============================================================
# 26. 构建 Diagnosis Case Candidate
# ============================================================

case_rows = []  # 创建 Case Candidate 列表


for _, scenario in scenario_df.iterrows():  # 遍历 08 阶段全部 Scenario Month

    product_id = str(scenario["product_id"])  # 获取商品 ID

    current_month = scenario["month"]  # 获取当前月份


    business_features = get_business_baseline(  # 获取经营侧指标变化

        product_id,  # 当前商品

        current_month,  # 当前月份

    )  # Business Feature 结束


    review_features = get_review_baseline(  # 获取真实 Review 聚合变化

        product_id,  # 当前商品

        current_month,  # 当前月份

    )  # Review Feature 结束


    evidence_preview, evidence_count = get_golden_evidence(  # 获取人工 Golden Evidence Preview

        product_id,  # 当前商品

        current_month,  # 当前月份

    )  # Evidence Preview 获取结束


    if scenario["scenario_type"] == "no_review_explanation":  # 如果是 No-Explanation Case

        case_status = "candidate_requires_semantic_audit"  # 必须经过额外语义审计

    else:  # 其他 Case

        case_status = "candidate"  # 暂时为普通 Candidate


    case_row = {  # 创建当前 Diagnosis Case

        "case_id": scenario["case_id"],  # 唯一 Case ID

        "scenario_id": scenario["scenario_id"],  # 原始 Scenario ID

        "scenario_type": scenario["scenario_type"],  # 场景类型

        "product_id": product_id,  # 商品 ID

        "product_name": scenario["product_name"],  # 商品名称

        "current_month": current_month,  # 当前异常月份

        "expected_review_relation": scenario["expected_review_relation"],  # 评论与经营异常预期关系

        "case_status": case_status,  # 当前 Case 状态

        "business_source": "synthetic",  # 明确经营指标来自 synthetic

        "review_source": "real_amazon_reviews",  # 评论来自真实 Amazon Review

        "golden_review_source": "human_reviewed_frozen_subset",  # Golden Claim 来自冻结人工复核子集

        "causal_claim_allowed": False,  # 永远禁止直接输出因果结论

        "design_note": scenario["design_note"],  # 08 阶段场景设计说明

        "golden_evidence_claim_count": evidence_count,  # 当前月已有 Golden Claim 数

        "golden_evidence_preview": evidence_preview,  # 当前月人工 Evidence Preview

    }  # 基础 Case 信息结束


    case_row.update(business_features)  # 将 Business Feature 合并进 Case

    case_row.update(review_features)  # 将 Review Feature 合并进 Case


    case_rows.append(case_row)  # 保存当前 Case


case_df = pd.DataFrame(  # 将 Case 列表转成 DataFrame

    case_rows  # 使用所有 Scenario Case

)  # DataFrame 创建结束


# ============================================================
# 27. 保存完整 Diagnosis Case Candidate
# ============================================================

case_df.to_csv(  # 保存完整 Candidate

    CASE_OUTPUT,  # 输出文件

    index=False,  # 不保存 pandas 索引

    encoding="utf-8-sig",  # 使用 Excel 友好编码

)  # 保存结束


# ============================================================
# 28. 找到 No-Explanation Scenario
# ============================================================

noexp_df = scenario_df[  # 筛选 No-Explanation Case

    scenario_df["scenario_type"] == "no_review_explanation"  # 场景类型必须匹配

].copy()  # 创建副本


if len(noexp_df) != 1:  # V1 设计必须只有一个 No-Explanation Case

    raise ValueError(  # 否则停止

        f"预期 1 个 No-Explanation Case，实际 {len(noexp_df)} 个。"

    )  # 错误结束


noexp = noexp_df.iloc[0]  # 获取唯一 No-Explanation Case

noexp_product_id = str(noexp["product_id"])  # 获取商品 ID

noexp_month = noexp["month"]  # 获取当前异常月份


# ============================================================
# 29. 给完整真实 Review Dataset 创建 month
# ============================================================

raw_review_df["month"] = (  # 创建 Review 月份字段

    raw_review_df["created_at"]  # 使用真实 Review 时间

    .dt.tz_localize(None)  # 去掉时区

    .dt.to_period("M")  # 转换成月 Period

    .dt.to_timestamp()  # 转回 Timestamp

)  # Review Month 创建结束


# ============================================================
# 30. Rating Bucket 函数
# ============================================================

def build_rating_bucket(rating):  # 定义星级 Bucket

    if rating <= 2:  # 1～2 星

        return "negative"  # 归入 negative Bucket

    if rating == 3:  # 3 星

        return "neutral"  # 归入 neutral Bucket

    return "positive"  # 4～5 星归入 positive Bucket


raw_review_df["rating_bucket"] = (  # 为真实 Review 创建星级 Bucket

    raw_review_df["rating"]  # 使用星级

    .apply(build_rating_bucket)  # 调用 Bucket 函数

)  # Bucket 创建结束


# ============================================================
# 31. 获取 No-Explanation 当前月真实 Review
# ============================================================

current_noexp_reviews = raw_review_df[  # 筛选当前异常月份的所有真实 Review

    (

        raw_review_df["product_id"] == noexp_product_id  # 商品一致

    )

    &

    (

        raw_review_df["month"] == noexp_month  # 月份一致

    )

].copy()  # 创建副本


# ============================================================
# 32. 获取 No-Explanation 前三个月真实 Review
# ============================================================

noexp_baseline_start = (  # 计算 Baseline 开始月份

    noexp_month.to_period("M")  # 当前月转 Period

    - BASELINE_MONTHS  # 向前 3 个月

).to_timestamp()  # 转回 Timestamp


noexp_baseline_end = (  # 计算 Baseline 结束月份

    noexp_month.to_period("M")  # 当前月份

    - 1  # 上一个月份

).to_timestamp()  # 转回 Timestamp


baseline_noexp_reviews = raw_review_df[  # 筛选前三个月真实 Review

    (

        raw_review_df["product_id"] == noexp_product_id  # 商品一致

    )

    &

    (

        raw_review_df["month"] >= noexp_baseline_start  # 不早于 Baseline 起点

    )

    &

    (

        raw_review_df["month"] <= noexp_baseline_end  # 不晚于 Baseline 终点

    )

].copy()  # 创建副本


# ============================================================
# 33. 分层抽样函数
# ============================================================

def sample_reviews(source_df, total_target, audit_window):  # 定义 NoExp 语义审计抽样函数

    proportions = {  # 定义抽样时的星级覆盖比例

        "negative": 0.35,  # 35% 负向星级

        "neutral": 0.15,  # 15% 三星

        "positive": 0.50,  # 50% 正向星级

    }  # 抽样比例结束


    sampled_parts = []  # 创建抽样结果列表

    used_review_ids = set()  # 保存已经抽中的 Review ID


    for bucket_index, (bucket, proportion) in enumerate(  # 遍历三个 Rating Bucket

        proportions.items()  # 使用 Bucket 配置

    ):  # 循环开始


        target_count = round(  # 计算当前 Bucket 目标抽样数量

            total_target * proportion  # 总样本乘对应比例

        )  # 数量计算结束


        bucket_df = source_df[  # 筛选当前 Rating Bucket

            source_df["rating_bucket"] == bucket  # Bucket 必须匹配

        ].copy()  # 创建副本


        sample_count = min(  # 实际抽样数不能超过已有 Review

            target_count,  # 目标数量

            len(bucket_df),  # 当前 Bucket 实际数量

        )  # 实际数量结束


        if sample_count == 0:  # 如果当前 Bucket 没有样本

            continue  # 跳过该 Bucket


        sampled = bucket_df.sample(  # 随机抽取 Review

            n=sample_count,  # 抽样数量

            random_state=RANDOM_SEED + bucket_index,  # 固定随机种子

        ).copy()  # 抽样结束


        sampled_parts.append(sampled)  # 保存抽样结果

        used_review_ids.update(  # 记录已经抽中的 Review ID

            sampled["review_id"].tolist()  # 转成列表后加入集合

        )  # 更新结束


    if sampled_parts:  # 如果至少抽到了一个 Bucket

        sampled_df = pd.concat(  # 合并各 Bucket 样本

            sampled_parts,  # 使用抽样列表

            ignore_index=True,  # 重建索引

        )  # 合并结束

    else:  # 如果一个样本都没有

        sampled_df = source_df.head(0).copy()  # 创建同结构空 DataFrame


    missing_count = (  # 计算距离目标数量还差多少

        total_target  # 目标总量

        - len(sampled_df)  # 减去已经抽到的数量

    )  # 差额计算结束


    if missing_count > 0:  # 如果仍然不足目标数量

        remaining_df = source_df[  # 获取还没有抽中的 Review

            ~source_df["review_id"].isin(used_review_ids)  # 排除已抽 Review

        ].copy()  # 创建副本


        fill_count = min(  # 计算实际可补多少条

            missing_count,  # 还缺多少

            len(remaining_df),  # 实际剩余多少

        )  # 补样数量结束


        if fill_count > 0:  # 如果还有 Review 可以补

            fill_df = remaining_df.sample(  # 随机补充

                n=fill_count,  # 补充数量

                random_state=RANDOM_SEED + 100,  # 使用固定第二随机种子

            ).copy()  # 补充结束


            sampled_df = pd.concat(  # 合并原抽样与补充样本

                [

                    sampled_df,  # 原抽样

                    fill_df,  # 补充样本

                ],

                ignore_index=True,  # 重建索引

            )  # 合并结束


    sampled_df["audit_window"] = audit_window  # 标记 Baseline 或 Current


    return sampled_df  # 返回最终抽样结果


# ============================================================
# 34. 抽取 No-Explanation Current 40 条
# ============================================================

current_audit_df = sample_reviews(  # 对当前异常月进行分层抽样

    current_noexp_reviews,  # 当前月真实 Review

    NOEXP_CURRENT_SAMPLE,  # 目标 40 条

    "current",  # 标记为当前异常月

)  # 当前月抽样结束


# ============================================================
# 35. 抽取 No-Explanation Baseline 60 条
# ============================================================

baseline_audit_df = sample_reviews(  # 对前三个月 Review 分层抽样

    baseline_noexp_reviews,  # Baseline Review

    NOEXP_BASELINE_SAMPLE,  # 目标 60 条

    "baseline",  # 标记为 Baseline

)  # Baseline 抽样结束


# ============================================================
# 36. 合并 No-Explanation Audit Dataset
# ============================================================

noexp_audit_df = pd.concat(  # 将 Baseline 和 Current 合并

    [

        baseline_audit_df,  # 前三个月 60 条

        current_audit_df,  # 当前月 40 条

    ],

    ignore_index=True,  # 重建索引

)  # Audit Dataset 合并结束


# ============================================================
# 37. 检查 Audit 是否出现重复 Review
# ============================================================

audit_duplicate_count = int(  # 统计重复 Review ID

    noexp_audit_df["review_id"]  # 使用 review_id

    .duplicated()  # 判断重复

    .sum()  # 统计数量

)  # 统计结束


if audit_duplicate_count > 0:  # 如果出现重复

    raise ValueError(  # 停止程序

        f"No-Explanation Audit 中发现 {audit_duplicate_count} 条重复 Review。"

    )  # 错误结束


# ============================================================
# 38. 整理 No-Explanation Audit 字段
# ============================================================

audit_columns = [  # 定义人工语义审计需要保留的字段

    "review_id",  # Review ID

    "product_id",  # 商品 ID

    "created_at",  # 评论时间

    "month",  # 评论月份

    "rating",  # 星级

    "rating_bucket",  # 星级 Bucket

    "title",  # 评论标题

    "text",  # 评论正文

    "audit_window",  # Baseline / Current

]  # Audit 字段结束


noexp_audit_df = noexp_audit_df[  # 只保留这些审计字段

    audit_columns  # 使用字段列表

].copy()  # 创建副本


# ============================================================
# 39. 增加语义审计待填写字段
# ============================================================

noexp_audit_df["audit_primary_topic"] = ""  # 预留人工一级 Topic

noexp_audit_df["audit_secondary_topic"] = ""  # 预留人工二级 Topic

noexp_audit_df["audit_polarity"] = ""  # 预留人工 Polarity

noexp_audit_df["audit_evidence"] = ""  # 预留人工 Evidence Span

noexp_audit_df["meaningful_change"] = ""  # 预留是否发现明显语义变化

noexp_audit_df["audit_notes"] = ""  # 预留人工备注


# ============================================================
# 40. 保存 No-Explanation Semantic Audit
# ============================================================

noexp_audit_df.to_csv(  # 保存 100 条语义审计数据

    NOEXP_AUDIT_OUTPUT,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # Excel 友好编码

)  # 保存结束


# ============================================================
# 41. 创建精简 Case Summary
# ============================================================

summary_columns = [  # 定义 Case Summary 关键字段

    "case_id",  # 唯一 Case ID

    "scenario_id",  # Scenario ID

    "scenario_type",  # Scenario 类型

    "product_name",  # 商品名称

    "current_month",  # 当前月份

    "expected_review_relation",  # 评论预期关系

    "case_status",  # 当前状态

    "order_change_pct",  # 日均订单变化

    "refund_rate_delta",  # 退款率变化

    "avg_rating_delta",  # 平均评分变化

    "negative_ratio_delta",  # 负面星级比例变化

    "positive_ratio_delta",  # 正面星级比例变化

    "golden_evidence_claim_count",  # 当前月 Golden Claim 数

]  # Summary 字段结束


existing_summary_columns = [  # 兼容旧版 Review Metric 缺少部分字段

    column  # 返回字段

    for column in summary_columns  # 遍历希望展示的字段

    if column in case_df.columns  # 只保留真实存在的字段

]  # 字段筛选结束


case_summary_df = case_df[  # 创建精简 Case Summary

    existing_summary_columns  # 使用实际存在字段

].copy()  # 创建副本


case_summary_df.to_csv(  # 保存 Case Summary

    CASE_SUMMARY_OUTPUT,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # Excel 友好编码

)  # 保存结束


# ============================================================
# 42. 构建 09 阶段质量检查
# ============================================================

validation_rows = []  # 创建 Validation 结果列表


validation_rows.append({  # 检查 1：Case 数量

    "check_name": "case_month_count",  # 检查名称

    "expected": len(scenario_df),  # 期望 Scenario Month 数

    "actual": len(case_df),  # 实际 Case 数

    "passed": len(case_df) == len(scenario_df),  # 是否通过

})  # 检查 1 结束


validation_rows.append({  # 检查 2：Case ID 是否唯一

    "check_name": "unique_case_id",  # 检查名称

    "expected": len(case_df),  # 期望唯一数量

    "actual": case_df["case_id"].nunique(),  # 实际唯一数量

    "passed": case_df["case_id"].nunique() == len(case_df),  # 是否通过

})  # 检查 2 结束


validation_rows.append({  # 检查 3：经营指标来源必须都是 synthetic

    "check_name": "business_source_is_synthetic",  # 检查名称

    "expected": 0,  # 期望错误数量为 0

    "actual": int(  # 统计错误来源数量

        (

            case_df["business_source"] != "synthetic"  # 找非 synthetic

        ).sum()

    ),  # 数量结束

    "passed": bool(  # 判断是否通过

        (

            case_df["business_source"] != "synthetic"  # 非 synthetic

        ).sum()

        == 0  # 必须没有

    ),  # 判断结束

})  # 检查 3 结束


validation_rows.append({  # 检查 4：禁止因果结论

    "check_name": "causal_claim_disabled",  # 检查名称

    "expected": 0,  # 期望允许因果的 Case 数为 0

    "actual": int(  # 统计允许因果的 Case

        case_df["causal_claim_allowed"]  # 获取 Boolean

        .astype(bool)  # 转成 Boolean

        .sum()  # 统计 True 数量

    ),  # 实际数量结束

    "passed": bool(  # 判断是否通过

        case_df["causal_claim_allowed"]  # 获取字段

        .astype(bool)  # 转 Boolean

        .sum()  # 统计

        == 0  # 必须为 0

    ),  # 判断结束

})  # 检查 4 结束


validation_rows.append({  # 检查 5：Golden Review 时间全部关联成功

    "check_name": "golden_review_created_at_missing",  # 检查名称

    "expected": 0,  # 期望缺失为 0

    "actual": missing_created_at_count,  # 实际缺失数量

    "passed": missing_created_at_count == 0,  # 是否通过

})  # 检查 5 结束


validation_rows.append({  # 检查 6：Golden Claim Month 全部关联成功

    "check_name": "golden_claim_month_missing",  # 检查名称

    "expected": 0,  # 期望缺失为 0

    "actual": missing_claim_month_count,  # 实际缺失

    "passed": missing_claim_month_count == 0,  # 是否通过

})  # 检查 6 结束


validation_rows.append({  # 检查 7：NoExp Audit 当前月数量

    "check_name": "noexp_current_audit_count",  # 检查名称

    "expected": NOEXP_CURRENT_SAMPLE,  # 期望 40

    "actual": len(current_audit_df),  # 实际 Current 样本数

    "passed": len(current_audit_df) == NOEXP_CURRENT_SAMPLE,  # 是否通过

})  # 检查 7 结束


validation_rows.append({  # 检查 8：NoExp Audit Baseline 数量

    "check_name": "noexp_baseline_audit_count",  # 检查名称

    "expected": NOEXP_BASELINE_SAMPLE,  # 期望 60

    "actual": len(baseline_audit_df),  # 实际 Baseline 数量

    "passed": len(baseline_audit_df) == NOEXP_BASELINE_SAMPLE,  # 是否通过

})  # 检查 8 结束


validation_rows.append({  # 检查 9：NoExp Audit 总数量

    "check_name": "noexp_total_audit_count",  # 检查名称

    "expected": (  # 期望总数

        NOEXP_CURRENT_SAMPLE  # Current 40

        + NOEXP_BASELINE_SAMPLE  # Baseline 60

    ),  # 期望总数结束

    "actual": len(noexp_audit_df),  # 实际总数

    "passed": len(noexp_audit_df)  # 实际数量

    == (

        NOEXP_CURRENT_SAMPLE  # Current

        + NOEXP_BASELINE_SAMPLE  # Baseline

    ),  # 判断结束

})  # 检查 9 结束


validation_rows.append({  # 检查 10：Audit 是否有重复

    "check_name": "noexp_duplicate_review_id",  # 检查名称

    "expected": 0,  # 期望重复数量为 0

    "actual": audit_duplicate_count,  # 实际重复数

    "passed": audit_duplicate_count == 0,  # 是否通过

})  # 检查 10 结束


validation_df = pd.DataFrame(  # 创建 Validation DataFrame

    validation_rows  # 使用所有校验项

)  # Validation 创建结束


validation_df.to_csv(  # 保存 Validation

    VALIDATION_OUTPUT,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # Excel 友好编码

)  # 保存结束


# ============================================================
# 43. 如果任意关键 Validation 失败，给出警告
# ============================================================

failed_validation_df = validation_df[  # 找出失败项目

    ~validation_df["passed"]  # passed 为 False

].copy()  # 创建副本


# ============================================================
# 44. 控制台输出
# ============================================================

print(  # 输出完成标题

    "\n================ 09 Diagnosis Case Candidate 构建完成 ================\n"

)  # 标题结束


print(f"Diagnosis Case Month 数量：{len(case_df)}")  # 输出 Case 数量

print(f"Golden Review 数量：{len(golden_review_df)}")  # 输出 Golden Review 数量

print(f"Golden Topic Claim 数量：{len(golden_claim_df)}")  # 输出 Golden Claim 数量


print("\nCase Summary：")  # 输出 Case Summary 标题

print(  # 打印精简 Summary

    case_summary_df.to_string(

        index=False  # 不显示 pandas 行号

    )

)  # Summary 打印结束


print("\nNo-Explanation Semantic Audit：")  # 输出 NoExp Audit 标题

print(  # 输出具体 Case

    f"{noexp['product_name']} | "

    f"Current={noexp_month:%Y-%m} | "

    f"Baseline={noexp_baseline_start:%Y-%m} ~ {noexp_baseline_end:%Y-%m}"

)  # Case 信息结束


print(  # 输出审计样本数

    f"Audit 样本："

    f"Baseline {len(baseline_audit_df)} + "

    f"Current {len(current_audit_df)} = "

    f"{len(noexp_audit_df)}"

)  # 样本数结束


print("\n质量校验：")  # 输出 Validation 标题

print(  # 打印全部 Validation

    validation_df.to_string(

        index=False  # 不显示 pandas 索引

    )

)  # Validation 打印结束


if failed_validation_df.empty:  # 如果所有校验通过

    print("\n全部 Validation 通过。")  # 输出成功提示

else:  # 如果存在失败项

    print("\n警告：以下 Validation 未通过：")  # 输出警告

    print(  # 打印失败项目

        failed_validation_df.to_string(

            index=False  # 不显示行号

        )

    )  # 打印结束


print("\n输出文件：")  # 输出文件标题

print(f"Case Candidate：{CASE_OUTPUT}")  # 输出完整 Case 文件

print(f"NoExp Audit：{NOEXP_AUDIT_OUTPUT}")  # 输出 NoExp Audit 文件

print(f"Case Summary：{CASE_SUMMARY_OUTPUT}")  # 输出 Summary 文件

print(f"Validation：{VALIDATION_OUTPUT}")  # 输出 Validation 文件


print(  # 输出阶段提醒

    "\n注意：当前仍然是 Diagnosis Case Candidate。"

)  # 提醒结束


print(  # 输出下一步提醒

    "SYN-NOEXP-001 的 100 条真实评论完成语义审计后，"

    "才决定它能不能正式进入 Golden Diagnosis Set。"

)  # 下一步提醒结束
