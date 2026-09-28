from pathlib import Path  # 导入 Path，用于处理项目目录和输出目录

import matplotlib.pyplot as plt  # 导入 matplotlib，用于生成趋势图
import pandas as pd  # 导入 pandas，用于读取和分析 Review Dataset


# ============================================================
# 1. 基础目录
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前 03_review_eda.py 所在目录

DATA_DIR = BASE_DIR / "data" / "01_标准化评论数据"  # 定义 Review Dataset 所在目录

OUTPUT_DIR = BASE_DIR / "outputs" / "02_评论探索分析"  # 定义 EDA 分析结果输出目录

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 如果输出目录不存在，则自动创建


# ============================================================
# 2. 数据文件
# ============================================================

REVIEW_FILE = DATA_DIR / "评论数据集_v1.parquet"  # 指定 02 脚本生成的 Review Dataset V1


# ============================================================
# 3. 读取 Review Dataset
# ============================================================

print("正在读取 Review Dataset V1……")  # 控制台提示开始读取数据

df = pd.read_parquet(REVIEW_FILE)  # 从 Parquet 文件读取全部评论数据

print(f"读取完成，共 {len(df):,} 条评论。")  # 输出当前数据集评论总数


# ============================================================
# 4. 基础字段检查
# ============================================================

required_columns = [  # 定义本次 EDA 必须存在的字段
    "review_id",  # 评论唯一 ID
    "sku",  # 当前项目中的商品分析 ID
    "product_name",  # 商品简称
    "rating",  # 评论评分
    "text",  # 评论正文
    "created_at",  # 评论时间
]  # 必须字段列表结束

missing_columns = [column for column in required_columns if column not in df.columns]  # 检查是否有必须字段缺失

if missing_columns:  # 判断是否存在字段缺失
    raise ValueError(f"Review Dataset 缺少字段：{missing_columns}")  # 如果字段缺失则直接停止程序


# ============================================================
# 5. 时间和评分标准化
# ============================================================

