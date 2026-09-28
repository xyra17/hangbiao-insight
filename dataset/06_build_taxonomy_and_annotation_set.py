from pathlib import Path  # 导入 Path，用于处理文件路径

import json  # 导入 json，用于保存 Taxonomy 配置

import pandas as pd  # 导入 pandas，用于读取、筛选和抽样评论


# ============================================================
# 1. 基础目录
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前脚本所在目录

REVIEW_FILE = BASE_DIR / "data" / "01_标准化评论数据" / "评论数据集_v1.parquet"  # 指定 Review Dataset V1

OUTPUT_DIR = BASE_DIR / "outputs" / "05_标注集_v1"  # 定义本阶段输出目录

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 如果输出目录不存在则自动创建


# ============================================================
# 2. 随机种子
# ============================================================

RANDOM_SEED = 42  # 固定随机种子，保证每次抽样结果可复现


# ============================================================
# 3. P0 核心商品
# ============================================================

CORE_PRODUCTS = {  # 定义进入 P0 Golden Set 的三个核心商品

    "B07P9S6R9Y": "Yootech_Wireless_Charger",  # 无线充电器

    "B0BYJ8ZVWH": "AILKIN_USB_Wall_Charger",  # USB 墙充

    "B07H2V5YLH": "Ailun_Screen_Protector",  # 钢化膜

}  # 商品定义结束


# ============================================================
# 4. 重点评论变化窗口
# ============================================================

FOCUS_WINDOWS = [  # 定义 04 / 05 阶段已经发现的重点窗口

    {
        "product_id": "B07P9S6R9Y",  # Yootech 无线充
        "month": "2020-06-01",  # 2020 年 6 月
        "signal": "negative_shift",  # 明显负向变化
    },

    {
        "product_id": "B07P9S6R9Y",  # Yootech 无线充
        "month": "2022-02-01",  # 2022 年 2 月
        "signal": "negative_shift",  # 明显负向变化
    },

    {
        "product_id": "B07P9S6R9Y",  # Yootech 无线充
        "month": "2023-04-01",  # 2023 年 4 月
        "signal": "negative_shift",  # 明显负向变化
    },

    {
        "product_id": "B0BYJ8ZVWH",  # AILKIN 墙充
        "month": "2022-07-01",  # 2022 年 7 月
        "signal": "positive_shift",  # 明显正向变化
    },

    {
        "product_id": "B0BYJ8ZVWH",  # AILKIN 墙充
        "month": "2022-08-01",  # 2022 年 8 月
        "signal": "negative_shift",  # 随后发生负向变化
    },

    {
        "product_id": "B07H2V5YLH",  # Ailun 钢化膜
        "month": "2022-11-01",  # 2022 年 11 月
        "signal": "positive_shift",  # 正向恢复
    },

    {
        "product_id": "B07H2V5YLH",  # Ailun 钢化膜
        "month": "2022-12-01",  # 2022 年 12 月
        "signal": "positive_shift",  # 持续改善
    },

]  # 重点窗口定义结束


# ============================================================
# 5. 每个重点窗口的抽样数量
# ============================================================

FOCUS_SAMPLE_TARGET = {  # 每个重点窗口计划抽 30 条评论

    "negative": 12,  # 1～2 星抽 12 条

    "neutral": 4,  # 3 星抽 4 条

    "positive": 14,  # 4～5 星抽 14 条

}  # 重点窗口抽样配置结束


# ============================================================
# 6. 每个商品普通时期的抽样数量
# ============================================================

BACKGROUND_SAMPLE_TARGET = {  # 每个商品普通时期计划抽 50 条评论

    "negative": 20,  # 1～2 星抽 20 条

    "neutral": 6,  # 3 星抽 6 条

    "positive": 24,  # 4～5 星抽 24 条

}  # 普通时期抽样配置结束


# ============================================================
# 7. 定义 Taxonomy V1
# ============================================================

