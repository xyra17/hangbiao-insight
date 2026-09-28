# 数据选品脚本
# 从海量 3C 评论里，筛出最适合做我们项目的商品。主要看：
# * 评论数量够不够
# * 负面评论够不够
# * 时间跨度够不够
# * 评论正文是否完整
# * 商品是不是适合做异常诊断
#
# 输出两个文件：
# * `01_候选商品/候选商品Top100.csv`
# * `01_候选商品/候选商品Top20.csv`
#
# 我们真正要看的是 **Top20**。
# 下一步就是：Top20 候选商品 → 人工挑出最终 3～5 个商品 → 抽取这些商品的评论 → 做正式数据集和评测 Case。



from collections import defaultdict  # 导入 defaultdict，用于为每个 parent_asin 自动创建统计对象
from datetime import datetime, timezone  # 导入日期时间工具，用于把毫秒时间戳转换为日期
from heapq import nlargest  # 导入 nlargest，用于不创建超大 DataFrame 就找出评分最高的候选商品
from math import log1p  # 导入 log1p，用于压缩评论数量差距，避免评论特别多的商品完全支配评分
from pathlib import Path  # 导入 Path，用于安全创建 outputs 输出目录

import pandas as pd  # 导入 pandas，用于最终生成和保存候选商品表格
from datasets import load_dataset  # 导入 Hugging Face datasets，用于流式读取评论和 Metadata
from tqdm import tqdm  # 导入 tqdm，用于在扫描大数据集时显示进度


# ============================================================
# 1. 基础配置
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前 01_scan_candidates.py 所在目录
OUTPUT_DIR = BASE_DIR / "outputs" / "01_候选商品"  # 定义候选商品结果目录
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 如果 outputs 目录不存在，则自动创建

REVIEW_FILE = "hf://datasets/McAuley-Lab/Amazon-Reviews-2023/raw/review_categories/Cell_Phones_and_Accessories.jsonl"  # 定义手机及配件评论数据地址

META_FILES = [  # 定义手机及配件 Metadata 的 7 个 Parquet 分片
    f"hf://datasets/McAuley-Lab/Amazon-Reviews-2023/raw_meta_Cell_Phones_and_Accessories/full-{i:05d}-of-00007.parquet"  # 根据分片编号自动构造文件地址
    for i in range(7)  # 生成 0 到 6 共 7 个 Metadata 文件
]  # Metadata 文件列表结束

MIN_REVIEW_COUNT = 300  # 首轮要求一个商品至少拥有 300 条评论
MIN_NEGATIVE_COUNT = 60  # 首轮要求至少存在 60 条 1～3 星评论
MIN_SPAN_DAYS = 365  # 首轮要求评论时间跨度至少达到 365 天
MIN_TEXT_RATIO = 0.90  # 首轮要求至少 90% 的评论存在正文
META_LOOKUP_LIMIT = 100  # 评论扫描后只给综合评分最高的 100 个商品查询 Metadata
FINAL_TOP_N = 20  # 最终输出 20 个最值得进一步检查的商品


# ============================================================
# 2. 定义轻量级商品统计对象
# ============================================================

class ProductStat:  # 定义一个专门保存商品统计信息的轻量对象
    __slots__ = (  # 使用 __slots__ 减少大量商品对象占用的内存
        "review_count",  # 保存评论总数
        "rating_sum",  # 保存评分总和，用于计算平均评分
        "negative_count",  # 保存 1～3 星评论数量
        "very_negative_count",  # 保存 1～2 星评论数量
        "non_empty_text",  # 保存正文非空评论数量
        "verified_count",  # 保存 Verified Purchase 评论数量
        "min_timestamp",  # 保存最早评论时间
        "max_timestamp",  # 保存最晚评论时间
    )  # __slots__ 字段定义结束

    def __init__(self):  # 定义 ProductStat 初始化方法
        self.review_count = 0  # 初始评论数量设为 0
        self.rating_sum = 0.0  # 初始评分总和设为 0
        self.negative_count = 0  # 初始 1～3 星评论数量设为 0
        self.very_negative_count = 0  # 初始 1～2 星评论数量设为 0
        self.non_empty_text = 0  # 初始非空正文数量设为 0
        self.verified_count = 0  # 初始 Verified Purchase 数量设为 0
        self.min_timestamp = None  # 初始最早时间设为空
        self.max_timestamp = None  # 初始最晚时间设为空


# ============================================================
# 3. 定义安全转换函数
# ============================================================

def safe_float(value, default=None):  # 定义安全浮点数转换函数，避免脏数据导致程序中断
    try:  # 尝试进行浮点数转换
        return float(value)  # 如果转换成功，则返回浮点数
    except (TypeError, ValueError):  # 捕获空值或格式错误
        return default  # 转换失败时返回默认值