df["created_at"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")  # 确保 created_at 是标准 UTC 时间

df["rating"] = pd.to_numeric(df["rating"], errors="coerce")  # 确保评分字段是数值类型

df = df.dropna(subset=["created_at", "rating"]).copy()  # 删除时间或评分仍然为空的异常记录


# ============================================================
# 6. 增加月份字段
# ============================================================

df["month"] = df["created_at"].dt.tz_localize(None).dt.to_period("M").dt.to_timestamp()  # 把每条评论归属到对应月份


# ============================================================
# 7. 增加 EDA 用的评分倾向字段
# ============================================================

def rating_polarity(rating):  # 定义根据星级生成简单倾向标签的函数

    if rating <= 2:  # 如果评分为 1～2 星
        return "negative"  # 暂时记为负向反馈

    if rating == 3:  # 如果评分正好为 3 星
        return "neutral"  # 暂时记为中性反馈

    return "positive"  # 4～5 星暂时记为正向反馈


df["polarity"] = df["rating"].apply(rating_polarity)  # 为每条评论增加 polarity 字段


# ============================================================
# 8. 商品整体统计
# ============================================================

product_summary = df.groupby(["sku", "product_name"]).agg(  # 按商品汇总整体评论情况

    review_count=("review_id", "count"),  # 统计总评论数量

    avg_rating=("rating", "mean"),  # 计算整体平均评分

    first_review=("created_at", "min"),  # 获取最早评论时间

    last_review=("created_at", "max"),  # 获取最新评论时间

).reset_index()  # 把 sku 和 product_name 恢复为普通字段


# ============================================================
# 9. 统计正向、中性、负向评论数量
# ============================================================

polarity_counts = df.pivot_table(  # 创建商品 × polarity 的数量透视表

    index="sku",  # 每一行对应一个商品

    columns="polarity",  # 每一列对应一种评分倾向

    values="review_id",  # 使用 review_id 进行计数

    aggfunc="count",  # 聚合方式使用计数

    fill_value=0,  # 没有数据的位置填 0

)  # 透视表创建结束

for polarity in ["positive", "neutral", "negative"]:  # 遍历三种评分倾向

    if polarity not in polarity_counts.columns:  # 判断当前倾向列是否缺失
        polarity_counts[polarity] = 0  # 如果缺失则补一列 0


polarity_counts = polarity_counts.reset_index()  # 把 sku 从索引恢复成普通字段

product_summary = product_summary.merge(polarity_counts, on="sku", how="left")  # 把评分倾向数量合并到商品总表


# ============================================================
# 10. 计算正向、中性、负向比例
# ============================================================

product_summary["positive_ratio"] = product_summary["positive"] / product_summary["review_count"]  # 计算正向评论比例

product_summary["neutral_ratio"] = product_summary["neutral"] / product_summary["review_count"]  # 计算中性评论比例

product_summary["negative_ratio"] = product_summary["negative"] / product_summary["review_count"]  # 计算负向评论比例


# ============================================================
# 11. 计算活跃月份数量和时间跨度
# ============================================================

active_months = df.groupby("sku")["month"].nunique()  # 统计每个商品实际出现评论的月份数量

product_summary["active_months"] = product_summary["sku"].map(active_months)  # 把活跃月份数量加入商品总表

product_summary["span_days"] = (  # 计算最早评论到最新评论之间的时间跨度
    product_summary["last_review"] - product_summary["first_review"]  # 用最新评论时间减去最早评论时间
).dt.days  # 把时间差转换为天数


# ============================================================
# 12. 保存商品整体统计
# ============================================================

product_summary_output = OUTPUT_DIR / "01_商品汇总.csv"  # 定义商品整体统计输出文件

product_summary.to_csv(product_summary_output, index=False, encoding="utf-8-sig")  # 保存商品整体统计


# ============================================================
# 13. 生成月度基础统计
# ============================================================

monthly_base = df.groupby(["sku", "product_name", "month"]).agg(  # 按商品和月份聚合评论

    review_count=("review_id", "count"),  # 统计当月评论数量

    avg_rating=("rating", "mean"),  # 计算当月平均评分

).reset_index()  # 恢复普通字段


# ============================================================
# 14. 生成月度评分倾向统计
# ============================================================

monthly_polarity = df.pivot_table(  # 创建商品 × 月份 × polarity 的评论数量表

    index=["sku", "month"],  # 每一行对应某商品的某个月

    columns="polarity",  # 三种评分倾向分别作为列

    values="review_id",  # 使用 review_id 计数

    aggfunc="count",  # 聚合方式为计数

    fill_value=0,  # 缺失值填 0

).reset_index()  # 把索引恢复为普通列


for polarity in ["positive", "neutral", "negative"]:  # 遍历三种倾向

    if polarity not in monthly_polarity.columns:  # 判断某种倾向是否不存在
        monthly_polarity[polarity] = 0  # 不存在则补 0


# ============================================================
# 15. 合并月度统计
# ============================================================

monthly = monthly_base.merge(  # 开始合并月度基础数据与评分倾向数据

    monthly_polarity,  # 传入评分倾向统计表

    on=["sku", "month"],  # 使用 sku 和 month 对齐

    how="left",  # 保留所有月度基础统计

)  # 合并结束


# ============================================================
# 16. 计算月度评分倾向比例
# ============================================================

monthly["positive_ratio"] = monthly["positive"] / monthly["review_count"]  # 计算当月正向反馈比例

monthly["neutral_ratio"] = monthly["neutral"] / monthly["review_count"]  # 计算当月中性反馈比例

monthly["negative_ratio"] = monthly["negative"] / monthly["review_count"]  # 计算当月负向反馈比例


# ============================================================
# 17. 排序
# ============================================================

monthly = monthly.sort_values(["sku", "month"]).reset_index(drop=True)  # 按商品和月份顺序排列


# ============================================================
# 18. 计算月度变化
# ============================================================

monthly["review_count_change_pct"] = monthly.groupby("sku")["review_count"].pct_change()  # 计算评论量环比变化比例

monthly["avg_rating_change"] = monthly.groupby("sku")["avg_rating"].diff()  # 计算平均评分环比变化

monthly["positive_ratio_change"] = monthly.groupby("sku")["positive_ratio"].diff()  # 计算正向反馈比例环比变化

monthly["negative_ratio_change"] = monthly.groupby("sku")["negative_ratio"].diff()  # 计算负向反馈比例环比变化


# ============================================================
# 19. 计算变化强度分数
# ============================================================

monthly["change_score"] = (  # 构建一个只用于 EDA 排序的变化强度分数

    monthly["review_count_change_pct"].abs().clip(upper=3).fillna(0)  # 评论量变化作为一个信号

    + monthly["avg_rating_change"].abs().fillna(0)  # 平均评分变化作为一个信号

    + monthly["positive_ratio_change"].abs().fillna(0) * 3  # 正向反馈比例变化作为一个信号

    + monthly["negative_ratio_change"].abs().fillna(0) * 3  # 负向反馈比例变化作为一个信号

)  # 变化强度计算结束


# ============================================================
# 20. 保存完整月度统计
# ============================================================

monthly_output = OUTPUT_DIR / "02_评论月度指标.csv"  # 定义完整月度统计输出路径

monthly.to_csv(monthly_output, index=False, encoding="utf-8-sig")  # 保存所有商品的月度统计


# ============================================================
# 21. 找出每个商品变化最明显的月份
# ============================================================

candidate_windows = (  # 开始构建候选变化窗口表

    monthly[monthly["review_count"] >= 30]  # 暂时要求单月至少 30 条评论，避免极小样本制造假波动

    .sort_values(["sku", "change_score"], ascending=[True, False])  # 按商品和变化强度从高到低排序

    .groupby("sku")  # 按商品分别处理

    .head(10)  # 每个商品保留变化最明显的前 10 个月

    .copy()  # 创建独立 DataFrame

)  # 候选窗口构建结束


# ============================================================
# 22. 保存候选变化窗口
# ============================================================

candidate_output = OUTPUT_DIR / "03_候选变化窗口.csv"  # 定义候选时间窗口输出文件

candidate_windows.to_csv(candidate_output, index=False, encoding="utf-8-sig")  # 保存候选月份


# ============================================================
# 23. 生成每个商品的月度评论量趋势图
# ============================================================

for sku, product_df in monthly.groupby("sku"):  # 逐个商品生成趋势图

    product_name = product_df["product_name"].iloc[0]  # 获取当前商品名称

    plt.figure(figsize=(12, 5))  # 创建单独的评论量趋势图

    plt.plot(product_df["month"], product_df["review_count"])  # 绘制月度评论数量变化曲线

    plt.title(f"{product_name} - Monthly Review Count")  # 设置图表标题

    plt.xlabel("Month")  # 设置横轴名称

    plt.ylabel("Review Count")  # 设置纵轴名称

    plt.xticks(rotation=45)  # 旋转月份标签，避免重叠

    plt.tight_layout()  # 自动调整图表边距

    plt.savefig(OUTPUT_DIR / f"{sku}_01_review_count.png", dpi=160)  # 保存评论量趋势图

    plt.close()  # 关闭当前图表，释放内存


# ============================================================
# 24. 生成每个商品的月度平均评分趋势图
# ============================================================

for sku, product_df in monthly.groupby("sku"):  # 再次逐个商品生成评分趋势图

    product_name = product_df["product_name"].iloc[0]  # 获取当前商品名称

    plt.figure(figsize=(12, 5))  # 创建单独的平均评分趋势图

    plt.plot(product_df["month"], product_df["avg_rating"])  # 绘制月度平均评分变化

    plt.title(f"{product_name} - Monthly Average Rating")  # 设置标题

    plt.xlabel("Month")  # 设置横轴名称

    plt.ylabel("Average Rating")  # 设置纵轴名称

    plt.ylim(1, 5)  # 固定评分纵轴为 Amazon 的 1～5 星

    plt.xticks(rotation=45)  # 旋转月份标签

    plt.tight_layout()  # 自动整理边距

    plt.savefig(OUTPUT_DIR / f"{sku}_02_avg_rating.png", dpi=160)  # 保存评分趋势图

    plt.close()  # 关闭当前图表


# ============================================================
# 25. 生成每个商品的正/中/负反馈比例趋势图
# ============================================================

for sku, product_df in monthly.groupby("sku"):  # 逐个商品生成反馈结构趋势图

    product_name = product_df["product_name"].iloc[0]  # 获取当前商品名称

    plt.figure(figsize=(12, 5))  # 创建独立趋势图

    plt.plot(product_df["month"], product_df["positive_ratio"], label="Positive 4-5")  # 绘制正向反馈比例

    plt.plot(product_df["month"], product_df["neutral_ratio"], label="Neutral 3")  # 绘制中性反馈比例

    plt.plot(product_df["month"], product_df["negative_ratio"], label="Negative 1-2")  # 绘制负向反馈比例

    plt.title(f"{product_name} - Monthly Rating Polarity")  # 设置标题

    plt.xlabel("Month")  # 设置横轴名称

    plt.ylabel("Ratio")  # 设置纵轴名称

    plt.ylim(0, 1)  # 比例固定在 0～1 之间

    plt.legend()  # 显示图例

    plt.xticks(rotation=45)  # 旋转月份标签

    plt.tight_layout()  # 自动调整图表布局

    plt.savefig(OUTPUT_DIR / f"{sku}_03_polarity.png", dpi=160)  # 保存反馈结构图

    plt.close()  # 关闭当前图表


# ============================================================
# 26. 控制台输出整体统计
# ============================================================

display_columns = [  # 定义控制台需要展示的字段

    "sku",  # 商品 ID

    "product_name",  # 商品名称

    "review_count",  # 评论数量

    "avg_rating",  # 平均评分

    "positive_ratio",  # 正向反馈比例

    "neutral_ratio",  # 中性反馈比例

    "negative_ratio",  # 负向反馈比例

    "active_months",  # 活跃月份数量

    "span_days",  # 数据时间跨度

]  # 展示字段结束

print("\n================ 商品整体情况 ================\n")  # 打印标题

print(product_summary[display_columns].to_string(index=False))  # 输出商品整体统计


# ============================================================
# 27. 控制台输出变化最明显的月份
# ============================================================

candidate_display_columns = [  # 定义候选窗口展示字段

    "sku",  # 商品 ID

    "product_name",  # 商品名称

    "month",  # 月份

    "review_count",  # 当月评论数

    "avg_rating",  # 当月平均评分

    "positive_ratio",  # 当月正向比例

    "negative_ratio",  # 当月负向比例

    "review_count_change_pct",  # 评论量环比变化

    "avg_rating_change",  # 评分环比变化

    "change_score",  # 综合变化强度

]  # 候选窗口字段定义结束

print("\n================ 候选变化窗口 ================\n")  # 打印标题

print(candidate_windows[candidate_display_columns].to_string(index=False))  # 输出候选月份


# ============================================================
# 28. 输出文件位置
# ============================================================

print(f"\n商品统计：{product_summary_output}")  # 输出商品整体统计文件路径

print(f"月度数据：{monthly_output}")  # 输出完整月度统计文件路径

print(f"候选窗口：{candidate_output}")  # 输出候选变化窗口文件路径

print(f"趋势图目录：{OUTPUT_DIR}")  # 输出趋势图保存目录