TAXONOMY_V1 = {  # 定义第一版评论主题体系

    "version": "taxonomy-v1-draft",  # 当前仍是待人工验证版本

    "design": {  # 定义整体标签设计原则

        "unit": "topic_claim",  # 每个标签对应一个明确评论语义

        "multi_label": True,  # 一条评论允许存在多个 Topic

        "topic_has_fixed_polarity": False,  # Topic 本身不自带正负属性

        "evidence_span_required": True,  # 每一个标签都必须对应评论原文 Evidence Span

        "rating_is_not_polarity": True,  # 星级不能直接替代语义 Polarity

    },

    "polarity": [  # 定义正式标注允许使用的 Polarity

        "positive",  # 明确正向评价

        "negative",  # 明确负向评价

        "neutral",  # 客观描述，没有明显正负态度

    ],

    "topics": {  # 开始定义一级 Topic

        "function_performance": {  # 功能 / 性能

            "definition": "产品核心功能是否能够正常实现，以及性能表现如何。",

            "subtopics": [

                "charging_success",  # 是否能够成功充电

                "charging_speed",  # 充电速度

                "protection_effectiveness",  # 屏幕保护效果

                "general_performance",  # 其他功能表现

            ],

        },

        "reliability_durability": {  # 可靠性 / 耐用性

            "definition": "产品是否稳定工作，以及经过一段时间使用后是否失效或损坏。",

            "subtopics": [

                "stopped_working",  # 使用后完全失效

                "intermittent_failure",  # 时好时坏

                "short_lifespan",  # 使用寿命短

                "cracking_breakage",  # 开裂 / 破损

                "wear_damage",  # 磨损或其他耐用性问题

            ],

        },

        "compatibility_fit": {  # 兼容性 / 适配

            "definition": "产品与设备、保护壳、接口、尺寸等是否匹配。",

            "subtopics": [

                "device_compatibility",  # 与具体设备兼容性

                "case_compatibility",  # 与手机壳等配件兼容性

                "size_fit",  # 尺寸是否合适

                "port_fit",  # 接口适配情况

            ],

        },

        "safety_thermal": {  # 安全 / 温度

            "definition": "产品是否出现异常发热、融化、起火或异常声音等安全信号。",

            "subtopics": [

                "overheating",  # 过热

                "melting_fire",  # 融化 / 起火

                "abnormal_noise",  # 异常声音

            ],

        },

        "usability_installation": {  # 使用体验 / 安装

            "definition": "产品安装、定位、操作和日常使用是否方便。",

            "subtopics": [

                "positioning_alignment",  # 无线充定位等

                "installation_difficulty",  # 安装困难

                "bubbles_adhesion",  # 气泡 / 粘贴问题

                "ease_of_use",  # 是否方便使用

            ],

        },

        "convenience_value": {  # 便利性 / 价值感

            "definition": "多设备能力、便携性、价格价值以及使用便利性。",

            "subtopics": [

                "dual_port_multi_device",  # 双口 / 多设备

                "portability_size",  # 体积 / 便携性

                "price_value",  # 性价比

                "general_convenience",  # 其他便利性

            ],

        },

        "product_quality": {  # 产品质量

            "definition": "材料、做工、出厂状态及附件本身的质量。",

            "subtopics": [

                "material_build_quality",  # 材质与做工

                "defective_on_arrival",  # 到货即存在产品缺陷

                "accessory_condition",  # 附件或配套物料状态

            ],

        },

        "other_unknown": {  # 其他 / 无法判断

            "definition": "评论信息不足，或者无法可靠归入已有 Topic。",

            "subtopics": [

                "insufficient_information",  # 信息不足

                "uncategorized",  # 暂未覆盖的新主题

            ],

        },

    },

}  # Taxonomy 定义结束


# ============================================================
# 8. 将 Taxonomy 保存为 JSON
# ============================================================

taxonomy_output = OUTPUT_DIR / "01_主题分类体系_v1.json"  # 定义 Taxonomy 输出路径

with open(taxonomy_output, "w", encoding="utf-8") as f:  # 使用 UTF-8 打开 JSON 文件

    json.dump(  # 保存 Taxonomy

        TAXONOMY_V1,  # 要保存的 Python 字典

        f,  # 文件对象

        ensure_ascii=False,  # 保留中文字符

        indent=2,  # 使用两格缩进

    )  # JSON 保存结束


# ============================================================
# 9. 读取 Review Dataset
# ============================================================

print("正在读取 Review Dataset V1……")  # 输出当前步骤

df = pd.read_parquet(REVIEW_FILE)  # 读取 Review Dataset

print(f"读取完成，共 {len(df):,} 条评论。")  # 输出总评论数量


# ============================================================
# 10. 字段标准化
# ============================================================

df["created_at"] = pd.to_datetime(  # 标准化 created_at

    df["created_at"],  # 使用原始时间字段

    utc=True,  # 按 UTC 处理

    errors="coerce",  # 无法解析的值变成空

)  # 时间转换结束

