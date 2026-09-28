from pathlib import Path  # 导入 Path，用于处理文件路径

import re  # 导入正则表达式，用于基础文本清洗

import numpy as np  # 导入 numpy，用于数值计算

import pandas as pd  # 导入 pandas，用于数据分析

from sklearn.feature_extraction.text import CountVectorizer  # 导入 CountVectorizer，用于统计词和短语出现频率


# ============================================================
# 1. 基础目录
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前脚本所在目录

REVIEW_FILE = BASE_DIR / "data" / "01_标准化评论数据" / "评论数据集_v1.parquet"  # 指定完整 Review Dataset

WINDOW_FILE = BASE_DIR / "outputs" / "03_稳定时间窗" / "02_候选稳定窗口.csv"  # 指定 04 脚本产生的候选窗口

OUTPUT_DIR = BASE_DIR / "outputs" / "04_主题探索"  # 定义本次 Topic 探索输出目录

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 如果输出目录不存在则创建


# ============================================================
# 2. 本轮重点研究窗口
# ============================================================

TARGET_WINDOWS = [  # 定义我们上一轮筛出的重点窗口

    ("B07P9S6R9Y", "2020-06-01"),  # Yootech 无线充：明显负向变化

    ("B07P9S6R9Y", "2022-02-01"),  # Yootech 无线充：明显负向变化

    ("B07P9S6R9Y", "2023-04-01"),  # Yootech 无线充：明显负向变化

    ("B0BYJ8ZVWH", "2022-07-01"),  # AILKIN 墙充：明显正向变化

    ("B0BYJ8ZVWH", "2022-08-01"),  # AILKIN 墙充：紧接着出现负向变化

    ("B07H2V5YLH", "2022-11-01"),  # Ailun 钢化膜：正向恢复

    ("B07H2V5YLH", "2022-12-01"),  # Ailun 钢化膜：继续改善

]  # 重点窗口定义结束


# ============================================================
# 3. 文本统计参数
# ============================================================

MIN_TERM_DOCS = 5  # 一个词至少在当前窗口 5 条评论中出现，才进入候选

TOP_TERMS = 15  # 每个窗口、每种评论类型最多输出 15 个明显变化词

MAX_FEATURES = 20000  # 最多保留 20000 个 unigram / bigram 特征

RANDOM_SEED = 42  # 固定随机种子，保证人工抽样结果可复现


# ============================================================
# 4. 自定义停用词
# ============================================================

CUSTOM_STOP_WORDS = [  # 定义一些评论中高频但诊断价值较低的词

    "product",  # 通用“产品”

    "item",  # 通用“商品”

    "amazon",  # 平台名称

    "bought",  # 购买行为词

    "buy",  # 购买行为词

    "purchased",  # 购买行为词

    "purchase",  # 购买行为词

    "really",  # 程度副词

    "just",  # 通用副词

    "thing",  # 泛化名词

    "things",  # 泛化名词复数

    "got",  # 高频但通常无诊断价值

    "get",  # 高频通用动词

    "like",  # 高频表达词

]  # 自定义停用词结束


# ============================================================
# 5. 文本清洗函数
# ============================================================

def clean_text(text):  # 定义基础文本清洗函数

    if pd.isna(text):  # 如果文本为空

        return ""  # 返回空字符串

    text = str(text).lower()  # 转成字符串并统一为小写

    text = re.sub(r"http\S+", " ", text)  # 删除 URL

    text = re.sub(r"[^a-z0-9\s'-]", " ", text)  # 保留英文、数字、空格、撇号和连字符

    text = re.sub(r"\s+", " ", text).strip()  # 合并连续空格并去除首尾空格

    return text  # 返回清洗后的文本


# ============================================================
# 6. 星级转为 EDA Polarity
# ============================================================

