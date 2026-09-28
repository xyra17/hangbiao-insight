from pathlib import Path  # 导入 Path，用于处理目录和文件路径

import re  # 导入正则表达式，用于匹配评论文本

import pandas as pd  # 导入 pandas，用于读取和生成标注数据


# ============================================================
# 1. 基础目录
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前脚本所在目录

INPUT_DIR = BASE_DIR / "outputs" / "06_标注规范_v1_0"  # 06B 阶段输出目录

REVIEW_FILE = INPUT_DIR / "03_标注评论集_v1_0.csv"  # 冻结的 360 条评论

OUTPUT_DIR = BASE_DIR / "outputs" / "07_黄金评论构建"  # 07 阶段输出目录

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 自动创建输出目录


# ============================================================
# 2. 每条评论最多生成多少个候选标签
# ============================================================

MAX_PRELABELS_PER_REVIEW = 4  # 与 06B 的 4 个 Label Slot 保持一致


# ============================================================
# 3. 规则说明
# ============================================================

# 这里的规则只用于“候选预标”。
# 它们绝对不是最终 Ground Truth。
#
# 所有规则生成的标签都必须在后续人工复核后，
# 才能进入 Golden Set。


# ============================================================
# 4. Topic 候选规则
# ============================================================

TOPIC_RULES = [

    # --------------------------------------------------------
    # function_performance / charging_success
    # --------------------------------------------------------

    {
        "primary_topic": "function_performance",
        "secondary_topic": "charging_success",
        "polarity": "negative",
        "patterns": [
            r"\bdoesn['’]?t charge\b",
            r"\bdoes not charge\b",
            r"\bwon['’]?t charge\b",
            r"\bwill not charge\b",
            r"\bnot charging\b",
            r"\bwouldn['’]?t charge\b",
            r"\bnever charges\b",
            r"\bdoesn['’]?t work\b",
        ],
    },

    {
        "primary_topic": "function_performance",
        "secondary_topic": "charging_success",
        "polarity": "positive",
        "patterns": [
            r"\bcharges perfectly\b",
            r"\bcharges well\b",
            r"\bworks perfectly\b",
            r"\bworks great\b",
            r"\bworks well\b",
            r"\bcharges my phone\b",
        ],
    },


    # --------------------------------------------------------
    # function_performance / charging_speed
    # --------------------------------------------------------

    {
        "primary_topic": "function_performance",
        "secondary_topic": "charging_speed",
        "polarity": "negative",
        "patterns": [
            r"\bslow charg",
            r"\bcharges slowly\b",
            r"\bcharging slowly\b",
            r"\bnot fast charg",
            r"\btakes forever\b",
            r"\bvery slow\b",
        ],
    },

    {
        "primary_topic": "function_performance",
        "secondary_topic": "charging_speed",
        "polarity": "positive",
        "patterns": [
            r"\bfast charg",
            r"\bcharges fast\b",
            r"\bcharges quickly\b",
            r"\bcharging quickly\b",
            r"\bvery fast\b",
        ],
    },


    # --------------------------------------------------------
    # function_performance / protection_effectiveness
    # --------------------------------------------------------

    {
        "primary_topic": "function_performance",
        "secondary_topic": "protection_effectiveness",
        "polarity": "positive",
        "patterns": [
            r"\bprotected my screen\b",
            r"\bsaved my screen\b",
            r"\bprotects the screen\b",
            r"\bscreen didn['’]?t crack\b",
        ],
    },

    {
        "primary_topic": "function_performance",
        "secondary_topic": "protection_effectiveness",
        "polarity": "negative",
        "patterns": [
            r"\bdidn['’]?t protect\b",
            r"\bscreen still cracked\b",
            r"\bfailed to protect\b",
        ],
    },


    # --------------------------------------------------------
    # reliability_durability / stopped_working
    # --------------------------------------------------------

    {
        "primary_topic": "reliability_durability",
        "secondary_topic": "stopped_working",
        "polarity": "negative",
        "patterns": [
            r"\bstopped working\b",
            r"\bstop working\b",
            r"\bquit working\b",
            r"\bno longer works\b",
            r"\bdied after\b",
            r"\bworked for .* then\b",
        ],
    },


    # --------------------------------------------------------
    # reliability_durability / intermittent_failure
    # --------------------------------------------------------

    {
        "primary_topic": "reliability_durability",
        "secondary_topic": "intermittent_failure",
        "polarity": "negative",
        "patterns": [
            r"\bsometimes works\b",
            r"\bworks sometimes\b",
            r"\bworks intermittently\b",
            r"\bintermittent\b",
            r"\bon and off\b",
            r"\bkeeps disconnecting\b",
        ],
    },


    # --------------------------------------------------------
    # reliability_durability / short_lifespan
    # --------------------------------------------------------

    {
        "primary_topic": "reliability_durability",
        "secondary_topic": "short_lifespan",
        "polarity": "negative",
        "patterns": [
            r"\bonly lasted\b",
            r"\blasted .* days\b",
            r"\blasted .* weeks\b",
            r"\blasted .* months\b",
            r"\bafter a few days\b",
            r"\bafter a few weeks\b",
        ],
    },


    # --------------------------------------------------------
    # reliability_durability / cracking_breakage
    # --------------------------------------------------------

    {
        "primary_topic": "reliability_durability",
        "secondary_topic": "cracking_breakage",
        "polarity": "negative",
        "patterns": [
            r"\bcracked\b",
            r"\bcracks\b",
            r"\bbroke\b",
            r"\bbroken\b",
            r"\bshattered\b",
            r"\bbreaks easily\b",
        ],
    },


    # --------------------------------------------------------
    # compatibility_fit / device_compatibility
    # --------------------------------------------------------

    {
        "primary_topic": "compatibility_fit",
        "secondary_topic": "device_compatibility",
        "polarity": "negative",
        "patterns": [
            r"\bnot compatible\b",
            r"\bdoesn['’]?t work with my\b",
            r"\bdoes not work with my\b",
            r"\bwon['’]?t work with my\b",
        ],
    },

    {
        "primary_topic": "compatibility_fit",
        "secondary_topic": "device_compatibility",
        "polarity": "positive",
        "patterns": [
            r"\bworks with my\b",
            r"\bcompatible with\b",
        ],
    },


    # --------------------------------------------------------
    # compatibility_fit / case_compatibility
    # --------------------------------------------------------

    {
        "primary_topic": "compatibility_fit",
        "secondary_topic": "case_compatibility",
        "polarity": "negative",
        "patterns": [
            r"\bwon['’]?t charge with .* case\b",
            r"\bdoesn['’]?t charge with .* case\b",
            r"\bremove .* case\b",
            r"\bcase .* too thick\b",
        ],
    },

    {
        "primary_topic": "compatibility_fit",
        "secondary_topic": "case_compatibility",
        "polarity": "positive",
        "patterns": [
            r"\bcharges through .* case\b",
            r"\bworks through .* case\b",
            r"\bwith my case on\b",
        ],
    },


    # --------------------------------------------------------
    # compatibility_fit / size_fit
    # --------------------------------------------------------

    {
        "primary_topic": "compatibility_fit",
        "secondary_topic": "size_fit",
        "polarity": "negative",
        "patterns": [
            r"\btoo small\b",
            r"\btoo large\b",
            r"\btoo big\b",
            r"\bdoesn['’]?t fit\b",
            r"\bdoes not fit\b",
            r"\bnot fit\b",
        ],
    },

    {
        "primary_topic": "compatibility_fit",
        "secondary_topic": "size_fit",
        "polarity": "positive",
        "patterns": [
            r"\bfits perfectly\b",
            r"\bperfect fit\b",
            r"\bfits well\b",
        ],
    },


    # --------------------------------------------------------
    # compatibility_fit / wall_socket_fit
    # --------------------------------------------------------

    {
        "primary_topic": "compatibility_fit",
        "secondary_topic": "wall_socket_fit",
        "polarity": "negative",
        "patterns": [
            r"\bfalls out .* outlet\b",
            r"\bfalls out .* socket\b",
            r"\bloose in .* outlet\b",
            r"\bloose in .* socket\b",
            r"\bplug .* loose\b",
        ],
    },


    # --------------------------------------------------------
    # safety_risk / overheating
    # --------------------------------------------------------

    {
        "primary_topic": "safety_risk",
        "secondary_topic": "overheating",
        "polarity": "negative",
        "patterns": [
            r"\bgets hot\b",
            r"\bgets very hot\b",
            r"\bgets extremely hot\b",
            r"\boverheat",
            r"\btoo hot\b",
            r"\bvery hot\b",
        ],
    },


    # --------------------------------------------------------
    # safety_risk / melting_fire
    # --------------------------------------------------------

    {
        "primary_topic": "safety_risk",
        "secondary_topic": "melting_fire",
        "polarity": "negative",
        "patterns": [
            r"\bmelting\b",
            r"\bmelted\b",
            r"\bcaught fire\b",
            r"\bcatch fire\b",
            r"\bsmoke\b",
            r"\bburned\b",
            r"\bburning smell\b",
        ],
    },


    # --------------------------------------------------------
    # safety_risk / abnormal_noise
    # --------------------------------------------------------

    {
        "primary_topic": "safety_risk",
        "secondary_topic": "abnormal_noise",
        "polarity": "negative",
        "patterns": [
            r"\bbuzzing\b",
            r"\bhumming\b",
            r"\bnoise\b",
            r"\bstrange sound\b",
            r"\bweird sound\b",
        ],
    },


    # --------------------------------------------------------
    # safety_risk / physical_injury_irritation
    # --------------------------------------------------------

    {
        "primary_topic": "safety_risk",
        "secondary_topic": "physical_injury_irritation",
        "polarity": "negative",
        "patterns": [
            r"\bcut my\b",
            r"\bcut me\b",
            r"\bsharp glass\b",
            r"\bglass pieces\b",
            r"\bglass shards\b",
            r"\birritat",
        ],
    },


    # --------------------------------------------------------
    # usability_installation / positioning_alignment
    # --------------------------------------------------------

    {
        "primary_topic": "usability_installation",
        "secondary_topic": "positioning_alignment",
        "polarity": "negative",
        "patterns": [
            r"\bexact position\b",
            r"\bexactly right\b",
            r"\breposition\b",
            r"\bmove the phone\b",
            r"\bline it up\b",
            r"\balign .* phone\b",
        ],
    },


    # --------------------------------------------------------
    # usability_installation / installation_difficulty
    # --------------------------------------------------------

    {
        "primary_topic": "usability_installation",
        "secondary_topic": "installation_difficulty",
        "polarity": "negative",
        "patterns": [
            r"\bhard to install\b",
            r"\bdifficult to install\b",
            r"\binstallation .* difficult\b",
            r"\binstallation .* hard\b",
        ],
    },

    {
        "primary_topic": "usability_installation",
        "secondary_topic": "installation_difficulty",
        "polarity": "positive",
        "patterns": [
            r"\beasy to install\b",
            r"\bsimple to install\b",
            r"\binstallation was easy\b",
        ],
    },


    # --------------------------------------------------------
    # usability_installation / bubbles_adhesion
    # --------------------------------------------------------

    {
        "primary_topic": "usability_installation",
        "secondary_topic": "bubbles_adhesion",
        "polarity": "negative",
        "patterns": [
            r"\bair bubble",
            r"\bbubbles\b",
            r"\bwon['’]?t stick\b",
            r"\bdoesn['’]?t stick\b",
            r"\bedges .* lift\b",
            r"\bedges .* peel\b",
        ],
    },

    {
        "primary_topic": "usability_installation",
        "secondary_topic": "bubbles_adhesion",
        "polarity": "positive",
        "patterns": [
            r"\bno bubbles\b",
            r"\bwithout bubbles\b",
            r"\bsticks well\b",
        ],
    },


    # --------------------------------------------------------
    # usability_installation / ease_of_use
    # --------------------------------------------------------

    {
        "primary_topic": "usability_installation",
        "secondary_topic": "ease_of_use",
        "polarity": "positive",
        "patterns": [
            r"\beasy to use\b",
            r"\bvery easy to use\b",
            r"\bsimple to use\b",
        ],
    },

    {
        "primary_topic": "usability_installation",
        "secondary_topic": "ease_of_use",
        "polarity": "negative",
        "patterns": [
            r"\bhard to use\b",
            r"\bdifficult to use\b",
            r"\bannoying to use\b",
        ],
    },


    # --------------------------------------------------------
    # usability_installation / status_indicator_feedback
    # --------------------------------------------------------

    {
        "primary_topic": "usability_installation",
        "secondary_topic": "status_indicator_feedback",
        "polarity": "negative",
        "patterns": [
            r"\blight .* too bright\b",
            r"\bled .* too bright\b",
            r"\bcan['’]?t tell .* charging\b",
            r"\bno indicator\b",
        ],
    },


    # --------------------------------------------------------
    # usability_installation / installation_accessories
    # --------------------------------------------------------

    {
        "primary_topic": "usability_installation",
        "secondary_topic": "installation_accessories",
        "polarity": "negative",
        "patterns": [
            r"\bwipe .* dry\b",
            r"\bdry wipe\b",
            r"\bmissing .* guide\b",
            r"\bmissing .* kit\b",
            r"\binstallation kit .* missing\b",
        ],
    },


    # --------------------------------------------------------
    # convenience_value / dual_port_multi_device
    # --------------------------------------------------------

    {
        "primary_topic": "convenience_value",
        "secondary_topic": "dual_port_multi_device",
        "polarity": "positive",
        "patterns": [
            r"\btwo devices\b",
            r"\bcharge two\b",
            r"\bcharges two\b",
            r"\bdual port\b",
            r"\bboth devices\b",
        ],
    },


    # --------------------------------------------------------
    # convenience_value / portability_size
    # --------------------------------------------------------

    {
        "primary_topic": "convenience_value",
        "secondary_topic": "portability_size",
        "polarity": "positive",
        "patterns": [
            r"\bcompact\b",
            r"\bsmall and\b",
            r"\beasy to travel\b",
            r"\bgreat for travel\b",
        ],
    },

    {
        "primary_topic": "convenience_value",
        "secondary_topic": "portability_size",
        "polarity": "negative",
        "patterns": [
            r"\btoo bulky\b",
            r"\btoo large\b",
            r"\bheavy\b",
        ],
    },


    # --------------------------------------------------------
    # convenience_value / price_value
    # --------------------------------------------------------

    {
        "primary_topic": "convenience_value",
        "secondary_topic": "price_value",
        "polarity": "positive",
        "patterns": [
            r"\bgreat value\b",
            r"\bgood value\b",
            r"\bworth the price\b",
            r"\bfor the price\b",
        ],
    },

    {
        "primary_topic": "convenience_value",
        "secondary_topic": "price_value",
        "polarity": "negative",
        "patterns": [
            r"\bnot worth\b",
            r"\bwaste of money\b",
            r"\boverpriced\b",
        ],
    },


    # --------------------------------------------------------
    # product_quality / material_build_quality
    # --------------------------------------------------------

    {
        "primary_topic": "product_quality",
        "secondary_topic": "material_build_quality",
        "polarity": "positive",
        "patterns": [
            r"\bgood quality\b",
            r"\bwell made\b",
            r"\bsolid build\b",
        ],
    },

    {
        "primary_topic": "product_quality",
        "secondary_topic": "material_build_quality",
        "polarity": "negative",
        "patterns": [
            r"\bpoor quality\b",
            r"\bbad quality\b",
            r"\bcheap quality\b",
            r"\bfeels cheap\b",
            r"\bpoorly made\b",
        ],
    },


    # --------------------------------------------------------
    # product_quality / defective_on_arrival
    # --------------------------------------------------------

    {
        "primary_topic": "product_quality",
        "secondary_topic": "defective_on_arrival",
        "polarity": "negative",
        "patterns": [
            r"\bout of the box\b",
            r"\bdead on arrival\b",
            r"\barrived broken\b",
            r"\barrived damaged\b",
            r"\bnever worked\b",
        ],
    },

]  # Topic 规则定义结束