df["rating"] = pd.to_numeric(  # 确保 rating 是数值字段

    df["rating"],  # 使用评分字段

    errors="coerce",  # 无法解析则为空

)  # rating 转换结束

df = df.dropna(  # 删除必要字段为空的数据

    subset=[

        "review_id",  # 评论 ID 必须存在

        "sku",  # 当前数据中的商品 ID 字段必须存在

        "rating",  # 评分必须存在

        "created_at",  # 时间必须存在

        "text",  # 评论正文必须存在

    ]

).copy()  # 创建新的 DataFrame


# ============================================================
# 11. 把当前 sku 字段统一映射为 product_id
# ============================================================

df["product_id"] = df["sku"].astype(str)  # 当前数据里的 sku 实际是 parent_asin，因此统一转成 product_id

df["product_name"] = df["product_id"].map(CORE_PRODUCTS)  # 根据 product_id 获取标准商品名称


# ============================================================
# 12. 只保留三个核心商品
# ============================================================

df = df[  # 开始筛选

    df["product_id"].isin(CORE_PRODUCTS.keys())  # 只保留进入 P0 的三个商品

].copy()  # 创建独立 DataFrame


# ============================================================
# 13. 增加月份字段
# ============================================================

df["month"] = (  # 创建评论月份字段

    df["created_at"]  # 从评论时间开始

    .dt.tz_localize(None)  # 去掉时区

    .dt.to_period("M")  # 转成月粒度

    .dt.to_timestamp()  # 再转回 Timestamp

)  # 月份字段生成结束


# ============================================================
# 14. 增加星级抽样分层字段
# ============================================================

def rating_bucket(rating):  # 定义星级抽样分层函数

    if rating <= 2:  # 如果是 1～2 星

        return "negative"  # 仅用于抽样时标记为 negative bucket

    if rating == 3:  # 如果是 3 星

        return "neutral"  # 仅用于抽样时标记为 neutral bucket

    return "positive"  # 4～5 星进入 positive bucket


df["rating_bucket"] = df["rating"].apply(rating_bucket)  # 生成抽样分层字段


# ============================================================
# 15. 清理极短文本
# ============================================================

df["text"] = df["text"].astype(str).str.strip()  # 去掉正文首尾空格

df = df[  # 过滤几乎没有语义的信息

    df["text"].str.len() >= 8  # 评论正文至少 8 个字符

].copy()  # 创建副本


# ============================================================
# 16. 通用分层抽样函数
# ============================================================

def stratified_sample(  # 定义按 rating bucket 进行抽样的函数

    source_df,  # 输入评论集合

    target_dict,  # 各分层目标数量

    random_state,  # 随机种子

):  # 函数开始

    selected_parts = []  # 用于保存各 bucket 抽出的数据

    selected_ids = set()  # 用于记录已经抽中的 review_id


    for bucket_name, target_count in target_dict.items():  # 遍历各星级分层

        bucket_df = source_df[  # 筛选当前分层

            source_df["rating_bucket"] == bucket_name  # 匹配 bucket

        ].copy()  # 创建副本


        actual_count = min(  # 计算真正能够抽多少条

            target_count,  # 计划数量

            len(bucket_df),  # 当前 bucket 实际数量

        )  # 数量计算结束


        if actual_count == 0:  # 如果当前 bucket 没有评论

            continue  # 跳过


        sampled = bucket_df.sample(  # 从当前 bucket 随机抽样

            n=actual_count,  # 抽样数量

            random_state=random_state,  # 固定随机种子

        ).copy()  # 创建副本


        selected_parts.append(sampled)  # 保存当前抽样结果

        selected_ids.update(sampled["review_id"].tolist())  # 保存已抽中的 Review ID


    if selected_parts:  # 如果成功抽到了评论

        selected_df = pd.concat(  # 合并所有 bucket

            selected_parts,  # 各分层抽样结果

            ignore_index=True,  # 重建索引

        )  # 合并完成

    else:  # 如果完全没有抽到

        selected_df = pd.DataFrame(columns=source_df.columns)  # 创建空表


    target_total = sum(target_dict.values())  # 计算计划总抽样数

    missing_count = target_total - len(selected_df)  # 判断还差多少条


    if missing_count > 0:  # 如果某些 bucket 样本不足

        remaining_df = source_df[  # 从未被抽中的评论里补样本

            ~source_df["review_id"].isin(selected_ids)  # 排除已经选中的评论

        ].copy()  # 创建副本


        fill_count = min(  # 计算可以补多少条

            missing_count,  # 需要补充的数量

            len(remaining_df),  # 剩余评论数量

        )  # 数量计算结束


        if fill_count > 0:  # 如果还有数据可以补

            fill_df = remaining_df.sample(  # 从剩余数据中随机抽样

                n=fill_count,  # 补充数量

                random_state=random_state + 1000,  # 使用不同随机种子

            ).copy()  # 创建副本


            selected_df = pd.concat(  # 合并原抽样和补充样本

                [selected_df, fill_df],  # 两部分数据

                ignore_index=True,  # 重建索引

            )  # 合并结束


    return selected_df  # 返回最终抽样结果


