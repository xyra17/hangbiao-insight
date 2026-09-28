from pathlib import Path  # 导入 Path，用于处理文件目录

import numpy as np  # 导入 numpy，用于数值计算
import pandas as pd  # 导入 pandas，用于数据分析


# ============================================================
# 1. 基础目录
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前脚本所在目录

INPUT_FILE = BASE_DIR / "outputs" / "02_评论探索分析" / "02_评论月度指标.csv"  # 读取 03 脚本生成的月度数据

OUTPUT_DIR = BASE_DIR / "outputs" / "03_稳定时间窗"  # 定义本次分析结果目录

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 如果目录不存在则创建


# ============================================================
# 2. 参数配置
# ============================================================

BASELINE_MONTHS = 3  # 使用当前月之前连续 3 个月作为基线

MIN_CURRENT_REVIEWS = 50  # 当前月至少需要 50 条评论

MIN_BASELINE_MONTH_REVIEWS = 30  # 基线中的每个月至少需要 30 条评论

MAX_RATING_STD = 0.30  # 基线 3 个月评分标准差不能超过 0.30

MAX_NEGATIVE_RATIO_STD = 0.08  # 基线负向比例标准差不能超过 8 个百分点

MAX_POSITIVE_RATIO_STD = 0.08  # 基线正向比例标准差不能超过 8 个百分点


# ============================================================
# 3. 读取月度数据
# ============================================================

print("正在读取月度评论数据……")  # 输出当前步骤

df = pd.read_csv(INPUT_FILE)  # 读取 03 脚本生成的数据

df["month"] = pd.to_datetime(df["month"])  # 把月份字段转换成日期类型

df = df.sort_values(["sku", "month"]).reset_index(drop=True)  # 按商品和月份排序

print(f"读取完成，共 {len(df):,} 条商品-月份记录。")  # 输出读取数量


# ============================================================
# 4. 保存最终候选窗口
# ============================================================

results = []  # 创建最终结果列表


# ============================================================
# 5. 按商品分别分析
# ============================================================