def rating_to_polarity(rating):  # 定义星级到简单倾向的映射

    if rating <= 2:  # 1～2 星

        return "negative"  # 记为负向

    if rating == 3:  # 3 星

        return "neutral"  # 记为中性

    return "positive"  # 4～5 星记为正向


# ============================================================
# 7. 读取评论数据
# ============================================================

print("正在读取 Review Dataset V1……")  # 输出当前步骤

reviews = pd.read_parquet(REVIEW_FILE)  # 读取完整评论数据

reviews["created_at"] = pd.to_datetime(  # 标准化评论时间

    reviews["created_at"],  # 使用 created_at 字段

    utc=True,  # 保持 UTC 时间

    errors="coerce",  # 无法解析的值转为空

)  # 时间转换结束

reviews["month"] = (  # 创建月份字段

    reviews["created_at"]  # 从评论时间开始

    .dt.tz_localize(None)  # 去掉时区，便于月份比较

    .dt.to_period("M")  # 转为月份 Period

    .dt.to_timestamp()  # 再转成 Timestamp

)  # 月份字段生成结束

reviews["rating"] = pd.to_numeric(  # 确保评分为数值

    reviews["rating"],  # 使用 rating 字段

    errors="coerce",  # 异常值转为空

)  # 数值转换结束

reviews["polarity"] = reviews["rating"].apply(rating_to_polarity)  # 创建简单星级倾向字段


# ============================================================
# 8. 合并标题和正文
# ============================================================

reviews["title"] = reviews["title"].fillna("")  # 标题为空时填空字符串

reviews["text"] = reviews["text"].fillna("")  # 正文为空时填空字符串

reviews["full_text"] = (  # 合并标题和正文

    reviews["title"].astype(str)  # 标题转字符串

    + " "  # 中间增加空格

    + reviews["text"].astype(str)  # 拼接评论正文

)  # 合并结束

reviews["clean_text"] = reviews["full_text"].apply(clean_text)  # 对完整文本进行基础清洗

print(f"评论读取完成，共 {len(reviews):,} 条。")  # 输出数据量


# ============================================================
# 9. 读取稳定窗口结果
# ============================================================

windows = pd.read_csv(WINDOW_FILE)  # 读取 04 脚本结果

windows["baseline_start"] = pd.to_datetime(windows["baseline_start"])  # 转换基线开始时间

windows["baseline_end"] = pd.to_datetime(windows["baseline_end"])  # 转换基线结束时间

windows["current_month"] = pd.to_datetime(windows["current_month"])  # 转换当前月份


# ============================================================
# 10. 筛选本轮研究窗口
# ============================================================

target_keys = {  # 把目标窗口转换成方便匹配的集合

    (sku, pd.Timestamp(month))  # 每个元素由商品 ID 和当前月份组成

    for sku, month in TARGET_WINDOWS  # 遍历目标窗口

}  # 集合生成结束

selected_windows = windows[  # 开始筛选目标窗口

    windows.apply(  # 对每一行判断是否属于目标窗口

        lambda row: (row["sku"], row["current_month"]) in target_keys,  # 判断当前 sku + month 是否存在于目标集合

        axis=1,  # 按行执行

    )  # apply 结束

].copy()  # 创建独立 DataFrame


if len(selected_windows) != len(TARGET_WINDOWS):  # 判断是否有指定窗口没有找到

    print(  # 输出提醒

        f"注意：计划分析 {len(TARGET_WINDOWS)} 个窗口，"

        f"实际在候选文件中找到 {len(selected_windows)} 个。"  # 打印实际找到数量

    )  # 提示结束


# ============================================================
# 11. 保存选中的窗口
# ============================================================

selected_windows_output = OUTPUT_DIR / "01_选定窗口.csv"  # 定义选中窗口输出文件

selected_windows.to_csv(  # 保存目标窗口

    selected_windows_output,  # 输出路径

    index=False,  # 不保存 DataFrame 索引

    encoding="utf-8-sig",  # 使用 Excel 友好的 UTF-8 编码

)  # 保存结束