# ============================================================
# 17. 抽取重点窗口评论
# ============================================================

focus_samples = []  # 用于保存重点窗口抽样结果

focus_window_months = {}  # 用于记录每个商品哪些月份属于重点窗口


for index, window in enumerate(FOCUS_WINDOWS):  # 遍历每一个重点变化窗口

    product_id = window["product_id"]  # 获取当前商品 ID

    month = pd.Timestamp(window["month"])  # 获取当前月份

    signal = window["signal"]  # 获取当前窗口信号类型


    focus_window_months.setdefault(  # 确保当前商品有月份集合

        product_id,  # 商品 ID

        set(),  # 默认建立空集合

    ).add(month)  # 把当前重点月份加入集合


    window_df = df[  # 筛选当前商品当前月份的评论

        (df["product_id"] == product_id)  # 商品匹配

        &

        (df["month"] == month)  # 月份匹配

    ].copy()  # 创建副本


    sampled_df = stratified_sample(  # 对当前重点窗口进行分层抽样

        window_df,  # 当前窗口全部评论

        FOCUS_SAMPLE_TARGET,  # 使用重点窗口抽样配置

        RANDOM_SEED + index,  # 每个窗口使用不同但可复现的随机种子

    )  # 抽样结束


    sampled_df["sample_scope"] = "focus_window"  # 标记为重点窗口样本

    sampled_df["window_signal"] = signal  # 保存该窗口信号类型

    sampled_df["window_month"] = month  # 保存窗口月份

    focus_samples.append(sampled_df)  # 保存当前窗口抽样结果


# ============================================================
# 18. 合并重点窗口样本
# ============================================================

focus_df = pd.concat(  # 合并七个重点窗口

    focus_samples,  # 所有重点窗口抽样数据

    ignore_index=True,  # 重建索引

)  # 合并完成


# ============================================================
# 19. 删除可能重复的重点样本
# ============================================================

focus_df = focus_df.drop_duplicates(  # 对 review_id 去重

    subset=["review_id"],  # 使用 review_id 判断重复

    keep="first",  # 保留第一次出现

).copy()  # 创建副本


# ============================================================
# 20. 抽取普通背景时期评论
# ============================================================

background_samples = []  # 保存普通时期的抽样结果


for index, product_id in enumerate(CORE_PRODUCTS.keys()):  # 遍历三个核心商品

    product_df = df[  # 获取当前商品全部评论

        df["product_id"] == product_id  # 匹配当前商品

    ].copy()  # 创建副本


    excluded_months = focus_window_months.get(  # 获取当前商品全部重点窗口

        product_id,  # 当前商品 ID

        set(),  # 如果没有则使用空集合

    )  # 获取结束


    background_df = product_df[  # 构建普通时期评论池

        ~product_df["month"].isin(excluded_months)  # 排除重点变化月份

    ].copy()  # 创建副本


    background_df = background_df[  # 再排除已经进入重点样本的评论

        ~background_df["review_id"].isin(focus_df["review_id"])  # 防止重复

    ].copy()  # 创建副本


    sampled_df = stratified_sample(  # 对普通时期进行星级分层抽样

        background_df,  # 普通时期评论池

        BACKGROUND_SAMPLE_TARGET,  # 使用普通时期抽样数量

        RANDOM_SEED + 100 + index,  # 固定随机种子

    )  # 抽样结束


    sampled_df["sample_scope"] = "background"  # 标记为普通时期

    sampled_df["window_signal"] = "background"  # 没有指定异常信号

    sampled_df["window_month"] = pd.NaT  # 普通时期没有固定窗口月份

    background_samples.append(sampled_df)  # 保存当前商品抽样


# ============================================================
# 21. 合并普通时期样本
# ============================================================

background_df = pd.concat(  # 合并三个商品普通样本

    background_samples,  # 普通时期抽样列表

    ignore_index=True,  # 重建索引

)  # 合并完成