for sku, product_df in df.groupby("sku"):  # 逐个商品进行分析

    product_df = product_df.sort_values("month").reset_index(drop=True)  # 确保月份按时间排序

    product_name = product_df["product_name"].iloc[0]  # 获取当前商品名称


    # ========================================================
    # 6. 从第 4 个月开始分析
    # ========================================================

    for i in range(BASELINE_MONTHS, len(product_df)):  # 从拥有完整 3 个月基线的位置开始循环

        current = product_df.iloc[i]  # 获取当前月数据

        baseline = product_df.iloc[i - BASELINE_MONTHS:i].copy()  # 获取前 3 个月作为基线


        # ====================================================
        # 7. 检查月份是否连续
        # ====================================================

        expected_months = pd.date_range(  # 创建理论上应该连续出现的月份

            start=baseline["month"].iloc[0],  # 从第一个基线月开始

            periods=BASELINE_MONTHS + 1,  # 一共需要 3 个基线月 + 当前月

            freq="MS",  # MS 表示每月第一天

        )  # 连续月份生成结束


        actual_months = list(baseline["month"]) + [current["month"]]  # 获取真实月份序列


        if list(expected_months) != actual_months:  # 判断月份是否完全连续

            continue  # 中间存在缺失月份则跳过


        # ====================================================
        # 8. 检查样本量
        # ====================================================

        if current["review_count"] < MIN_CURRENT_REVIEWS:  # 当前月评论太少时

            continue  # 跳过当前窗口


        if (baseline["review_count"] < MIN_BASELINE_MONTH_REVIEWS).any():  # 判断基线是否存在低样本月份

            continue  # 如果存在则跳过


        # ====================================================
        # 9. 检查基线是否足够稳定
        # ====================================================

        baseline_rating_std = baseline["avg_rating"].std()  # 计算基线平均评分波动

        baseline_negative_std = baseline["negative_ratio"].std()  # 计算基线负向比例波动

        baseline_positive_std = baseline["positive_ratio"].std()  # 计算基线正向比例波动


        if baseline_rating_std > MAX_RATING_STD:  # 如果评分本身就非常不稳定

            continue  # 不适合作为稳定基线


        if baseline_negative_std > MAX_NEGATIVE_RATIO_STD:  # 如果负向比例本身波动太大

            continue  # 跳过


        if baseline_positive_std > MAX_POSITIVE_RATIO_STD:  # 如果正向比例本身波动太大

            continue  # 跳过


        # ====================================================
        # 10. 计算基线加权平均
        # ====================================================

        baseline_total_reviews = baseline["review_count"].sum()  # 计算基线总评论数

        baseline_avg_rating = np.average(  # 计算基线加权平均评分

            baseline["avg_rating"],  # 使用每月平均评分

            weights=baseline["review_count"],  # 使用每月评论量作为权重

        )  # 平均评分计算结束


        baseline_positive_ratio = np.average(  # 计算基线正向反馈比例

            baseline["positive_ratio"],  # 使用每月正向比例

            weights=baseline["review_count"],  # 使用评论量作为权重

        )  # 正向比例计算结束


        baseline_negative_ratio = np.average(  # 计算基线负向反馈比例

            baseline["negative_ratio"],  # 使用每月负向比例

            weights=baseline["review_count"],  # 使用评论量作为权重

        )  # 负向比例计算结束


        baseline_monthly_reviews = baseline["review_count"].mean()  # 计算基线平均每月评论量


        # ====================================================
        # 11. 计算当前月相对于基线的变化
        # ====================================================

        rating_delta = current["avg_rating"] - baseline_avg_rating  # 计算评分变化

        positive_delta = current["positive_ratio"] - baseline_positive_ratio  # 计算正向比例变化

        negative_delta = current["negative_ratio"] - baseline_negative_ratio  # 计算负向比例变化

        review_volume_ratio = current["review_count"] / baseline_monthly_reviews  # 计算评论量相对于基线的倍数


        # ====================================================
        # 12. 判断评论信号方向
        # ====================================================

        negative_shift = (  # 定义负向变化条件

            rating_delta <= -0.30  # 平均评分下降至少 0.30

            or negative_delta >= 0.08  # 或负向评论比例增加至少 8 个百分点

            or positive_delta <= -0.08  # 或正向评论比例下降至少 8 个百分点

        )  # 负向条件结束


        positive_shift = (  # 定义正向变化条件

            rating_delta >= 0.30  # 平均评分提升至少 0.30

            or negative_delta <= -0.08  # 或负向比例下降至少 8 个百分点

            or positive_delta >= 0.08  # 或正向比例提升至少 8 个百分点

        )  # 正向条件结束


        volume_surge = review_volume_ratio >= 1.80  # 评论量达到基线的 1.8 倍则认为明显增加

        volume_drop = review_volume_ratio <= 0.55  # 评论量低于基线的 55% 则认为明显下降


        # ====================================================
        # 13. 给窗口分类
        # ====================================================

        if negative_shift and positive_shift:  # 如果正负信号同时触发

            signal_type = "mixed_shift"  # 标记为混合变化

        elif negative_shift:  # 如果主要是负向变化

            signal_type = "negative_shift"  # 标记为负向变化

        elif positive_shift:  # 如果主要是正向变化

            signal_type = "positive_shift"  # 标记为正向变化

        elif volume_surge:  # 如果只有评论量明显增加

            signal_type = "volume_surge"  # 标记为评论量激增

        elif volume_drop:  # 如果只有评论量明显下降

            signal_type = "volume_drop"  # 标记为评论量下降

        else:  # 如果没有明显变化

            signal_type = "stable"  # 标记为稳定


        # ====================================================
        # 14. 计算变化强度
        # ====================================================

        signal_score = (  # 综合计算变化强度

            abs(rating_delta)  # 评分变化贡献

            + abs(positive_delta) * 3  # 正向比例变化贡献

            + abs(negative_delta) * 3  # 负向比例变化贡献

            + min(abs(review_volume_ratio - 1), 2)  # 评论量变化贡献，并限制极端低基数影响

        )  # 综合分计算结束


        # ====================================================
        # 15. 保存当前窗口
        # ====================================================

        results.append({  # 保存完整窗口数据

            "sku": sku,  # 商品 ID

            "product_name": product_name,  # 商品名称

            "baseline_start": baseline["month"].iloc[0],  # 基线开始月份

            "baseline_end": baseline["month"].iloc[-1],  # 基线结束月份

            "current_month": current["month"],  # 当前分析月份

            "baseline_review_count": int(baseline_total_reviews),  # 基线总评论量

            "current_review_count": int(current["review_count"]),  # 当前月评论量

            "review_volume_ratio": round(review_volume_ratio, 4),  # 评论量相对基线倍数

            "baseline_avg_rating": round(baseline_avg_rating, 4),  # 基线评分

            "current_avg_rating": round(current["avg_rating"], 4),  # 当前评分

            "rating_delta": round(rating_delta, 4),  # 评分变化

            "baseline_positive_ratio": round(baseline_positive_ratio, 4),  # 基线正向比例

            "current_positive_ratio": round(current["positive_ratio"], 4),  # 当前正向比例

            "positive_ratio_delta": round(positive_delta, 4),  # 正向比例变化

            "baseline_negative_ratio": round(baseline_negative_ratio, 4),  # 基线负向比例

            "current_negative_ratio": round(current["negative_ratio"], 4),  # 当前负向比例

            "negative_ratio_delta": round(negative_delta, 4),  # 负向比例变化

            "baseline_rating_std": round(baseline_rating_std, 4),  # 基线评分稳定程度

            "signal_type": signal_type,  # 当前窗口的变化类型

            "signal_score": round(signal_score, 4),  # 当前窗口变化强度

        })  # 当前窗口保存结束