# ============================================================
# 5. 读取冻结 Review
# ============================================================

print("正在读取冻结 Review 标注集……")  # 输出步骤提示

reviews = pd.read_csv(REVIEW_FILE)  # 读取 360 条冻结 Review

print(f"读取完成，共 {len(reviews):,} 条评论。")  # 输出数量


# ============================================================
# 6. 处理标题和正文
# ============================================================

reviews["title"] = reviews["title"].fillna("").astype(str)  # 标题空值转为空字符串

reviews["text"] = reviews["text"].fillna("").astype(str)  # 正文空值转为空字符串

reviews["full_text"] = (  # 合并标题与正文

    reviews["title"]  # 使用标题

    + " "  # 中间增加空格

    + reviews["text"]  # 拼接正文

).str.strip()  # 去掉首尾空格


# ============================================================
# 7. 从原文中寻找 Evidence Span
# ============================================================

def find_pattern_match(text, patterns):  # 定义从原文查找关键词的函数

    for pattern in patterns:  # 逐个检查当前规则里的正则

        match = re.search(  # 尝试匹配

            pattern,  # 当前正则表达式

            text,  # 评论原文

            flags=re.IGNORECASE,  # 忽略英文大小写

        )  # 匹配结束


        if match:  # 如果成功匹配

            return match.group(0)  # 直接返回原始文本中的匹配片段


    return None  # 如果所有 pattern 都没有命中则返回 None