# ============================================================
# 22. 合并正式人工标注 Review 集
# ============================================================

annotation_df = pd.concat(  # 合并重点窗口和背景样本

    [

        focus_df,  # 重点窗口样本

        background_df,  # 普通时期样本

    ],

    ignore_index=True,  # 重新生成索引

)  # 合并结束


# ============================================================
# 23. 最终去重
# ============================================================

annotation_df = annotation_df.drop_duplicates(  # 再次确保 review_id 唯一

    subset=["review_id"],  # 按 review_id 去重

    keep="first",  # 保留第一条

).copy()  # 创建副本


# ============================================================
# 24. 创建稳定 Annotation ID
# ============================================================

annotation_df = annotation_df.sort_values(  # 按商品、时间、评分排序

    [

        "product_id",  # 先按商品

        "created_at",  # 再按时间

        "rating",  # 最后按评分

    ]

).reset_index(drop=True)  # 重建索引


annotation_df["annotation_id"] = [  # 创建方便人工标注使用的 ID

    f"A-{i:04d}"  # 例如 A-0001

    for i in range(1, len(annotation_df) + 1)  # 从 1 开始编号

]  # Annotation ID 创建完成


# ============================================================
# 25. 整理人工标注文件需要的字段
# ============================================================

annotation_columns = [  # 定义人工标注主表字段

    "annotation_id",  # 人工标注样本 ID

    "review_id",  # 原始评论稳定 ID

    "product_id",  # 项目内部商品 ID

    "product_name",  # 商品名称

    "created_at",  # 评论时间

    "rating",  # Amazon 星级，仅作为辅助上下文

    "title",  # 评论标题

    "text",  # 评论正文

    "sample_scope",  # focus_window / background

    "window_signal",  # 当前窗口为何被选择

    "window_month",  # 当前重点窗口月份

    "rating_bucket",  # 仅用于抽样检查，不是正式 Polarity

]  # 字段列表结束


annotation_reviews = annotation_df[  # 只保留人工标注真正需要的字段

    annotation_columns  # 使用上面定义的列

].copy()  # 创建副本


# ============================================================
# 26. 保存人工标注 Review 主表
# ============================================================

annotation_reviews_output = OUTPUT_DIR / "02_标注评论集_v1.csv"  # 定义人工 Review 文件

annotation_reviews.to_csv(  # 保存人工标注主表

    annotation_reviews_output,  # 输出路径

    index=False,  # 不保存 DataFrame 索引

    encoding="utf-8-sig",  # 使用 Excel 兼容编码

)  # 保存完成


# ============================================================
# 27. 创建 Long Format 标签模板
# ============================================================

label_template = annotation_reviews[  # 从 Review 主表复制基础标识字段

    [

        "annotation_id",  # 标注 ID

        "review_id",  # 原始 Review ID

        "product_id",  # 商品 ID

        "product_name",  # 商品名称

    ]

].copy()  # 创建副本


# ============================================================
# 28. 增加正式标注字段
# ============================================================

label_template["label_index"] = 1  # 默认从第一个 Topic 标签开始

label_template["primary_topic"] = ""  # 一级 Topic，人工填写

label_template["secondary_topic"] = ""  # 二级 Topic，人工填写

label_template["polarity"] = ""  # 该 Topic 对应的真实语义 Polarity

label_template["evidence_span"] = ""  # 必须复制评论中直接支持标签的原文

label_template["annotator"] = ""  # 标注员

label_template["annotation_status"] = "pending"  # 默认状态为待标注

label_template["notes"] = ""  # 边界问题或备注


# ============================================================
# 29. 保存 Long Format 标签模板
# ============================================================

label_output = OUTPUT_DIR / "03_标注标签模板_v1.csv"  # 定义标签模板路径

label_template.to_csv(  # 保存标签模板

    label_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # 使用 UTF-8 BOM

)  # 保存结束


# ============================================================
# 30. 生成抽样统计
# ============================================================

sampling_summary = (  # 开始生成抽样统计

    annotation_reviews  # 使用人工标注主表

    .groupby(  # 按商品、样本来源、评分分层汇总

        [

            "product_id",  # 商品 ID

            "product_name",  # 商品名称

            "sample_scope",  # 重点窗口 / 背景

            "rating_bucket",  # 抽样星级层

        ],

        dropna=False,  # 不丢弃空值

    )

    .size()  # 统计数量

    .reset_index(name="review_count")  # 转成普通 DataFrame

)  # 汇总结束