def safe_int(value, default=None):  # 定义安全整数转换函数
    try:  # 尝试把输入转换成整数
        return int(value)  # 转换成功时返回整数
    except (TypeError, ValueError):  # 捕获空值和异常格式
        return default  # 转换失败时返回默认值


# ============================================================
# 4. 加载评论数据
# ============================================================

print("正在连接 Amazon Reviews 2023 评论数据……")  # 在控制台提示当前阶段

reviews = load_dataset(  # 使用 Hugging Face 读取评论数据
    "json",  # 明确指定当前数据格式为 JSON/JSONL
    data_files=REVIEW_FILE,  # 指定 Cell Phones & Accessories 评论文件
    split="train",  # 把这个单文件数据视为 train split
    streaming=True,  # 使用流式模式，避免一次把 20M+ 评论载入内存
)  # 评论数据加载配置结束

print("评论数据连接成功。")  # 告知用户数据源已经连接


# ============================================================
# 5. 扫描全部评论
# ============================================================

stats = defaultdict(ProductStat)  # 创建 parent_asin -> ProductStat 的统计容器
total_rows = 0  # 初始化已经扫描的评论数量
skipped_rows = 0  # 初始化因为缺少 parent_asin 而跳过的记录数量

print("\n开始扫描评论，请耐心等待……")  # 提示开始执行大规模评论扫描

for row in tqdm(reviews, desc="扫描评论"):  # 按流式方式逐条遍历评论，并显示进度
    total_rows += 1  # 每读取一条评论，总扫描数量加 1

    parent_asin = row.get("parent_asin")  # 读取当前评论所属的 parent_asin

    if not parent_asin:  # 判断当前记录是否缺少商品主 ID
        skipped_rows += 1  # 如果缺少 parent_asin，则记录一次跳过
        continue  # 跳过这条无法归属商品的评论

    stat = stats[parent_asin]  # 获取当前商品对应的轻量统计对象

    stat.review_count += 1  # 当前商品评论总数加 1

    rating = safe_float(row.get("rating"))  # 安全读取当前评论评分

    if rating is not None:  # 只有评分有效时才参与评分统计
        stat.rating_sum += rating  # 把当前评分加入评分总和

        if rating <= 3.0:  # 判断当前评分是否属于 1～3 星
            stat.negative_count += 1  # 如果属于，则负面候选评论数加 1

        if rating <= 2.0:  # 判断当前评分是否属于更强烈的 1～2 星负面评论
            stat.very_negative_count += 1  # 如果属于，则强负面评论数加 1

    text = row.get("text")  # 读取当前评论正文

    if text is not None and str(text).strip():  # 判断正文是否真实存在且不是空字符串
        stat.non_empty_text += 1  # 正文有效时，非空正文数量加 1

    if row.get("verified_purchase") is True:  # 判断当前评论是否来自 Verified Purchase
        stat.verified_count += 1  # 如果是验证购买评论，则对应计数加 1

    timestamp = safe_int(row.get("timestamp"))  # 安全读取当前评论的毫秒时间戳

    if timestamp is not None:  # 只有时间戳有效时才更新时间范围
        if stat.min_timestamp is None or timestamp < stat.min_timestamp:  # 判断是否出现了更早的评论
            stat.min_timestamp = timestamp  # 更新当前商品的最早评论时间

        if stat.max_timestamp is None or timestamp > stat.max_timestamp:  # 判断是否出现了更晚的评论
            stat.max_timestamp = timestamp  # 更新当前商品的最晚评论时间


# ============================================================
# 6. 输出第一轮扫描基本信息
# ============================================================

print(f"\n扫描完成，总评论记录：{total_rows:,}")  # 输出实际读取到的评论总量
print(f"缺少 parent_asin 的记录：{skipped_rows:,}")  # 输出无法归属商品的评论数量
print(f"发现 parent_asin 数量：{len(stats):,}")  # 输出数据中不同 parent_asin 的数量


# ============================================================
# 7. 把商品统计转换成候选记录
# ============================================================

candidate_records = []  # 创建通过首轮基础条件的候选商品列表

