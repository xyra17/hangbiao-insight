from pathlib import Path  # 导入 Path，用于处理文件和目录路径

import numpy as np  # 导入 numpy，用于随机数和数值计算

import pandas as pd  # 导入 pandas，用于数据读取、生成和聚合


# ============================================================
# 1. 基础目录
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前脚本所在目录

REVIEW_FILE = BASE_DIR / "data" / "01_标准化评论数据" / "评论数据集_v1.parquet"  # 读取真实 Review Dataset

STABLE_WINDOW_FILE = (  # 定义 04 阶段的全部稳定窗口文件

    BASE_DIR

    / "outputs"

    / "03_稳定时间窗"

    / "01_全部稳定窗口.csv"

)  # 稳定窗口文件路径结束

DATA_DIR = BASE_DIR / "data" / "02_构造经营指标"  # 定义构造经营指标输出目录

OUTPUT_DIR = BASE_DIR / "outputs" / "08_经营指标"  # 定义经营指标分析输出目录

DATA_DIR.mkdir(parents=True, exist_ok=True)  # 如果 data 目录不存在则创建

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 如果输出目录不存在则创建


# ============================================================
# 2. 数据版本
# ============================================================

METRIC_VERSION = "business-metric-v1.0"  # 定义经营指标版本

RANDOM_SEED = 20260911  # 固定总随机种子，保证以后可以完全复现


# ============================================================
# 3. 核心商品
# ============================================================

CORE_PRODUCTS = {  # 定义已经冻结进入 P0 的三个核心商品

    "B07P9S6R9Y": "Yootech_Wireless_Charger",  # 无线充电器

    "B0BYJ8ZVWH": "AILKIN_USB_Wall_Charger",  # USB 墙充

    "B07H2V5YLH": "Ailun_Screen_Protector",  # 钢化膜

}  # 核心商品定义结束


# ============================================================
# 4. 每个商品的基础经营参数
# ============================================================

PRODUCT_CONFIG = {  # 定义三个商品不同的基础经营水平

    "B07P9S6R9Y": {  # Yootech 无线充配置

        "base_daily_orders": 180,  # 基础日订单量约 180

        "base_refund_rate": 0.045,  # 基础退款率约 4.5%

        "annual_amplitude": 0.12,  # 年度季节性振幅

        "trend_per_year": 0.035,  # 每年约 3.5% 的基础趋势

        "seed_offset": 101,  # 商品独立随机种子偏移

    },  # Yootech 配置结束

    "B0BYJ8ZVWH": {  # AILKIN 墙充配置

        "base_daily_orders": 95,  # 基础日订单量

        "base_refund_rate": 0.038,  # 基础退款率约 3.8%

        "annual_amplitude": 0.10,  # 年度季节性振幅

        "trend_per_year": 0.025,  # 每年约 2.5% 趋势

        "seed_offset": 202,  # 独立随机种子

    },  # AILKIN 配置结束

    "B07H2V5YLH": {  # Ailun 钢化膜配置

        "base_daily_orders": 140,  # 基础日订单量

        "base_refund_rate": 0.042,  # 基础退款率约 4.2%

        "annual_amplitude": 0.14,  # 年度季节性振幅

        "trend_per_year": 0.030,  # 每年约 3% 趋势

        "seed_offset": 303,  # 独立随机种子

    },  # Ailun 配置结束

}  # 商品参数定义结束


# ============================================================
# 5. 已经确定的业务场景
# ============================================================