# ============================================================
# 12. 词频变化分析函数
# ============================================================

def extract_term_shifts(  # 定义词和短语变化分析函数

    baseline_df,  # 基线评论数据

    current_df,  # 当前窗口评论数据

    segment_name,  # 当前分析的是 all / positive / negative

):  # 函数定义开始

    baseline_texts = baseline_df["clean_text"].tolist()  # 获取基线文本列表

    current_texts = current_df["clean_text"].tolist()  # 获取当前窗口文本列表


    if len(baseline_texts) < 10:  # 如果基线评论太少

        return pd.DataFrame()  # 返回空结果


    if len(current_texts) < 10:  # 如果当前窗口评论太少

        return pd.DataFrame()  # 返回空结果


    all_texts = baseline_texts + current_texts  # 合并基线和当前文本


    stop_words = list(CUSTOM_STOP_WORDS)  # 复制自定义停用词列表


    vectorizer = CountVectorizer(  # 创建词频向量器

        lowercase=True,  # 统一转小写

        stop_words=stop_words,  # 使用自定义停用词

        ngram_range=(1, 2),  # 同时分析单词和双词短语

        binary=True,  # 只统计“该评论是否出现”，而不是出现多少次

        min_df=3,  # 至少在 3 条评论中出现

        max_features=MAX_FEATURES,  # 限制最大特征数量

        token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z'-]+\b",  # 主要保留英文词

    )  # 向量器创建结束


    try:  # 尝试建立词频矩阵

        matrix = vectorizer.fit_transform(all_texts)  # 把评论转成二值词频矩阵

    except ValueError:  # 如果当前文本没有有效词

        return pd.DataFrame()  # 返回空结果


    terms = np.array(vectorizer.get_feature_names_out())  # 获取所有词和双词短语


    baseline_matrix = matrix[:len(baseline_texts)]  # 截取基线部分矩阵

    current_matrix = matrix[len(baseline_texts):]  # 截取当前窗口部分矩阵


    baseline_doc_count = np.asarray(  # 计算每个 term 在多少条基线评论中出现

        baseline_matrix.sum(axis=0)  # 按列求和

    ).ravel()  # 转成一维数组


    current_doc_count = np.asarray(  # 计算每个 term 在多少条当前评论中出现

        current_matrix.sum(axis=0)  # 按列求和

    ).ravel()  # 转成一维数组


    baseline_share = baseline_doc_count / len(baseline_texts)  # 计算 term 在基线评论中的覆盖率

    current_share = current_doc_count / len(current_texts)  # 计算 term 在当前评论中的覆盖率


    share_delta = current_share - baseline_share  # 计算当前窗口相对于基线的覆盖率变化


    lift = (  # 计算平滑后的相对提升倍数

        current_share + 0.005  # 当前覆盖率加小平滑值

    ) / (  # 除以

        baseline_share + 0.005  # 基线覆盖率加相同平滑值

    )  # Lift 计算结束


    term_df = pd.DataFrame({  # 创建词变化 DataFrame

        "segment": segment_name,  # 当前分析分组

        "term": terms,  # 词或短语

        "baseline_doc_count": baseline_doc_count,  # 基线出现文档数

        "current_doc_count": current_doc_count,  # 当前出现文档数

        "baseline_share": baseline_share,  # 基线覆盖比例

        "current_share": current_share,  # 当前覆盖比例

        "share_delta": share_delta,  # 覆盖率变化

        "lift": lift,  # 当前相对基线提升倍数

    })  # DataFrame 创建结束


    term_df = term_df[  # 开始过滤极低频词

        term_df["current_doc_count"] >= MIN_TERM_DOCS  # 当前月至少出现在 5 条评论中

    ].copy()  # 创建独立结果


    term_df["abs_delta"] = term_df["share_delta"].abs()  # 创建绝对变化值用于辅助排序


    term_df = term_df.sort_values(  # 对词进行排序

        ["share_delta", "current_doc_count"],  # 优先看覆盖率增加，再看当前文档数

        ascending=[False, False],  # 两列均从高到低

    ).reset_index(drop=True)  # 重置索引


    return term_df  # 返回完整 term 变化表