# ============================================================
# 8. 对单条 Review 生成候选标签
# ============================================================

def generate_prelabels(review):  # 定义候选标签函数

    text = review["full_text"]  # 获取标题 + 正文

    candidates = []  # 创建当前评论候选标签列表

    seen_labels = set()  # 防止同一个 Topic/Polarity 重复出现


    for rule in TOPIC_RULES:  # 遍历全部候选规则

        evidence_span = find_pattern_match(  # 尝试寻找原文 Evidence

            text,  # 评论原文

            rule["patterns"],  # 当前规则 Patterns

        )  # 匹配结束


        if evidence_span is None:  # 如果当前规则没有命中

            continue  # 继续检查下一条规则


        label_key = (  # 创建标签唯一键

            rule["primary_topic"],  # 一级 Topic

            rule["secondary_topic"],  # 二级 Topic

            rule["polarity"],  # Polarity

        )  # 唯一键结束


        if label_key in seen_labels:  # 如果该标签已经添加过

            continue  # 避免重复添加


        seen_labels.add(label_key)  # 记录该标签已经出现


        candidates.append({  # 保存候选标签

            "primary_topic": rule["primary_topic"],  # 一级 Topic

            "secondary_topic": rule["secondary_topic"],  # 二级 Topic

            "polarity": rule["polarity"],  # 规则建议 Polarity

            "evidence_span": evidence_span,  # 原始匹配文字

            "prelabel_method": "rule_v1",  # 标记候选来源

        })  # 当前候选标签结束


    return candidates[:MAX_PRELABELS_PER_REVIEW]  # 最多返回四个候选标签