SCENARIO_ROWS = [  # 定义后续 Diagnosis Golden Case 的合成场景

    {  # 场景 1 开始

        "scenario_id": "SYN-EXPLAIN-001",  # 场景唯一 ID

        "scenario_type": "explainable_negative",  # 评论侧存在明显负向信号

        "product_id": "B07P9S6R9Y",  # Yootech 无线充

        "month": "2020-06-01",  # 对应真实负向 Review Shift 月份

        "order_multiplier": 0.78,  # 合成订单下降约 22%

        "refund_multiplier": 1.80,  # 合成退款概率明显升高

        "expected_review_relation": "supporting_evidence",  # 评论可以作为支持证据

        "design_note": (

            "经营异常与真实负向评论变化对齐；"

            "后续诊断只能说评论提供相关支持证据，不能宣称因果。"

        ),  # 场景设计说明

    },  # 场景 1 结束

    {  # 场景 2 开始

        "scenario_id": "SYN-MIXED-001",  # 混合信号场景

        "scenario_type": "mixed_conflict",  # 经营指标本身存在冲突

        "product_id": "B07P9S6R9Y",  # Yootech 无线充

        "month": "2022-02-01",  # 对应真实负向 Review Shift

        "order_multiplier": 1.35,  # 订单反而增加

        "refund_multiplier": 1.70,  # 同时退款概率恶化

        "expected_review_relation": "partial_support",  # 评论只解释部分现象

        "design_note": (

            "订单增长和退款恶化同时发生；"

            "评论可以解释质量侧风险，但不能解释订单为何增长。"

        ),  # 场景说明

    },  # 场景 2 结束

    {  # 场景 3 开始

        "scenario_id": "SYN-POSREC-001",  # 正向恢复场景

        "scenario_type": "positive_recovery",  # 正向经营变化

        "product_id": "B0BYJ8ZVWH",  # AILKIN 墙充

        "month": "2022-07-01",  # 真实正向 Review Shift

        "order_multiplier": 1.28,  # 合成订单增加

        "refund_multiplier": 0.72,  # 合成退款概率下降

        "expected_review_relation": "supporting_evidence",  # 正向评论可以提供支持

        "design_note": (

            "正向经营变化与真实正向评论变化对齐；"

            "用于验证系统不能只分析负面异常。"

        ),  # 场景说明

    },  # 场景 3 结束

    {  # 场景 4 开始

        "scenario_id": "SYN-REVERSAL-001",  # 正负反转场景

        "scenario_type": "negative_reversal",  # 前一月正向、后一月转负向

        "product_id": "B0BYJ8ZVWH",  # AILKIN 墙充

        "month": "2022-08-01",  # 真实负向 Review Shift

        "order_multiplier": 0.80,  # 订单明显下降

        "refund_multiplier": 1.75,  # 退款概率明显上升

        "expected_review_relation": "supporting_evidence",  # 评论存在负向支持信号

        "design_note": (

            "紧接 2022-07 正向月份之后出现经营反转；"

            "适合验证 Agent 是否能比较相邻月份并重新规划调查路径。"

        ),  # 场景说明

    },  # 场景 4 结束

    {  # 场景 5 开始

        "scenario_id": "SYN-POSREC-002",  # 钢化膜正向恢复场景

        "scenario_type": "positive_recovery",  # 正向恢复

        "product_id": "B07H2V5YLH",  # Ailun 钢化膜

        "month": "2022-11-01",  # 第一恢复月份

        "order_multiplier": 1.22,  # 订单提升

        "refund_multiplier": 0.80,  # 退款概率下降

        "expected_review_relation": "supporting_evidence",  # 评论存在正向支持信号

        "design_note": (

            "第一阶段正向恢复，"

            "用于观察多月持续改善。"

        ),  # 场景说明

    },  # 场景 5 结束

    {  # 场景 6 开始

        "scenario_id": "SYN-POSREC-002",  # 与上一月属于同一场景

        "scenario_type": "positive_recovery",  # 正向恢复

        "product_id": "B07H2V5YLH",  # Ailun 钢化膜

        "month": "2022-12-01",  # 第二恢复月份

        "order_multiplier": 1.28,  # 改善继续扩大

        "refund_multiplier": 0.72,  # 退款率继续改善

        "expected_review_relation": "supporting_evidence",  # 评论存在支持

        "design_note": (

            "第二阶段正向恢复，"

            "用于验证系统是否识别持续趋势而不是单月波动。"

        ),  # 场景说明

    },  # 场景 6 结束

]  # 固定场景定义结束


# ============================================================
# 6. 读取 Review Dataset
# ============================================================

print("正在读取 Review Dataset V1……")  # 输出当前步骤

reviews = pd.read_parquet(REVIEW_FILE)  # 读取完整评论数据

print(f"Review 数量：{len(reviews):,}")  # 输出 Review 总量


# ============================================================
# 7. 标准化 Review 字段
# ============================================================

reviews["created_at"] = pd.to_datetime(  # 标准化评论时间

    reviews["created_at"],  # 使用 created_at 字段

    utc=True,  # 按 UTC 时间处理

    errors="coerce",  # 无法解析的值转为空

)  # 时间处理结束

reviews["product_id"] = reviews["sku"].astype(str)  # 当前 sku 实际保存 parent_asin，因此转成内部 product_id

reviews = reviews[  # 只保留三个核心商品

    reviews["product_id"].isin(CORE_PRODUCTS.keys())  # 判断 product_id 是否属于核心商品

].copy()  # 创建副本


# ============================================================
# 8. 获取每个商品的真实活跃时间范围
# ============================================================