# ============================================================
# 13. 存储所有输出结果
# ============================================================

window_summary_rows = []  # 保存窗口整体统计

term_shift_frames = []  # 保存不同窗口的 term shift

representative_rows = []  # 保存代表性原始评论

manual_sample_frames = []  # 保存人工复核样本


# ============================================================
# 14. 逐个窗口分析
# ============================================================

for _, window in selected_windows.iterrows():  # 遍历每一个重点候选窗口

    sku = window["sku"]  # 获取商品 ID

    product_name = window["product_name"]  # 获取商品名称

    baseline_start = window["baseline_start"]  # 获取基线开始月份

    baseline_end = window["baseline_end"]  # 获取基线结束月份

    current_month = window["current_month"]  # 获取当前分析月份


    product_reviews = reviews[  # 筛选当前商品全部评论

        reviews["sku"] == sku  # 只保留当前商品

    ].copy()  # 创建副本


    baseline_df = product_reviews[  # 筛选基线窗口评论

        (product_reviews["month"] >= baseline_start)  # 评论月份不早于基线开始

        &

        (product_reviews["month"] <= baseline_end)  # 评论月份不晚于基线结束

    ].copy()  # 创建副本


    current_df = product_reviews[  # 筛选当前月份评论

        product_reviews["month"] == current_month  # 评论月份等于当前月份

    ].copy()  # 创建副本


    print("\n==================================================")  # 输出分隔线

    print(f"{product_name} | {current_month.strftime('%Y-%m')}")  # 输出当前商品和月份

    print(f"Baseline 评论：{len(baseline_df):,}")  # 输出基线评论数

    print(f"Current 评论：{len(current_df):,}")  # 输出当前评论数


    # ========================================================
    # 15. 保存窗口整体统计
    # ========================================================

    window_summary_rows.append({  # 保存窗口概览

        "sku": sku,  # 商品 ID

        "product_name": product_name,  # 商品名称

        "baseline_start": baseline_start,  # 基线开始

        "baseline_end": baseline_end,  # 基线结束

        "current_month": current_month,  # 当前月份

        "signal_type": window["signal_type"],  # 04 阶段识别出的信号类型

        "baseline_review_count": len(baseline_df),  # 基线评论数

        "current_review_count": len(current_df),  # 当前评论数

        "baseline_avg_rating": baseline_df["rating"].mean(),  # 基线平均评分

        "current_avg_rating": current_df["rating"].mean(),  # 当前平均评分

        "baseline_negative_ratio": (baseline_df["polarity"] == "negative").mean(),  # 基线负向比例

        "current_negative_ratio": (current_df["polarity"] == "negative").mean(),  # 当前负向比例

        "baseline_positive_ratio": (baseline_df["polarity"] == "positive").mean(),  # 基线正向比例

        "current_positive_ratio": (current_df["polarity"] == "positive").mean(),  # 当前正向比例

    })  # 窗口统计保存结束


    # ========================================================
    # 16. 分三种 Segment 做 Topic 探索
    # ========================================================

    segments = {  # 定义要分析的评论子集

        "all": (baseline_df, current_df),  # 全部评论

        "negative": (  # 负向评论

            baseline_df[baseline_df["polarity"] == "negative"],  # 基线 1～2 星

            current_df[current_df["polarity"] == "negative"],  # 当前 1～2 星

        ),  # negative 定义结束

        "positive": (  # 正向评论

            baseline_df[baseline_df["polarity"] == "positive"],  # 基线 4～5 星

            current_df[current_df["polarity"] == "positive"],  # 当前 4～5 星

        ),  # positive 定义结束

    }  # segments 定义结束


    for segment_name, (segment_baseline, segment_current) in segments.items():  # 遍历三个 Segment

        term_df = extract_term_shifts(  # 分析词和短语变化

            segment_baseline,  # 当前 Segment 的基线评论

            segment_current,  # 当前 Segment 的当前评论

            segment_name,  # Segment 名称

        )  # 分析结束


        if term_df.empty:  # 如果没有结果

            continue  # 跳过当前 Segment


        term_df["sku"] = sku  # 增加商品 ID

        term_df["product_name"] = product_name  # 增加商品名称

        term_df["current_month"] = current_month  # 增加当前月份


        top_term_df = term_df.head(TOP_TERMS).copy()  # 只保留变化最大的前 15 个 term


        term_shift_frames.append(top_term_df)  # 保存 term 变化结果


        print(f"\n[{segment_name}] 当前明显增加的词/短语：")  # 输出 Segment 名称


        for _, term_row in top_term_df.head(10).iterrows():  # 控制台只展示前 10 个

            print(  # 打印 term

                f"  {term_row['term']:<25} "

                f"当前 {term_row['current_share']:.1%} | "

                f"基线 {term_row['baseline_share']:.1%} | "

                f"Δ {term_row['share_delta']:+.1%}"  # 输出覆盖率变化

            )  # 打印结束


        # ====================================================
        # 17. 为明显增加的词寻找代表性评论
        # ====================================================

        for _, term_row in top_term_df.head(8).iterrows():  # 每个 Segment 前 8 个 term 找原文

            term = term_row["term"]  # 获取当前词或短语


            matching_reviews = segment_current[  # 筛选当前窗口中包含该 term 的评论

                segment_current["clean_text"].str.contains(  # 判断清洗文本是否包含 term

                    re.escape(term),  # 对 term 做正则转义

                    case=False,  # 不区分大小写

                    regex=True,  # 使用正则匹配

                    na=False,  # 空值按 False 处理

                )  # contains 结束

            ].copy()  # 创建副本


            if matching_reviews.empty:  # 如果没有匹配评论

                continue  # 跳过


            if "helpful_vote" in matching_reviews.columns:  # 如果存在 helpful_vote 字段

                matching_reviews["helpful_vote"] = pd.to_numeric(  # 标准化 helpful_vote

                    matching_reviews["helpful_vote"],  # 使用 helpful_vote

                    errors="coerce",  # 异常值转为空

                ).fillna(0)  # 空值填 0


                matching_reviews = matching_reviews.sort_values(  # 按有用票数排序

                    ["helpful_vote", "rating"],  # 优先 helpful_vote，其次评分

                    ascending=[False, True],  # helpful 高优先，低评分优先

                )  # 排序结束


            matching_reviews = matching_reviews.head(2)  # 每个 term 最多保留 2 条代表评论


            for _, review_row in matching_reviews.iterrows():  # 遍历代表评论

                representative_rows.append({  # 保存代表性评论

                    "sku": sku,  # 商品 ID

                    "product_name": product_name,  # 商品名称

                    "current_month": current_month,  # 当前月份

                    "segment": segment_name,  # 当前 Segment

                    "term": term,  # 触发该评论的候选词

                    "review_id": review_row["review_id"],  # 评论 ID

                    "rating": review_row["rating"],  # 评论评分

                    "created_at": review_row["created_at"],  # 评论时间

                    "title": review_row["title"],  # 评论标题

                    "text": review_row["text"],  # 评论正文

                    "helpful_vote": review_row.get("helpful_vote", None),  # 有用票数

                })  # 代表评论保存结束


    # ========================================================
    # 18. 生成一份人工复核样本
    # ========================================================

    for polarity_name, sample_size in [  # 定义不同倾向的抽样数量

        ("negative", 15),  # 负向抽 15 条

        ("neutral", 5),  # 中性抽 5 条

        ("positive", 15),  # 正向抽 15 条

    ]:  # 抽样配置结束


        polarity_df = current_df[  # 筛选当前倾向评论

            current_df["polarity"] == polarity_name  # 使用 polarity 过滤

        ].copy()  # 创建副本


        if polarity_df.empty:  # 如果该倾向没有评论

            continue  # 跳过


        actual_sample_size = min(  # 计算实际抽样数量

            sample_size,  # 目标数量

            len(polarity_df),  # 不能超过实际评论数量

        )  # 实际数量计算结束


        sample_df = polarity_df.sample(  # 随机抽样

            n=actual_sample_size,  # 抽样数量

            random_state=RANDOM_SEED,  # 固定随机种子保证可复现

        ).copy()  # 创建副本


        sample_df["sample_polarity"] = polarity_name  # 记录抽样倾向

        sample_df["window_month"] = current_month  # 记录当前研究窗口

        manual_sample_frames.append(sample_df)  # 保存人工检查样本