# ============================================================
# 9. 保存候选标签
# ============================================================

prelabel_rows = []  # 用于保存所有预标结果

queue_rows = []  # 用于保存 Review 级别复核队列


for _, review in reviews.iterrows():  # 遍历全部 360 条评论

    candidates = generate_prelabels(review)  # 获取规则候选标签


    if len(candidates) == 0:  # 如果完全没有候选 Topic

        queue_type = "no_candidate"  # 标记为需要人工从零判断

    elif len(candidates) == 1:  # 如果只有一个候选

        queue_type = "single_candidate"  # 相对简单

    else:  # 如果出现两个或更多候选

        queue_type = "multi_candidate"  # 需要检查 Multi-label 是否准确


    queue_rows.append({  # 保存 Review 级别信息

        "annotation_id": review["annotation_id"],  # Annotation ID

        "review_id": review["review_id"],  # Review ID

        "product_id": review["product_id"],  # 商品 ID

        "product_name": review["product_name"],  # 商品名称

        "rating": review["rating"],  # Amazon 星级

        "title": review["title"],  # 原始标题

        "text": review["text"],  # 原始正文

        "sample_scope": review["sample_scope"],  # focus / background

        "window_signal": review["window_signal"],  # 窗口类型

        "rating_bucket": review["rating_bucket"],  # 星级抽样 Bucket

        "candidate_count": len(candidates),  # 当前候选标签数量

        "review_queue": queue_type,  # 当前 Review 应进入什么复核队列

    })  # Review Queue 保存完成


    for label_index in range(1, MAX_PRELABELS_PER_REVIEW + 1):  # 创建 4 个 Label Slot

        if label_index <= len(candidates):  # 如果当前 Slot 有候选标签

            candidate = candidates[label_index - 1]  # 获取对应候选标签


            prelabel_rows.append({  # 保存有候选的 Slot

                "annotation_id": review["annotation_id"],

                "review_id": review["review_id"],

                "product_id": review["product_id"],

                "product_name": review["product_name"],

                "rating": review["rating"],

                "title": review["title"],

                "text": review["text"],

                "sample_scope": review["sample_scope"],

                "window_signal": review["window_signal"],

                "rating_bucket": review["rating_bucket"],

                "label_index": label_index,

                "primary_topic": candidate["primary_topic"],

                "secondary_topic": candidate["secondary_topic"],

                "polarity": candidate["polarity"],

                "evidence_span": candidate["evidence_span"],

                "prelabel_method": candidate["prelabel_method"],

                "annotation_confidence": "",

                "needs_review": True,

                "annotator": "",

                "annotation_status": "prelabel_only",

                "review_decision": "",

                "notes": "",

            })  # 候选 Slot 保存结束


        else:  # 如果当前 Slot 没有候选标签

            prelabel_rows.append({  # 保存空 Slot

                "annotation_id": review["annotation_id"],

                "review_id": review["review_id"],

                "product_id": review["product_id"],

                "product_name": review["product_name"],

                "rating": review["rating"],

                "title": review["title"],

                "text": review["text"],

                "sample_scope": review["sample_scope"],

                "window_signal": review["window_signal"],

                "rating_bucket": review["rating_bucket"],

                "label_index": label_index,

                "primary_topic": "",

                "secondary_topic": "",

                "polarity": "",

                "evidence_span": "",

                "prelabel_method": "",

                "annotation_confidence": "",

                "needs_review": "",

                "annotator": "",

                "annotation_status": "unused",

                "review_decision": "",

                "notes": "",

            })  # 空 Slot 保存结束