product_date_range = (  # 开始统计每个商品的真实评论时间范围

    reviews  # 使用 Review Dataset

    .groupby("product_id")["created_at"]  # 按 product_id 分组

    .agg(["min", "max"])  # 获取最早和最晚评论时间

    .reset_index()  # 转回普通 DataFrame

)  # 时间范围统计结束

product_date_range["min"] = (  # 处理最早日期

    product_date_range["min"]  # 获取 min

    .dt.tz_localize(None)  # 去掉时区

    .dt.normalize()  # 归一化到当天 00:00

)  # 最早日期处理结束

product_date_range["max"] = (  # 处理最晚日期

    product_date_range["max"]  # 获取 max

    .dt.tz_localize(None)  # 去掉时区

    .dt.normalize()  # 归一化到当天

)  # 最晚日期处理结束


# ============================================================
# 9. 读取稳定窗口，用于自动选择 No-Explanation Case
# ============================================================

print("正在寻找 Review Stable Month……")  # 输出步骤提示

stable_windows = pd.read_csv(STABLE_WINDOW_FILE)  # 读取全部稳定窗口


# ============================================================
# 10. 兼容 sku / product_id 两种旧字段命名
# ============================================================

if "product_id" in stable_windows.columns:  # 如果 04 已经使用 product_id

    stable_product_column = "product_id"  # 使用 product_id

else:  # 否则

    stable_product_column = "sku"  # 使用旧版 sku 字段


# ============================================================
# 11. 标准化 Stable Window 时间
# ============================================================

stable_windows["current_month"] = pd.to_datetime(  # 转换当前月份

    stable_windows["current_month"],  # 使用 current_month

    errors="coerce",  # 异常转为空

)  # 时间转换结束


# ============================================================
# 12. 建立需要排除的重点月份
# ============================================================

excluded_periods = set()  # 创建排除月份集合


for scenario in SCENARIO_ROWS:  # 遍历已经确定的场景

    product_id = scenario["product_id"]  # 获取商品 ID

    month_period = pd.Period(  # 把场景月份转成 Period

        scenario["month"],  # 场景月份

        freq="M",  # 使用月粒度

    )  # Period 创建结束


    for month_offset in [-1, 0, 1]:  # 同时排除前一月、当月、后一月

        excluded_periods.add(  # 加入排除集合

            (

                product_id,  # 商品 ID

                month_period + month_offset,  # 相邻月份

            )

        )  # 排除月份加入结束


# ============================================================
# 13. 只保留真正 stable 的 Review Window
# ============================================================

stable_candidates = stable_windows[  # 筛选稳定月份

    (

        stable_windows["signal_type"] == "stable"  # 04 阶段判定为 stable

    )

    &

    (

        stable_windows[stable_product_column].isin(CORE_PRODUCTS.keys())  # 必须属于三个核心商品

    )

].copy()  # 创建副本


# ============================================================
# 14. 尝试找到 Review 数量字段
# ============================================================

review_count_column = None  # 默认还没有找到 Review 数量字段


for candidate_column in [  # 定义可能的字段名

    "current_review_count",  # 04 脚本标准字段

    "review_count",  # 可能的简化字段

    "current_count",  # 兼容其他命名

]:  # 字段候选结束

    if candidate_column in stable_candidates.columns:  # 如果字段存在

        review_count_column = candidate_column  # 保存字段名

        break  # 找到后结束循环


# ============================================================
# 15. 如果存在 Review Count，则过滤极低样本月份
# ============================================================

if review_count_column is not None:  # 如果找到了评论数

    stable_candidates = stable_candidates[  # 开始过滤

        stable_candidates[review_count_column] >= 80  # 至少 80 条评论

    ].copy()  # 创建副本


# ============================================================
# 16. 计算 Review-Side Stability Proxy
# ============================================================

stable_candidates["review_stability_score"] = 0.0  # 初始化稳定性分数


for delta_column in [  # 使用评论侧几个变化指标

    "rating_delta",  # 平均评分变化

    "negative_ratio_delta",  # 负向比例变化

    "positive_ratio_delta",  # 正向比例变化

]:  # 变化指标列表结束

    if delta_column in stable_candidates.columns:  # 如果该指标存在

        stable_candidates["review_stability_score"] += (  # 累加绝对变化

            pd.to_numeric(  # 转成数值

                stable_candidates[delta_column],  # 当前变化字段

                errors="coerce",  # 异常转为空

            )

            .fillna(0.0)  # 空值填 0

            .abs()  # 取绝对值

        )  # 累加结束