# ============================================================
# 19. 保存窗口总体统计
# ============================================================

window_summary_df = pd.DataFrame(window_summary_rows)  # 把窗口统计转换为 DataFrame

window_summary_output = OUTPUT_DIR / "02_窗口汇总.csv"  # 定义输出路径

window_summary_df.to_csv(  # 保存窗口统计

    window_summary_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # 使用 UTF-8 BOM

)  # 保存结束


# ============================================================
# 20. 保存词和短语变化结果
# ============================================================

if term_shift_frames:  # 如果存在 term shift 结果

    term_shift_df = pd.concat(  # 合并所有窗口结果

        term_shift_frames,  # 传入结果列表

        ignore_index=True,  # 重新生成索引

    )  # 合并结束

else:  # 如果完全没有 term 结果

    term_shift_df = pd.DataFrame()  # 创建空 DataFrame


term_shift_output = OUTPUT_DIR / "03_词项变化.csv"  # 定义输出文件

term_shift_df.to_csv(  # 保存 term shift

    term_shift_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # Excel 友好编码

)  # 保存结束


# ============================================================
# 21. 保存代表性评论
# ============================================================

representative_df = pd.DataFrame(representative_rows)  # 创建代表性评论表

representative_df = representative_df.drop_duplicates(  # 删除同一评论重复出现

    subset=["sku", "current_month", "segment", "review_id"]  # 根据窗口、Segment、review_id 去重

)  # 去重结束