for parent_asin, stat in stats.items():  # 遍历每个商品及其统计结果
    if stat.review_count < MIN_REVIEW_COUNT:  # 判断评论数量是否低于 300
        continue  # 评论太少的商品直接排除

    if stat.negative_count < MIN_NEGATIVE_COUNT:  # 判断 1～3 星评论数量是否低于要求
        continue  # 缺少足够问题样本的商品直接排除

    text_ratio = stat.non_empty_text / stat.review_count  # 计算评论正文完整率

    if text_ratio < MIN_TEXT_RATIO:  # 判断正文完整率是否低于 90%
        continue  # 正文质量较差的商品直接排除

    if stat.min_timestamp is None or stat.max_timestamp is None:  # 判断该商品是否缺少有效时间信息
        continue  # 没有时间信息就无法做异常窗口分析，因此排除

    min_date = datetime.fromtimestamp(stat.min_timestamp / 1000, tz=timezone.utc)  # 把最早毫秒时间戳转换成 UTC 日期
    max_date = datetime.fromtimestamp(stat.max_timestamp / 1000, tz=timezone.utc)  # 把最晚毫秒时间戳转换成 UTC 日期

    span_days = (max_date - min_date).days  # 计算该商品评论覆盖的总天数

    if span_days < MIN_SPAN_DAYS:  # 判断评论时间跨度是否低于一年
        continue  # 时间跨度过短则不适合做前后窗口诊断

    avg_rating = stat.rating_sum / stat.review_count  # 计算当前商品在评论数据中的平均评分
    negative_ratio = stat.negative_count / stat.review_count  # 计算 1～3 星评论比例
    very_negative_ratio = stat.very_negative_count / stat.review_count  # 计算 1～2 星评论比例
    verified_ratio = stat.verified_count / stat.review_count  # 计算 Verified Purchase 评论比例

    volume_score = min(log1p(stat.review_count) / log1p(10000), 1.0)  # 根据评论数量生成 0～1 的数量得分
    negative_score = min(stat.negative_count / 1000, 1.0)  # 根据负面候选评论数量生成 0～1 得分
    span_score = min(span_days / 1460, 1.0)  # 按最多四年的时间跨度生成 0～1 得分
    text_score = min(text_ratio, 1.0)  # 正文完整率直接作为文本质量得分
    verified_score = min(verified_ratio, 1.0)  # Verified Purchase 比例作为真实性辅助得分

    suitability_score = (volume_score * 0.30 + negative_score * 0.25 + span_score * 0.20 + text_score * 0.15 + verified_score * 0.10)  # 按权重计算项目适用性综合分

    candidate_records.append({  # 把当前商品的筛选统计结果加入候选列表
        "parent_asin": parent_asin,  # 保存商品主 ID
        "review_count": stat.review_count,  # 保存评论总数
        "avg_rating_from_reviews": round(avg_rating, 3),  # 保存根据评论计算出的平均评分
        "negative_1_3_count": stat.negative_count,  # 保存 1～3 星评论数量
        "negative_1_3_ratio": round(negative_ratio, 4),  # 保存 1～3 星评论比例
        "negative_1_2_count": stat.very_negative_count,  # 保存 1～2 星评论数量
        "negative_1_2_ratio": round(very_negative_ratio, 4),  # 保存 1～2 星评论比例
        "text_ratio": round(text_ratio, 4),  # 保存正文完整率
        "verified_ratio": round(verified_ratio, 4),  # 保存验证购买比例
        "first_review_date": min_date.date().isoformat(),  # 保存最早评论日期
        "last_review_date": max_date.date().isoformat(),  # 保存最晚评论日期
        "span_days": span_days,  # 保存评论时间跨度
        "suitability_score": round(suitability_score, 4),  # 保存项目适用性综合评分
    })  # 当前候选商品记录构造结束


# ============================================================
# 8. 选出最值得查询 Metadata 的 100 个商品
# ============================================================

top_candidates = nlargest(META_LOOKUP_LIMIT, candidate_records, key=lambda item: item["suitability_score"])  # 从所有候选中取综合评分最高的 100 个

target_parent_asins = {item["parent_asin"] for item in top_candidates}  # 把 Top100 parent_asin 转换成集合，加快 Metadata 匹配

print(f"\n满足基础条件的商品：{len(candidate_records):,}")  # 输出基础筛选后还剩多少商品
print(f"将查询 Metadata 的候选商品：{len(target_parent_asins):,}")  # 输出即将补充商品信息的数量


# ============================================================
# 9. 加载标准 Parquet Metadata
# ============================================================

print("\n开始读取商品 Metadata……")  # 提示即将进入 Metadata 查询阶段

metadata_stream = load_dataset(  # 读取已经转换为标准 Parquet 的商品 Metadata
    "parquet",  # 明确指定数据格式为 Parquet
    data_files={"train": META_FILES},  # 传入全部 7 个 Metadata 分片
    split="train",  # 读取 train 这一逻辑 split
    streaming=True,  # 使用 streaming，避免把全部 Metadata 放入内存
)  # Metadata 数据加载配置结束


# ============================================================
# 10. 只保存 Top100 对应的 Metadata
# ============================================================