# ============================================================
# 17. 排除重点场景附近月份
# ============================================================

stable_candidates["month_period"] = (  # 创建月 Period

    stable_candidates["current_month"]  # 当前月份

    .dt.to_period("M")  # 转成月粒度 Period

)  # Period 创建结束

stable_candidates["excluded"] = stable_candidates.apply(  # 判断是否需要排除

    lambda row: (

        (

            str(row[stable_product_column]),  # 商品 ID

            row["month_period"],  # 当前月份

        )

        in excluded_periods  # 是否存在于排除集合

    ),

    axis=1,  # 按行执行

)  # 排除判断结束

stable_candidates = stable_candidates[  # 保留不在排除区间的月份

    ~stable_candidates["excluded"]  # excluded 必须为 False

].copy()  # 创建副本


# ============================================================
# 18. 对 Stable Candidate 排序
# ============================================================

sort_columns = ["review_stability_score"]  # 首先按照 Review 稳定程度排序

sort_ascending = [True]  # 越低越稳定


if review_count_column is not None:  # 如果存在评论数

    sort_columns.append(review_count_column)  # 同时加入评论数量

    sort_ascending.append(False)  # 评论越多越优先


stable_candidates = stable_candidates.sort_values(  # 执行排序

    sort_columns,  # 排序字段

    ascending=sort_ascending,  # 排序方向

).reset_index(drop=True)  # 重建索引


# ============================================================
# 19. 选择一个 No-Explanation Month
# ============================================================

if stable_candidates.empty:  # 如果没有任何稳定月份

    raise ValueError(  # 停止脚本

        "没有找到合适的 Review Stable Month，无法构造 No-Explanation Case。"

    )  # 错误结束


no_explanation_row = stable_candidates.iloc[0]  # 选择最稳定的一行

# no_explanation_product_id = str(  # 获取商品 ID
#
#     no_explanation_row[stable_product_column]
#
# )  # 商品 ID 获取结束
#
# no_explanation_month = (  # 获取月份字符串
#
#     no_explanation_row["current_month"]
#
#     .strftime("%Y-%m-01")
#
# )  # 月份格式化结束

no_explanation_product_id = "B0BYJ8ZVWH"  # 09C 语义审计最终选中的 AILKIN

no_explanation_month = "2019-06-01"  # 09C 最终选中的语义稳定月份

# ============================================================
# 20. 将 No-Explanation Case 加入场景表
# ============================================================

SCENARIO_ROWS.append({  # 新增一个完全由经营侧注入的异常

    "scenario_id": "SYN-NOEXP-001",  # 场景 ID

    "scenario_type": "no_review_explanation",  # 评论无法解释的业务异常

    "product_id": no_explanation_product_id,  # 自动选择的稳定商品

    "month": no_explanation_month,  # 自动选择的稳定月份

    "order_multiplier": 0.68,  # 人为制造明显订单下降

    "refund_multiplier": 1.55,  # 同时制造退款恶化

    "expected_review_relation": "no_explanation",  # 预期评论无法解释

    "design_note": (

        "该月份由 04 阶段 Review Stable Window 自动选择；"

        "经营指标异常为 synthetic 注入，"

        "用于验证系统是否能够明确回答“评论无法解释该异常”。"

    ),  # 场景说明

})  # No-Explanation 场景加入结束


# ============================================================
# 21. 将场景表转换为 DataFrame
# ============================================================

scenario_df = pd.DataFrame(SCENARIO_ROWS)  # 转换为 DataFrame

scenario_df["month"] = pd.to_datetime(  # 标准化场景月份

    scenario_df["month"]  # 使用 month

)  # 转换结束

scenario_df["product_name"] = scenario_df["product_id"].map(  # 增加商品名称

    CORE_PRODUCTS

)  # 商品名称映射结束

scenario_df["source"] = "synthetic"  # 明确标记经营场景为 synthetic

scenario_df["version"] = METRIC_VERSION  # 保存生成版本


# ============================================================
# 22. 建立 Product + Month 到 Scenario 的映射
# ============================================================

scenario_lookup = {}  # 创建场景查询字典


for _, scenario in scenario_df.iterrows():  # 遍历全部场景

    lookup_key = (  # 创建 Product + Month 唯一键

        scenario["product_id"],  # 商品 ID

        scenario["month"].to_period("M"),  # 场景月份

    )  # 唯一键结束


    if lookup_key in scenario_lookup:  # 如果同一商品同月已经有场景

        raise ValueError(  # 防止场景冲突

            f"发现重复 Product-Month 场景：{lookup_key}"

        )  # 错误结束


    scenario_lookup[lookup_key] = scenario.to_dict()  # 保存场景信息