# ============================================================
# 10. 创建 DataFrame
# ============================================================

prelabels_df = pd.DataFrame(prelabel_rows)  # 创建 Long Format 预标数据

queue_df = pd.DataFrame(queue_rows)  # 创建 Review Queue 数据


# ============================================================
# 11. 保存 Long Format 预标结果
# ============================================================

prelabels_output = OUTPUT_DIR / "01_规则预标长表_v1.csv"  # 定义预标文件路径

prelabels_df.to_csv(  # 保存 Long Format 预标数据

    prelabels_output,  # 输出路径

    index=False,  # 不保存 DataFrame 索引

    encoding="utf-8-sig",  # Excel 兼容编码

)  # 保存完成


# ============================================================
# 12. 保存 Review 复核队列
# ============================================================

queue_output = OUTPUT_DIR / "02_人工复核队列_v1.csv"  # 定义 Review Queue 文件路径

queue_df.to_csv(  # 保存 Review Queue

    queue_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # Excel 兼容编码

)  # 保存结束


# ============================================================
# 13. 生成候选 Topic 统计
# ============================================================

matched_prelabels = prelabels_df[  # 只保留实际存在候选标签的行

    prelabels_df["annotation_status"] == "prelabel_only"

].copy()  # 创建副本


if not matched_prelabels.empty:  # 如果存在候选标签

    topic_summary = (  # 开始统计 Topic 数量

        matched_prelabels  # 使用候选标签

        .groupby(  # 按 Topic + Polarity 汇总

            [

                "primary_topic",

                "secondary_topic",

                "polarity",

            ]

        )

        .size()  # 统计出现次数

        .reset_index(name="candidate_count")  # 转成 DataFrame

        .sort_values(  # 按候选数量倒序

            "candidate_count",

            ascending=False,

        )

    )  # Topic 汇总完成