# ============================================================
# 16. 转换为 DataFrame
# ============================================================

result_df = pd.DataFrame(results)  # 把所有窗口转换为 DataFrame


# ============================================================
# 17. 保存全部有效窗口
# ============================================================

all_output = OUTPUT_DIR / "01_全部稳定窗口.csv"  # 定义全部窗口文件路径

result_df.to_csv(all_output, index=False, encoding="utf-8-sig")  # 保存全部有效窗口


# ============================================================
# 18. 过滤真正有明显信号的窗口
# ============================================================

interesting_df = result_df[result_df["signal_type"] != "stable"].copy()  # 删除没有明显变化的稳定月份

interesting_df = interesting_df.sort_values(  # 按变化强度排序

    "signal_score",  # 使用 signal_score 排序

    ascending=False,  # 从高到低

).reset_index(drop=True)  # 重新生成索引


# ============================================================
# 19. 每个商品保留前 10 个候选窗口
# ============================================================

top_windows = interesting_df.groupby("sku").head(10).copy()  # 每个商品取最明显的前 10 个窗口


# ============================================================
# 20. 保存候选窗口
# ============================================================

candidate_output = OUTPUT_DIR / "02_候选稳定窗口.csv"  # 定义最终候选窗口文件

top_windows.to_csv(candidate_output, index=False, encoding="utf-8-sig")  # 保存候选窗口


# ============================================================
# 21. 控制台展示
# ============================================================

display_columns = [  # 定义控制台展示字段

    "sku",  # 商品 ID

    "product_name",  # 商品名称

    "current_month",  # 当前月份

    "signal_type",  # 信号类型

    "current_review_count",  # 当前评论数量

    "review_volume_ratio",  # 评论量相对基线倍数

    "baseline_avg_rating",  # 基线评分

    "current_avg_rating",  # 当前评分

    "rating_delta",  # 评分变化

    "negative_ratio_delta",  # 负向比例变化

    "positive_ratio_delta",  # 正向比例变化

    "signal_score",  # 综合变化强度

]  # 展示字段结束


print("\n================ 稳定基线候选窗口 ================\n")  # 打印标题

print(top_windows[display_columns].to_string(index=False))  # 输出候选结果


# ============================================================
# 22. 输出文件位置
# ============================================================

print(f"\n全部稳定窗口：{all_output}")  # 输出全部结果地址

print(f"候选变化窗口：{candidate_output}")  # 输出重点候选窗口地址