# ============================================================
# 23. 定义星期季节性
# ============================================================

WEEKDAY_FACTOR = {  # 给不同星期设置轻微订单差异

    0: 0.96,  # 星期一

    1: 0.98,  # 星期二

    2: 1.00,  # 星期三

    3: 1.02,  # 星期四

    4: 1.06,  # 星期五

    5: 1.10,  # 星期六

    6: 1.04,  # 星期日

}  # 星期系数结束


# ============================================================
# 24. 准备存储 Daily Metric
# ============================================================

daily_rows = []  # 创建每日经营指标列表


# ============================================================
# 25. 逐商品生成 Daily Synthetic Metric
# ============================================================

for _, date_row in product_date_range.iterrows():  # 遍历三个商品

    product_id = date_row["product_id"]  # 获取商品 ID

    product_name = CORE_PRODUCTS[product_id]  # 获取商品名称

    config = PRODUCT_CONFIG[product_id]  # 获取商品生成参数

    start_date = date_row["min"]  # 获取真实数据最早日期

    end_date = date_row["max"]  # 获取真实数据最晚日期


    rng = np.random.default_rng(  # 为当前商品创建独立随机生成器

        RANDOM_SEED + config["seed_offset"]  # 固定商品级随机种子

    )  # 随机生成器创建结束


    date_range = pd.date_range(  # 创建连续日历

        start=start_date,  # 起始日期

        end=end_date,  # 结束日期

        freq="D",  # 每日一条

    )  # 日历生成结束


    total_days = len(date_range)  # 记录总天数


    for day_index, current_date in enumerate(date_range):  # 遍历每日日期

        years_from_start = day_index / 365.25  # 计算距起始日的大致年份


        trend_factor = (  # 计算长期经营趋势

            1.0

            + config["trend_per_year"] * years_from_start

        )  # 趋势系数结束


        annual_position = (  # 计算当前日期在一年中的位置

            current_date.dayofyear / 365.25

        )  # 年度位置结束


        annual_factor = (  # 创建平滑年度季节性

            1.0

            + config["annual_amplitude"]

            * np.sin(

                2.0 * np.pi * annual_position

            )

        )  # 年度季节性计算结束


        weekday_factor = WEEKDAY_FACTOR[  # 获取星期系数

            current_date.weekday()

        ]  # 星期系数获取结束


        base_order_lambda = (  # 计算当天基础订单期望

            config["base_daily_orders"]

            * trend_factor

            * annual_factor

            * weekday_factor

        )  # 基础订单期望计算结束


        base_refund_probability = (  # 计算当天基础退款概率

            config["base_refund_rate"]

            + 0.004

            * np.sin(

                2.0

                * np.pi

                * annual_position

                + 0.8

            )

        )  # 基础退款概率结束


        refund_noise = rng.normal(  # 生成每天的小幅退款率随机波动

            loc=0.0,  # 波动均值为 0

            scale=0.004,  # 标准差 0.4 个百分点

        )  # 随机噪声结束


        base_refund_probability += refund_noise  # 加入随机波动


        current_period = current_date.to_period("M")  # 获取当前月份 Period


        scenario = scenario_lookup.get(  # 查询当前日是否属于注入场景

            (

                product_id,  # 当前商品

                current_period,  # 当前月份

            )

        )  # 场景查询结束


        if scenario is not None:  # 如果当前月份存在场景

            order_multiplier = scenario["order_multiplier"]  # 获取订单倍率

            refund_multiplier = scenario["refund_multiplier"]  # 获取退款倍率

            scenario_id = scenario["scenario_id"]  # 获取场景 ID

            scenario_type = scenario["scenario_type"]  # 获取场景类型

            is_injected = True  # 标记为注入区间


        else:  # 如果是普通月份

            order_multiplier = 1.0  # 订单不调整

            refund_multiplier = 1.0  # 退款概率不调整

            scenario_id = ""  # 没有场景 ID

            scenario_type = "baseline"  # 标记普通基线

            is_injected = False  # 不是注入数据


        final_order_lambda = (  # 计算场景调整后的订单期望

            base_order_lambda

            * order_multiplier

        )  # 最终订单期望结束


        final_refund_probability = (  # 计算场景调整后的退款概率

            base_refund_probability

            * refund_multiplier

        )  # 最终退款概率结束


        final_refund_probability = np.clip(  # 限制退款概率到合理范围

            final_refund_probability,  # 当前退款概率

            0.005,  # 最低 0.5%

            0.30,  # 最高 30%

        )  # Clip 结束


        order_count = int(  # 从 Poisson 分布生成当天订单数

            rng.poisson(

                lam=max(

                    final_order_lambda,

                    1.0,

                )

            )

        )  # 订单数生成结束


        refund_count = int(  # 根据真实订单数生成退款数

            rng.binomial(

                n=order_count,  # 当天订单量

                p=final_refund_probability,  # 当天退款概率

            )

        )  # 退款数量生成结束


        if order_count > 0:  # 如果当天有订单

            refund_rate = refund_count / order_count  # 计算真实退款率

        else:  # 极端情况下如果订单为 0

            refund_rate = 0.0  # 退款率设为 0


        daily_rows.append({  # 保存当天数据

            "product_id": product_id,  # 商品 ID

            "product_name": product_name,  # 商品名称

            "date": current_date,  # 日期

            "order_count": order_count,  # 合成订单数

            "refund_count": refund_count,  # 合成退款数

            "refund_rate": refund_rate,  # 合成退款率

            "source": "synthetic",  # 明确来源为 synthetic

            "version": METRIC_VERSION,  # 数据版本

            "scenario_id": scenario_id,  # 场景 ID

            "scenario_type": scenario_type,  # 场景类型

            "is_injected": is_injected,  # 是否属于人工注入区间

        })  # 当天指标保存结束