metadata_map = {}  # 创建 parent_asin -> 商品 Metadata 的映射表

for meta in tqdm(metadata_stream, desc="匹配商品 Metadata"):  # 流式遍历 Metadata
    parent_asin = meta.get("parent_asin")  # 获取 Metadata 中的 parent_asin

    if parent_asin not in target_parent_asins:  # 判断当前 Metadata 是否属于 Top100 候选
        continue  # 与我们候选无关时立即跳过，避免保存无关信息

    categories = meta.get("categories") or []  # 获取商品层级分类，如果不存在则使用空列表
    category_path = " > ".join(str(item) for item in categories)  # 把分类列表拼接成可阅读的层级路径

    metadata_map[parent_asin] = {  # 保存当前候选商品真正需要的 Metadata
        "title": meta.get("title"),  # 保存商品标题
        "main_category": meta.get("main_category"),  # 保存商品主分类
        "category_path": category_path,  # 保存商品详细分类路径
        "store": meta.get("store"),  # 保存商品店铺或品牌店名称
        "price": meta.get("price"),  # 保存数据抓取时记录的商品价格
        "meta_average_rating": meta.get("average_rating"),  # 保存商品页 Metadata 中的平均评分
        "meta_rating_number": meta.get("rating_number"),  # 保存商品页显示的评分数量
    }  # 当前商品 Metadata 保存结束

    if len(metadata_map) == len(target_parent_asins):  # 判断 Top100 商品 Metadata 是否已经全部找到
        break  # 如果已经全部找到，则提前终止 Metadata 扫描，节约时间


# ============================================================
# 11. 合并评论统计与 Metadata
# ============================================================

final_records = []  # 创建最终候选商品结果列表

for item in top_candidates:  # 遍历前面筛出的 Top100 评论候选商品
    parent_asin = item["parent_asin"]  # 获取当前候选商品 ID
    meta = metadata_map.get(parent_asin, {})  # 根据 parent_asin 获取商品 Metadata，找不到时返回空字典
    merged = {**item, **meta}  # 合并评论统计字段和商品 Metadata 字段
    final_records.append(merged)  # 把合并后的商品记录加入最终列表


# ============================================================
# 12. 转换成 DataFrame 并排序
# ============================================================

final_df = pd.DataFrame(final_records)  # 把最终商品记录转换为 pandas DataFrame

final_df = final_df.sort_values("suitability_score", ascending=False)  # 按项目适用性综合评分从高到低排序

top20_df = final_df.head(FINAL_TOP_N).copy()  # 只取综合排名最高的 20 个商品用于人工检查


# ============================================================
# 13. 保存 CSV 文件
# ============================================================

top100_output = OUTPUT_DIR / "候选商品Top100.csv"  # 定义 Top100 候选商品输出路径
top20_output = OUTPUT_DIR / "候选商品Top20.csv"  # 定义 Top20 候选商品输出路径

final_df.to_csv(top100_output, index=False, encoding="utf-8-sig")  # 保存完整 Top100，并使用 utf-8-sig 方便 Excel 打开
top20_df.to_csv(top20_output, index=False, encoding="utf-8-sig")  # 保存最终 Top20，并使用 utf-8-sig 避免中文乱码


# ============================================================
# 14. 在控制台展示我们最关心的字段
# ============================================================

display_columns = [  # 定义控制台最终需要展示的字段
    "parent_asin",  # 展示商品主 ID
    "title",  # 展示真实商品标题
    "category_path",  # 展示详细商品类别
    "review_count",  # 展示评论数量
    "avg_rating_from_reviews",  # 展示评论计算平均分
    "negative_1_3_count",  # 展示 1～3 星评论数量
    "negative_1_3_ratio",  # 展示 1～3 星评论比例
    "span_days",  # 展示评论时间跨度
    "verified_ratio",  # 展示 Verified Purchase 比例
    "suitability_score",  # 展示最终项目适用性评分
]  # 控制台字段定义结束

existing_display_columns = [column for column in display_columns if column in top20_df.columns]  # 防止个别 Metadata 字段缺失造成显示报错

print("\n================ Top 20 候选商品 ================\n")  # 输出结果标题
print(top20_df[existing_display_columns].to_string(index=False))  # 在控制台完整打印 Top20 商品及其关键数据
print(f"\nTop100 已保存：{top100_output}")  # 告诉用户 Top100 CSV 的保存位置
print(f"Top20 已保存：{top20_output}")  # 告诉用户 Top20 CSV 的保存位置
print("\n下一步不要直接确定最终 SKU，需要先人工检查 Top20 的具体商品类型。")  # 提醒下一阶段仍需人工业务筛选