else:  # 如果一个候选都没有

    topic_summary = pd.DataFrame()  # 创建空表


# ============================================================
# 14. 保存候选 Topic 统计
# ============================================================

topic_summary_output = OUTPUT_DIR / "03_预标主题汇总_v1.csv"  # 定义统计输出路径

topic_summary.to_csv(  # 保存 Topic 候选统计

    topic_summary_output,

    index=False,

    encoding="utf-8-sig",

)  # 保存结束


# ============================================================
# 15. 生成 Review Queue 统计
# ============================================================

queue_summary = (  # 统计不同 Review Queue

    queue_df

    .groupby("review_queue")

    .size()

    .reset_index(name="review_count")

)  # Queue 汇总结束


queue_summary_output = OUTPUT_DIR / "04_复核队列汇总_v1.csv"  # 定义 Queue 汇总文件

queue_summary.to_csv(  # 保存 Queue 统计

    queue_summary_output,

    index=False,

    encoding="utf-8-sig",

)  # 保存结束


# ============================================================
# 16. 计算规则覆盖率
# ============================================================

matched_review_count = (  # 统计至少命中一个候选标签的 Review 数量

    queue_df["candidate_count"]

    .gt(0)

    .sum()

)  # 命中数量统计结束


coverage = matched_review_count / len(queue_df)  # 计算规则覆盖率


# ============================================================
# 17. 控制台输出
# ============================================================

print("\n================ 07A 预标完成 ================\n")

print(f"Review 总数：{len(queue_df):,}")

print(f"至少命中 1 个候选标签：{matched_review_count:,}")

print(f"规则覆盖率：{coverage:.2%}")

print(f"候选 Topic Claim 总数：{len(matched_prelabels):,}")


print("\nReview 复核队列：")

print(queue_summary.to_string(index=False))


print("\n候选 Topic 分布：")

if topic_summary.empty:

    print("暂无规则候选结果。")

else:

    print(topic_summary.to_string(index=False))


print("\n输出文件：")

print(f"候选 Label：{prelabels_output}")

print(f"Review Queue：{queue_output}")

print(f"Topic 统计：{topic_summary_output}")

print(f"Queue 统计：{queue_summary_output}")


# ============================================================
# 18. 最后提醒
# ============================================================

print(
    "\n注意：以上全部属于 PRELABEL，"
    "不能直接作为 Golden Ground Truth。"
)

print(
    "下一步必须进行 Review 级复核，"
    "确认 Topic、Polarity 和 Evidence Span。"
)