# ============================================================
# 26. 创建 Daily Business Metric DataFrame
# ============================================================

daily_df = pd.DataFrame(daily_rows)  # 转换为 DataFrame


# ============================================================
# 27. 保存 Daily Wide Format
# ============================================================

daily_csv_output = DATA_DIR / "经营指标日宽表_v1.csv"  # 定义 CSV 输出

daily_parquet_output = DATA_DIR / "经营指标日宽表_v1.parquet"  # 定义 Parquet 输出

daily_df.to_csv(  # 保存 CSV

    daily_csv_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # Excel 友好编码

)  # CSV 保存结束

daily_df.to_parquet(  # 保存 Parquet

    daily_parquet_output,  # 输出路径

    index=False,  # 不保存索引

)  # Parquet 保存结束


# ============================================================
# 28. 转换成通用 Long Format Metric Schema
# ============================================================

long_df = daily_df.melt(  # 将宽表转换成长表

    id_vars=[  # 定义不展开的字段

        "product_id",  # 商品 ID

        "product_name",  # 商品名称

        "date",  # 日期

        "source",  # 数据来源

        "version",  # 数据版本

        "scenario_id",  # 场景 ID

        "scenario_type",  # 场景类型

        "is_injected",  # 是否注入

    ],

    value_vars=[  # 需要转成长表的指标

        "order_count",  # 订单数量

        "refund_count",  # 退款数量

        "refund_rate",  # 退款率

    ],

    var_name="metric_name",  # 指标名称字段

    value_name="metric_value",  # 指标数值字段

)  # Long Format 转换结束


# ============================================================
# 29. 保存 Long Format
# ============================================================

long_csv_output = DATA_DIR / "经营指标日长表_v1.csv"  # 定义 Long CSV

long_parquet_output = DATA_DIR / "经营指标日长表_v1.parquet"  # 定义 Long Parquet

long_df.to_csv(  # 保存 Long CSV

    long_csv_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # UTF-8 BOM

)  # 保存结束

long_df.to_parquet(  # 保存 Long Parquet

    long_parquet_output,  # 输出路径

    index=False,  # 不保存索引

)  # 保存结束


# ============================================================
# 30. 创建 Monthly Business Metric
# ============================================================

daily_df["month"] = (  # 增加月份字段

    daily_df["date"]  # 使用日期

    .dt.to_period("M")  # 转成月份 Period

    .dt.to_timestamp()  # 再转成 Timestamp

)  # 月份生成结束


monthly_df = (  # 开始按月聚合

    daily_df

    .groupby(  # 按商品和月份

        [

            "product_id",  # 商品 ID

            "product_name",  # 商品名称

            "month",  # 月份

        ],

        as_index=False,  # 保持普通列

    )

    .agg(  # 聚合订单和退款数量

        order_count=(

            "order_count",

            "sum",

        ),

        refund_count=(

            "refund_count",

            "sum",

        ),

    )

)  # 月度聚合完成


# ============================================================
# 31. 使用订单量重新计算 Monthly Refund Rate
# ============================================================