# ============================================================
# 31. 保存抽样统计
# ============================================================

sampling_summary_output = OUTPUT_DIR / "04_抽样汇总_v1.csv"  # 定义统计文件路径

sampling_summary.to_csv(  # 保存抽样统计

    sampling_summary_output,  # 输出文件

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # Excel 友好编码

)  # 保存结束


# ============================================================
# 32. 生成人工标注说明
# ============================================================

guideline_text = """
商品经营异常智能诊断 Agent
Review Topic Annotation Guideline V1 Draft

1. 标注对象
每条评论可以有 0～N 个 Topic。
一个 Topic 对应评论中的一个明确事实、体验或判断。

2. Rating 不是 Polarity
1 星评论中也可能包含正向内容。
5 星评论中也可能包含负向问题。
正式 Polarity 必须根据 Evidence Span 的语义判断。

3. Multi-label
例如：
"Charges fast but gets extremely hot."

应该拆成：
function_performance / charging_speed / positive / "Charges fast"
safety_thermal / overheating / negative / "gets extremely hot"

4. Evidence Span
每一个 Topic 标签必须复制一段能够直接支持该标签的原始评论文字。
禁止使用模型推理出来但原文没有表达的信息。

5. Polarity
positive：明确认可、改善、成功、好用。
negative：明确抱怨、失效、风险、不符合预期。
neutral：客观描述，没有明显正负态度。

6. other_unknown
如果评论只有：
"Good."
"Bad."
"Works."
等缺乏具体业务语义的信息，可以标：
other_unknown / insufficient_information

7. 不要强行分类
Taxonomy 没有覆盖的新业务语义，应优先标：
other_unknown / uncategorized
并写入 notes。
后续统一评估是否需要增加新 Topic。

8. 同一评论允许多个标签
不要因为评论总体评分是五星，就忽略其中明确出现的负向问题。

9. 不做因果推断
评论中出现“发热”和经营指标异常同时发生，
只能说明存在评论侧信号，
不能在人工标签阶段标成“导致退款率上升”。

10. 当前版本
taxonomy-v1-draft

完成一轮人工检查后再升级并冻结为：
taxonomy-v1.0
""".strip()  # 标注说明文本结束


# ============================================================
# 33. 保存标注说明
# ============================================================

guideline_output = OUTPUT_DIR / "05_标注指南_v1.txt"  # 定义说明文件路径

with open(guideline_output, "w", encoding="utf-8") as f:  # 打开说明文件

    f.write(guideline_text)  # 写入人工标注规则


# ============================================================
# 34. 控制台输出最终结果
# ============================================================

print("\n================ 06 构建完成 ================\n")  # 输出完成提示

print(f"核心商品数量：{annotation_reviews['product_id'].nunique()}")  # 输出商品数量

print(f"人工标注 Review 数量：{len(annotation_reviews):,}")  # 输出最终样本总数

print("\n按商品统计：")  # 输出商品统计标题

print(  # 打印每个商品样本量

    annotation_reviews  # 使用人工标注表

    .groupby(["product_id", "product_name"])  # 按商品分组

    .size()  # 统计数量

    .reset_index(name="review_count")  # 转成 DataFrame

    .to_string(index=False)  # 输出完整表格

)  # 打印结束


print("\n按样本来源统计：")  # 输出样本来源统计

print(  # 打印 focus / background 数量

    annotation_reviews  # 使用标注 Review

    .groupby(["sample_scope"])  # 按样本类型分组

    .size()  # 统计数量

    .reset_index(name="review_count")  # 转成 DataFrame

    .to_string(index=False)  # 打印

)  # 输出结束


print("\n按星级抽样层统计：")  # 输出星级分层情况

print(  # 打印 rating bucket 统计

    annotation_reviews  # 使用标注 Review

    .groupby(["rating_bucket"])  # 按抽样分层

    .size()  # 统计数量

    .reset_index(name="review_count")  # 转为 DataFrame

    .to_string(index=False)  # 输出

)  # 打印结束


print(f"\nTaxonomy：{taxonomy_output}")  # 输出 Taxonomy 文件位置

print(f"Review 标注集：{annotation_reviews_output}")  # 输出人工 Review 文件

print(f"Label 模板：{label_output}")  # 输出标签模板

print(f"抽样统计：{sampling_summary_output}")  # 输出抽样统计

print(f"标注规则：{guideline_output}")  # 输出人工标注规则