representative_output = OUTPUT_DIR / "04_代表性评论.csv"  # 定义代表评论文件

representative_df.to_csv(  # 保存代表评论

    representative_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # 使用 UTF-8 BOM

)  # 保存结束


# ============================================================
# 22. 保存人工复核样本
# ============================================================

if manual_sample_frames:  # 如果存在人工抽样数据

    manual_samples_df = pd.concat(  # 合并全部人工抽样

        manual_sample_frames,  # 传入抽样结果

        ignore_index=True,  # 重建索引

    )  # 合并完成

else:  # 如果没有样本

    manual_samples_df = pd.DataFrame()  # 创建空表


manual_samples_output = OUTPUT_DIR / "05_人工复核样本.csv"  # 定义人工复核样本文件

manual_samples_df.to_csv(  # 保存人工复核样本

    manual_samples_output,  # 输出文件

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # UTF-8 BOM

)  # 保存结束


# ============================================================
# 23. 最终输出
# ============================================================

print("\n================ Topic 探索完成 ================\n")  # 输出完成提示

print(f"选中窗口：{selected_windows_output}")  # 输出选中窗口文件

print(f"窗口统计：{window_summary_output}")  # 输出窗口统计文件

print(f"词语变化：{term_shift_output}")  # 输出 term shift 文件

print(f"代表评论：{representative_output}")  # 输出代表评论文件

print(f"人工样本：{manual_samples_output}")  # 输出人工复核样本文件