monthly_df["refund_rate"] = (  # 计算月退款率

    monthly_df["refund_count"]

    / monthly_df["order_count"]

)  # 月退款率计算结束

monthly_df["source"] = "synthetic"  # 标记来源

monthly_df["version"] = METRIC_VERSION  # 标记版本


# ============================================================
# 32. 将 Scenario 信息合并到 Monthly Metric
# ============================================================

scenario_merge_df = scenario_df[  # 只取需要合并的场景字段

    [

        "product_id",  # 商品 ID

        "month",  # 月份

        "scenario_id",  # 场景 ID

        "scenario_type",  # 场景类型

        "expected_review_relation",  # 评论预期关系

    ]

].copy()  # 创建副本


monthly_df = monthly_df.merge(  # 合并场景信息

    scenario_merge_df,  # 场景表

    on=[

        "product_id",

        "month",

    ],  # 使用商品和月份作为 Key

    how="left",  # 普通月份也要保留

)  # 合并结束

monthly_df["scenario_id"] = monthly_df["scenario_id"].fillna("")  # 普通月份场景 ID 设为空

monthly_df["scenario_type"] = monthly_df["scenario_type"].fillna("baseline")  # 普通月份标记 baseline

monthly_df["expected_review_relation"] = (  # 普通月份关系设为空

    monthly_df["expected_review_relation"]

    .fillna("")

)  # 填充结束

monthly_df["is_injected"] = (  # 标记是否属于场景月份

    monthly_df["scenario_type"] != "baseline"

)  # 注入标记结束


# ============================================================
# 33. 保存 Monthly Business Metric
# ============================================================

monthly_output = OUTPUT_DIR / "01_经营指标月表_v1.csv"  # 定义月指标文件

monthly_df.to_csv(  # 保存月指标

    monthly_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # UTF-8 BOM

)  # 保存完成


# ============================================================
# 34. 保存 Scenario Injection Plan
# ============================================================

scenario_output = OUTPUT_DIR / "02_异常注入方案_v1.csv"  # 定义场景文件

scenario_df.to_csv(  # 保存场景注入记录

    scenario_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # UTF-8 BOM

)  # 保存完成


# ============================================================
# 35. 保存自动选择的 No-Explanation Window
# ============================================================

no_explanation_output = (  # 定义 No-Explanation 选择结果

    OUTPUT_DIR

    / "03_无解释案例选定窗口_v1.csv"

)  # 输出文件定义结束

no_explanation_row_df = pd.DataFrame([  # 创建单行 DataFrame

    {

        "product_id": no_explanation_product_id,  # 商品 ID

        "product_name": CORE_PRODUCTS[no_explanation_product_id],  # 商品名称

        "month": no_explanation_month,  # 稳定月份

        "review_stability_score": no_explanation_row["review_stability_score"],  # 评论稳定分数

        "source": "review_side_stable_window",  # 选择依据

        "scenario_id": "SYN-NOEXP-001",  # 后续注入的场景 ID

    }

])  # DataFrame 创建完成

no_explanation_row_df.to_csv(  # 保存结果

    no_explanation_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # UTF-8 BOM

)  # 保存完成


# ============================================================
# 36. 进行 Metric 数据质量校验
# ============================================================

validation_rows = []  # 创建质量校验结果列表


validation_rows.append({  # 检查 1

    "check_name": "daily_product_count",  # 检查名称

    "expected": 3,  # 期望三个商品

    "actual": daily_df["product_id"].nunique(),  # 实际商品数量

    "passed": daily_df["product_id"].nunique() == 3,  # 判断是否通过

})  # 检查 1 结束


validation_rows.append({  # 检查 2

    "check_name": "negative_order_count",  # 检查是否存在负订单

    "expected": 0,  # 期望 0

    "actual": int((daily_df["order_count"] < 0).sum()),  # 实际数量

    "passed": bool((daily_df["order_count"] < 0).sum() == 0),  # 是否通过

})  # 检查 2 结束


validation_rows.append({  # 检查 3

    "check_name": "refund_count_gt_order_count",  # 检查退款数是否大于订单

    "expected": 0,  # 期望没有

    "actual": int(  # 统计异常数量

        (

            daily_df["refund_count"]

            > daily_df["order_count"]

        ).sum()

    ),

    "passed": bool(  # 判断是否通过

        (

            daily_df["refund_count"]

            > daily_df["order_count"]

        ).sum()

        == 0

    ),

})  # 检查 3 结束


validation_rows.append({  # 检查 4

    "check_name": "refund_rate_out_of_range",  # 检查退款率范围

    "expected": 0,  # 期望没有越界

    "actual": int(  # 统计越界数量

        (

            (daily_df["refund_rate"] < 0)

            |

            (daily_df["refund_rate"] > 1)

        ).sum()

    ),

    "passed": bool(  # 判断是否通过

        (

            (

                (daily_df["refund_rate"] < 0)

                |

                (daily_df["refund_rate"] > 1)

            ).sum()

            == 0

        )

    ),

})  # 检查 4 结束


validation_rows.append({  # 检查 5

    "check_name": "scenario_month_count",  # 检查注入月份数

    "expected": len(scenario_df),  # 期望场景表行数

    "actual": int(monthly_df["is_injected"].sum()),  # 实际注入月份数量

    "passed": bool(  # 判断数量是否一致

        monthly_df["is_injected"].sum()

        == len(scenario_df)

    ),

})  # 检查 5 结束


validation_df = pd.DataFrame(validation_rows)  # 转换为 DataFrame

validation_output = OUTPUT_DIR / "04_指标校验_v1.csv"  # 定义校验文件

validation_df.to_csv(  # 保存质量校验

    validation_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # UTF-8 BOM

)  # 保存完成


# ============================================================
# 37. 输出 Scenario Month 的实际 Metric
# ============================================================

scenario_metric_df = monthly_df[  # 筛选所有注入月份

    monthly_df["is_injected"]

].copy()  # 创建副本

scenario_metric_df = scenario_metric_df.sort_values(  # 排序

    [

        "product_id",

        "month",

    ]

).reset_index(drop=True)  # 重建索引

scenario_metric_output = (  # 定义实际场景指标输出

    OUTPUT_DIR

    / "05_场景指标值_v1.csv"

)  # 文件路径结束

scenario_metric_df.to_csv(  # 保存场景月份指标

    scenario_metric_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # UTF-8 BOM

)  # 保存完成


# ============================================================
# 38. 控制台输出
# ============================================================

print("\n================ 08 Metric Dataset 构建完成 ================\n")  # 输出完成标题

print(f"核心商品数量：{daily_df['product_id'].nunique()}")  # 输出商品数量

print(f"Daily Metric 行数：{len(daily_df):,}")  # 输出每日宽表数量

print(f"Long Metric 行数：{len(long_df):,}")  # 输出长表数量

print(f"Monthly Metric 行数：{len(monthly_df):,}")  # 输出月度指标数量

print(f"Scenario Month 数量：{len(scenario_df)}")  # 输出场景月份数量


print("\n自动选中的 No-Explanation Case：")  # 输出 No-Explanation 标题

print(  # 输出商品和月份

    f"{CORE_PRODUCTS[no_explanation_product_id]} | "

    f"{no_explanation_month}"

)  # 输出结束


print("\nScenario Injection Plan：")  # 输出场景表标题

print(  # 打印主要场景字段

    scenario_df[

        [

            "scenario_id",

            "scenario_type",

            "product_name",

            "month",

            "order_multiplier",

            "refund_multiplier",

            "expected_review_relation",

        ]

    ]

    .to_string(index=False)

)  # 场景表打印结束


print("\n数据质量校验：")  # 输出质量检查标题

print(  # 打印校验结果

    validation_df.to_string(index=False)

)  # 打印结束


print("\n输出文件：")  # 输出文件标题

print(f"Daily CSV：{daily_csv_output}")  # Daily CSV

print(f"Daily Parquet：{daily_parquet_output}")  # Daily Parquet

print(f"Long CSV：{long_csv_output}")  # Long CSV

print(f"Long Parquet：{long_parquet_output}")  # Long Parquet

print(f"Monthly Metric：{monthly_output}")  # Monthly Metric

print(f"Scenario Plan：{scenario_output}")  # Scenario Plan

print(f"No-Explanation Window：{no_explanation_output}")  # No-Explanation

print(f"Validation：{validation_output}")  # Validation

print(f"Scenario Values：{scenario_metric_output}")  # 场景实际指标


# ============================================================
# 39. 最终提醒
# ============================================================

print(  # 输出数据语义提醒

    "\n注意：order_count / refund_count / refund_rate "

    "均为可复现 synthetic 数据，不是 Amazon 真实经营数据。"

)  # 提醒结束

print(  # 输出因果提醒

    "评论与经营场景的时间对齐属于 Evaluation Case Design，"

    "不能被解释为真实因果关系。"

)  # 提醒结束
