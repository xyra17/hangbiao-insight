from pathlib import Path  # 导入 Path，用于处理文件和目录路径

import json  # 导入 json，用于保存冻结后的 Taxonomy

import pandas as pd  # 导入 pandas，用于读取和生成标注表


# ============================================================
# 1. 基础目录
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前脚本所在目录

INPUT_DIR = BASE_DIR / "outputs" / "05_标注集_v1"  # 06 阶段输出目录

ANNOTATION_REVIEW_FILE = INPUT_DIR / "02_标注评论集_v1.csv"  # 读取 360 条待标注评论

OUTPUT_DIR = BASE_DIR / "outputs" / "06_标注规范_v1_0"  # 定义冻结后的 V1.0 输出目录

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)  # 如果输出目录不存在则自动创建


# ============================================================
# 2. Long Format 每条评论预留多少个标签位置
# ============================================================

MAX_LABELS_PER_REVIEW = 4  # 每条评论预留 4 个 Topic 标签位置

# 为什么先设为 4：
# 大多数评论实际包含 1～3 个独立业务语义。
# 预留 4 个位置基本足够，同时避免人工标注时反复复制行。
# 如果后面真的遇到 >4 个标签的评论，再单独扩展即可。


# ============================================================
# 3. 冻结 Taxonomy V1.0
# ============================================================

TAXONOMY_V1_0 = {

    "version": "taxonomy-v1.0",  # 正式版本号

    "status": "frozen_for_annotation_v1",  # 当前状态：用于第一轮正式人工标注

    "design": {

        "annotation_unit": "topic_claim",  # 一个标签对应评论中的一个明确语义片段

        "multi_label": True,  # 一条评论允许多个 Topic

        "topic_has_fixed_polarity": False,  # Topic 本身没有固定正负属性

        "polarity_is_semantic": True,  # Polarity 必须根据具体语义判断

        "rating_is_not_polarity": True,  # Amazon 星级不能直接作为正式 Polarity

        "evidence_span_required": True,  # 每个标签必须有原文 Evidence Span

        "causal_inference_forbidden": True,  # 评论标注阶段禁止做经营因果推断

    },

    "polarity": [

        "positive",  # 明确认可、成功、改善、满意

        "negative",  # 明确抱怨、失效、风险、不符合预期

        "neutral",  # 客观描述，没有明显正负态度

    ],

    "topics": {

        # ----------------------------------------------------
        # 一级主题 1：功能 / 性能
        # ----------------------------------------------------

        "function_performance": {

            "definition": "产品核心功能能否实现，以及实际性能表现是否符合预期。",

            "subtopics": {

                "charging_success": {
                    "definition": "设备是否能够成功开始或持续完成充电。",
                    "positive_examples": [
                        "charges my phone every time",
                        "works perfectly for charging",
                    ],
                    "negative_examples": [
                        "doesn't charge my phone",
                        "won't charge",
                    ],
                },

                "charging_speed": {
                    "definition": "充电速度快慢，以及是否符合用户对快充的预期。",
                    "positive_examples": [
                        "charges very fast",
                        "fast charging works great",
                    ],
                    "negative_examples": [
                        "charges extremely slow",
                        "not fast charging",
                    ],
                },

                "protection_effectiveness": {
                    "definition": "屏幕保护类产品是否真正起到防刮、防摔或保护屏幕作用。",
                    "positive_examples": [
                        "protected my screen when I dropped it",
                    ],
                    "negative_examples": [
                        "screen still cracked",
                    ],
                },

                "general_performance": {
                    "definition": "无法归入更具体子类的产品功能或性能表现。",
                    "positive_examples": [],
                    "negative_examples": [],
                },

            },

        },


        # ----------------------------------------------------
        # 一级主题 2：可靠性 / 耐用性
        # ----------------------------------------------------

        "reliability_durability": {

            "definition": "产品是否稳定工作，以及经过一段时间后是否失效、损坏或寿命过短。",

            "subtopics": {

                "stopped_working": {
                    "definition": "产品曾经正常工作，随后完全停止工作。",
                    "positive_examples": [],
                    "negative_examples": [
                        "stopped working after two months",
                        "worked for a while then died",
                    ],
                },

                "intermittent_failure": {
                    "definition": "产品时好时坏、连接不稳定或偶发失效。",
                    "positive_examples": [],
                    "negative_examples": [
                        "works only sometimes",
                        "keeps disconnecting",
                    ],
                },

                "short_lifespan": {
                    "definition": "产品使用寿命明显短于用户预期。",
                    "positive_examples": [],
                    "negative_examples": [
                        "only lasted a few weeks",
                    ],
                },

                "cracking_breakage": {
                    "definition": "产品出现开裂、碎裂、断裂等物理损坏。",
                    "positive_examples": [],
                    "negative_examples": [
                        "cracked after a few days",
                        "broke very easily",
                    ],
                },

                "wear_damage": {
                    "definition": "产品出现磨损、脱落或其他耐用性下降。",
                    "positive_examples": [],
                    "negative_examples": [],
                },

            },

        },


        # ----------------------------------------------------
        # 一级主题 3：兼容性 / 适配
        # ----------------------------------------------------

        "compatibility_fit": {

            "definition": "产品与设备、保护壳、接口、墙插或尺寸之间是否匹配。",

            "subtopics": {

                "device_compatibility": {
                    "definition": "产品是否兼容特定手机、平板、手表等设备型号。",
                    "positive_examples": [
                        "works with my iPhone",
                    ],
                    "negative_examples": [
                        "doesn't work with my phone",
                    ],
                },

                "case_compatibility": {
                    "definition": "产品在手机安装保护壳等配件后是否仍可正常使用。",
                    "positive_examples": [
                        "charges through my case",
                    ],
                    "negative_examples": [
                        "won't charge with the case on",
                    ],
                },

                "size_fit": {
                    "definition": "产品尺寸是否与目标设备或使用对象匹配。",
                    "positive_examples": [
                        "fits perfectly",
                    ],
                    "negative_examples": [
                        "too small",
                        "doesn't fit the screen",
                    ],
                },

                "port_fit": {
                    "definition": "线缆、插头或接口与设备端口之间是否适配。",
                    "positive_examples": [],
                    "negative_examples": [
                        "connector does not fit",
                    ],
                },

                "wall_socket_fit": {
                    "definition": "墙充插脚与墙壁插座之间是否牢固、松动或容易脱落。",
                    "positive_examples": [
                        "fits tightly in the outlet",
                    ],
                    "negative_examples": [
                        "falls out of the wall outlet",
                        "plug is too loose",
                    ],
                },

            },

        },


        # ----------------------------------------------------
        # 一级主题 4：安全风险
        # ----------------------------------------------------

        "safety_risk": {

            "definition": "使用产品过程中出现的温度、电气、物理伤害或其他安全风险。",

            "subtopics": {

                "overheating": {
                    "definition": "产品或设备出现明显异常发热。",
                    "positive_examples": [],
                    "negative_examples": [
                        "gets extremely hot",
                        "charger becomes very hot",
                    ],
                },

                "melting_fire": {
                    "definition": "产品出现融化、冒烟、烧焦或起火等严重安全问题。",
                    "positive_examples": [],
                    "negative_examples": [
                        "started melting",
                        "almost caught fire",
                    ],
                },

                "abnormal_noise": {
                    "definition": "产品出现异常电流声、滋滋声或其他不正常声音。",
                    "positive_examples": [],
                    "negative_examples": [
                        "makes a buzzing sound",
                    ],
                },

                "physical_injury_irritation": {
                    "definition": "产品导致扎手、割伤、皮肤刺激等身体不适或伤害风险。",
                    "positive_examples": [],
                    "negative_examples": [
                        "tiny glass pieces cut my finger",
                    ],
                },

            },

        },


        # ----------------------------------------------------
        # 一级主题 5：使用与安装
        # ----------------------------------------------------

        "usability_installation": {

            "definition": "产品的安装、定位、操作以及状态反馈是否清晰和方便。",

            "subtopics": {

                "positioning_alignment": {
                    "definition": "产品使用过程中是否需要反复调整位置才能正常工作。",
                    "positive_examples": [
                        "easy to position",
                    ],
                    "negative_examples": [
                        "have to place the phone exactly right",
                    ],
                },

                "installation_difficulty": {
                    "definition": "产品安装过程是否困难、复杂或容易失败。",
                    "positive_examples": [
                        "very easy to install",
                    ],
                    "negative_examples": [
                        "difficult to install",
                    ],
                },

                "bubbles_adhesion": {
                    "definition": "贴膜等产品是否出现气泡、翘边、不粘等问题。",
                    "positive_examples": [
                        "no bubbles",
                    ],
                    "negative_examples": [
                        "bubbles everywhere",
                        "edges don't stick",
                    ],
                },

                "ease_of_use": {
                    "definition": "日常使用是否简单、方便。",
                    "positive_examples": [
                        "easy to use",
                    ],
                    "negative_examples": [
                        "annoying to use",
                    ],
                },

                "status_indicator_feedback": {
                    "definition": "LED、指示灯或其他状态反馈是否能够清晰告诉用户设备状态。",
                    "positive_examples": [
                        "indicator clearly shows it is charging",
                    ],
                    "negative_examples": [
                        "can't tell whether it is charging",
                    ],
                },

                "installation_accessories": {
                    "definition": "安装模板、清洁片、湿巾等安装附件是否完整和好用。",
                    "positive_examples": [
                        "installation kit made it easy",
                    ],
                    "negative_examples": [
                        "cleaning wipe was completely dry",
                        "installation guide was missing",
                    ],
                },

            },

        },


        # ----------------------------------------------------
        # 一级主题 6：便利性 / 价值感
        # ----------------------------------------------------

        "convenience_value": {

            "definition": "产品带来的多设备能力、便携性、价格价值和便利体验。",

            "subtopics": {

                "dual_port_multi_device": {
                    "definition": "是否支持同时连接或服务多个设备。",
                    "positive_examples": [
                        "charges two devices at once",
                    ],
                    "negative_examples": [
                        "can't charge two devices together",
                    ],
                },

                "portability_size": {
                    "definition": "产品尺寸、重量和携带便利程度。",
                    "positive_examples": [
                        "small and easy to travel with",
                    ],
                    "negative_examples": [
                        "too bulky",
                    ],
                },

                "price_value": {
                    "definition": "用户认为产品价格与实际价值是否匹配。",
                    "positive_examples": [
                        "great value for the price",
                    ],
                    "negative_examples": [
                        "not worth the money",
                    ],
                },

                "general_convenience": {
                    "definition": "无法归入更具体子类的便利性体验。",
                    "positive_examples": [],
                    "negative_examples": [],
                },

            },

        },


        # ----------------------------------------------------
        # 一级主题 7：产品质量
        # ----------------------------------------------------

        "product_quality": {

            "definition": "产品材料、制造做工、出厂状态以及附属物料的质量。",

            "subtopics": {

                "material_build_quality": {
                    "definition": "材质、做工、结构质量是否良好。",
                    "positive_examples": [
                        "feels well made",
                    ],
                    "negative_examples": [
                        "feels cheap",
                        "poor build quality",
                    ],
                },

                "defective_on_arrival": {
                    "definition": "产品第一次使用或刚到货时就存在功能性缺陷。",
                    "positive_examples": [],
                    "negative_examples": [
                        "didn't work out of the box",
                    ],
                },

                "accessory_condition": {
                    "definition": "附带配件、包装内物料或安装工具本身的质量状态。",
                    "positive_examples": [],
                    "negative_examples": [
                        "included wipe was dried out",
                    ],
                },

            },

        },


        # ----------------------------------------------------
        # 一级主题 8：其他 / 无法判断
        # ----------------------------------------------------

        "other_unknown": {

            "definition": "评论信息不足或现有 Taxonomy 无法可靠覆盖。",

            "subtopics": {

                "insufficient_information": {
                    "definition": "评论只有笼统评价，没有足够语义判断具体 Topic。",
                    "positive_examples": [
                        "good",
                        "great",
                    ],
                    "negative_examples": [
                        "bad",
                        "terrible",
                    ],
                },

                "uncategorized": {
                    "definition": "评论表达了明确业务语义，但当前 Taxonomy 尚未覆盖。",
                    "positive_examples": [],
                    "negative_examples": [],
                },

            },

        },

    },

}  # Taxonomy 定义结束


# ============================================================
# 4. 保存冻结后的 Taxonomy
# ============================================================

taxonomy_output = OUTPUT_DIR / "01_主题分类体系_v1_0.json"  # 定义 Taxonomy 输出文件

with open(taxonomy_output, "w", encoding="utf-8") as f:  # 打开 JSON 文件

    json.dump(  # 保存冻结后的 Taxonomy

        TAXONOMY_V1_0,  # Taxonomy 数据

        f,  # 文件对象

        ensure_ascii=False,  # 保留中文

        indent=2,  # 使用两格缩进

    )  # JSON 保存完成


# ============================================================
# 5. 创建版本变更记录
# ============================================================

taxonomy_changes = [  # 定义 Draft → V1.0 的主要修改

    {
        "change_type": "rename",
        "old": "safety_thermal",
        "new": "safety_risk",
        "reason": "真实评论除过热外，还出现玻璃碎片、身体刺激等物理安全风险。",
    },

    {
        "change_type": "add_subtopic",
        "old": "",
        "new": "compatibility_fit / wall_socket_fit",
        "reason": "墙充存在插头在墙壁插座中过松、易脱落等独立问题。",
    },

    {
        "change_type": "add_subtopic",
        "old": "",
        "new": "safety_risk / physical_injury_irritation",
        "reason": "钢化膜评论出现碎玻璃扎手等身体伤害风险。",
    },

    {
        "change_type": "add_subtopic",
        "old": "",
        "new": "usability_installation / status_indicator_feedback",
        "reason": "无线充评论中存在 LED / 充电状态提示是否清晰的问题。",
    },

    {
        "change_type": "add_subtopic",
        "old": "",
        "new": "usability_installation / installation_accessories",
        "reason": "钢化膜评论出现安装模板、清洁片、湿巾等附件问题。",
    },

    {
        "change_type": "rule",
        "old": "rating ≈ polarity",
        "new": "rating only for sampling; semantic span determines polarity",
        "reason": "真实样本中存在高星负向问题和低星局部正向反馈。",
    },

    {
        "change_type": "rule",
        "old": "single label row",
        "new": "long-format multi-label",
        "reason": "同一评论可同时包含多个 Topic 和不同 Polarity。",
    },

]  # 修改记录结束


# ============================================================
# 6. 保存 Taxonomy 变更记录
# ============================================================

taxonomy_change_df = pd.DataFrame(taxonomy_changes)  # 转成 DataFrame

taxonomy_change_output = OUTPUT_DIR / "02_分类体系变更记录_v1_0.csv"  # 定义输出文件

taxonomy_change_df.to_csv(  # 保存变更记录

    taxonomy_change_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # 使用 Excel 兼容编码

)  # 保存完成


# ============================================================
# 7. 读取 360 条人工标注 Review
# ============================================================

print("正在读取 06 阶段人工标注 Review……")  # 输出当前步骤

reviews = pd.read_csv(ANNOTATION_REVIEW_FILE)  # 读取 360 条评论

print(f"读取完成，共 {len(reviews):,} 条评论。")  # 输出数据数量


# ============================================================
# 8. 检查必须字段
# ============================================================

required_columns = [  # 定义人工标注必须存在的字段

    "annotation_id",  # 标注样本 ID

    "review_id",  # 原始 Review ID

    "product_id",  # 商品 ID

    "product_name",  # 商品名称

    "created_at",  # 评论时间

    "rating",  # Amazon 星级

    "title",  # 评论标题

    "text",  # 评论正文

    "sample_scope",  # focus / background

    "window_signal",  # 重点窗口类型

    "window_month",  # 重点窗口月份

    "rating_bucket",  # 仅用于抽样的星级分层

]  # 字段定义结束


missing_columns = [  # 检查字段缺失

    column  # 返回缺失字段名

    for column in required_columns  # 遍历必须字段

    if column not in reviews.columns  # 判断是否不存在

]  # 缺失字段列表结束


if missing_columns:  # 如果发现缺失字段

    raise ValueError(  # 直接停止程序

        f"02_标注评论集_v1.csv 缺少字段：{missing_columns}"

    )


# ============================================================
# 9. 检查 Review ID 是否唯一
# ============================================================

duplicate_review_count = reviews["review_id"].duplicated().sum()  # 统计重复 Review ID


if duplicate_review_count > 0:  # 如果存在重复

    raise ValueError(  # 停止程序

        f"发现 {duplicate_review_count} 条重复 review_id，请先检查 06 抽样结果。"

    )


# ============================================================
# 10. 检查正文是否为空
# ============================================================

empty_text_count = (  # 统计空正文

    reviews["text"]  # 使用评论正文

    .fillna("")  # 空值转换为空字符串

    .astype(str)  # 统一转字符串

    .str.strip()  # 去除空格

    .eq("")  # 判断是否为空

    .sum()  # 求总数

)  # 空正文统计结束


if empty_text_count > 0:  # 如果存在空正文

    raise ValueError(  # 停止程序

        f"发现 {empty_text_count} 条空正文评论。"

    )


# ============================================================
# 11. 保存冻结后的 Review 标注主表
# ============================================================

frozen_review_output = OUTPUT_DIR / "03_标注评论集_v1_0.csv"  # 定义冻结 Review 文件

reviews.to_csv(  # 保存原始 Review 标注集

    frozen_review_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # 使用 UTF-8 BOM

)  # 保存完成


# ============================================================
# 12. 创建真正的 Long Format Multi-label 模板
# ============================================================

label_rows = []  # 用于保存所有 Long Format 标签行


for _, review in reviews.iterrows():  # 遍历每一条待标注评论

    for label_index in range(1, MAX_LABELS_PER_REVIEW + 1):  # 每条评论生成 4 个 Label Slot

        label_rows.append({  # 创建当前 Label Slot

            # ------------------------------------------------
            # Review 身份字段
            # ------------------------------------------------

            "annotation_id": review["annotation_id"],  # 人工样本 ID

            "review_id": review["review_id"],  # 原始 Review ID

            "product_id": review["product_id"],  # 商品 ID

            "product_name": review["product_name"],  # 商品名称


            # ------------------------------------------------
            # Review 上下文字段
            # ------------------------------------------------

            "rating": review["rating"],  # Amazon 星级，仅提供上下文

            "title": review["title"],  # 原始评论标题

            "text": review["text"],  # 原始评论正文

            "sample_scope": review["sample_scope"],  # focus / background

            "window_signal": review["window_signal"],  # 当前窗口信号

            "window_month": review["window_month"],  # 重点窗口月份

            "rating_bucket": review["rating_bucket"],  # 星级抽样层，不是正式 polarity


            # ------------------------------------------------
            # Multi-label Slot
            # ------------------------------------------------

            "label_index": label_index,  # 当前是该评论第几个 Topic 标签


            # ------------------------------------------------
            # 正式人工标注字段
            # ------------------------------------------------

            "primary_topic": "",  # 一级 Topic

            "secondary_topic": "",  # 二级 Topic

            "polarity": "",  # 当前 Topic 对应的真实语义 Polarity

            "evidence_span": "",  # 直接支持标签的原始文字片段


            # ------------------------------------------------
            # 标注质量字段
            # ------------------------------------------------

            "annotation_confidence": "",  # high / medium / low

            "needs_review": "",  # true / false

            "annotator": "",  # 标注者姓名或编号

            "annotation_status": "pending" if label_index == 1 else "unused",

            # 第一行默认 pending
            # 第 2～4 行默认 unused
            # 如果评论存在第二个 Topic，再把对应行改成 pending / completed


            # ------------------------------------------------
            # 边界与备注
            # ------------------------------------------------

            "notes": "",  # 用于记录边界问题、新主题等

        })  # 当前 Label Slot 创建完成


# ============================================================
# 13. 转成 DataFrame
# ============================================================

labels_long = pd.DataFrame(label_rows)  # 把所有 Label Slot 转成 DataFrame


# ============================================================
# 14. 保存 Long Format 标注模板
# ============================================================

labels_long_output = OUTPUT_DIR / "04_标注标签长表_v1_0.csv"  # 定义 Long Format 文件

labels_long.to_csv(  # 保存 Long Format

    labels_long_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # Excel 兼容编码

)  # 保存结束


# ============================================================
# 15. 生成 Topic Reference 表
# ============================================================

topic_rows = []  # 保存所有一级 Topic / 二级 Topic


for primary_topic, primary_info in TAXONOMY_V1_0["topics"].items():  # 遍历一级 Topic

    for secondary_topic, secondary_info in primary_info["subtopics"].items():  # 遍历二级 Topic

        topic_rows.append({  # 创建 Topic Reference 行

            "primary_topic": primary_topic,  # 一级 Topic

            "primary_definition": primary_info["definition"],  # 一级定义

            "secondary_topic": secondary_topic,  # 二级 Topic

            "secondary_definition": secondary_info["definition"],  # 二级定义

            "positive_examples": " | ".join(  # 合并正向示例

                secondary_info.get("positive_examples", [])  # 获取示例列表

            ),

            "negative_examples": " | ".join(  # 合并负向示例

                secondary_info.get("negative_examples", [])  # 获取示例列表

            ),

        })  # 当前 Topic Reference 创建完成


# ============================================================
# 16. 保存 Topic Reference
# ============================================================

topic_reference_df = pd.DataFrame(topic_rows)  # 转成 DataFrame

topic_reference_output = OUTPUT_DIR / "05_主题参考表_v1_0.csv"  # 定义输出文件

topic_reference_df.to_csv(  # 保存 Topic Reference

    topic_reference_output,  # 输出路径

    index=False,  # 不保存索引

    encoding="utf-8-sig",  # Excel 兼容编码

)  # 保存完成


# ============================================================
# 17. 生成人工标注规则 V1.0
# ============================================================

guideline_text = """
商品经营异常智能诊断 Agent
Review Topic Annotation Guideline V1.0

一、标注目标

本任务不是判断一条评论整体是好评还是差评。

真正的标注单位是：

Topic Claim = Topic + Polarity + Evidence Span

一条 Review 可以拥有多个 Topic Claim。


二、Rating 不等于 Polarity

Amazon 星级只提供上下文，不直接作为 Polarity。

例如：

4 星：
"Works well but charging is very slow."

应该标：

function_performance
charging_success
positive
"Works well"

以及：

function_performance
charging_speed
negative
"charging is very slow"


三、Multi-label

例如：

"Charges fast but gets extremely hot."

需要两个标签：

1.
function_performance
charging_speed
positive
"Charges fast"

2.
safety_risk
overheating
negative
"gets extremely hot"


四、Evidence Span

Evidence Span 必须来自评论原文。

要求：

1. 尽量短。
2. 必须能够独立支持当前标签。
3. 不得改写。
4. 不得加入评论中没有表达的原因。


五、Polarity

positive：
明确认可、成功、改善、满意、方便。

negative：
失效、风险、不满、不符合预期、明显缺陷。

neutral：
客观描述事实，没有明显价值判断。


六、Topic 与 Polarity 分离

charging_speed 不是负面 Topic。

例如：

"charges very quickly"
→ charging_speed / positive

"charges very slowly"
→ charging_speed / negative


七、Topic 优先级

优先使用最具体的二级 Topic。

例如：

"Stopped working after 3 weeks."

优先：

reliability_durability / stopped_working

不要只标：

function_performance / general_performance


八、可靠性与功能的区别

"Doesn't charge at all from the first use."

更适合：

function_performance / charging_success / negative

如果明确表示：

"Worked for two months and then stopped."

更适合：

reliability_durability / stopped_working / negative


九、安全风险独立标注

如果评论同时包含：

"charges well but gets hot"

必须同时保留：

charging_success / positive

overheating / negative


十、Return / Refund 不作为核心 Topic

例如：

"It stopped working so I returned it."

核心标签是：

reliability_durability / stopped_working

return 本身只是用户后续行为，不建立独立 Topic。


十一、信息不足

例如：

"Good."
"Bad."
"Great product."

没有具体业务语义时使用：

other_unknown / insufficient_information


十二、未覆盖的新主题

评论有明确语义，但 V1.0 没有合适标签时：

other_unknown / uncategorized

同时：

needs_review = true

并在 notes 中写清楚新问题。


十三、annotation_confidence

high：
标签边界明确，Evidence 直接支持。

medium：
基本能够判断，但存在相邻 Topic 边界。

low：
语义含糊、信息不足或 Taxonomy 可能需要调整。


十四、needs_review

true：
需要后续仲裁。

false：
当前标注者认为标签明确。


十五、Long Format 使用规则

每条 Review 已预留 4 个 Label Slot。

label_index = 1：
默认 annotation_status = pending。

label_index = 2～4：
默认 annotation_status = unused。

如果评论包含第二个 Topic：

把 label_index=2 改为 completed，并填写标签。

没有使用的 Slot 保持 unused。

完成的标签：

annotation_status = completed。


十六、因果红线

人工评论标签只表达：

“评论里说了什么”。

禁止标注：

“这个问题导致退款率上涨”
“这个问题导致销量下降”

经营指标和评论之间的关系属于后续 Diagnosis Case，
不属于 Review Topic 标签。


当前冻结版本：

taxonomy-v1.0
""".strip()  # 标注说明结束


# ============================================================
# 18. 保存 Annotation Guideline
# ============================================================

guideline_output = OUTPUT_DIR / "06_标注指南_v1_0.txt"  # 定义规则文件

with open(guideline_output, "w", encoding="utf-8") as f:  # 打开文件

    f.write(guideline_text)  # 写入标注规则


# ============================================================
# 19. 生成 Manifest
# ============================================================

manifest = {  # 创建本轮人工标注数据 Manifest

    "taxonomy_version": "taxonomy-v1.0",  # Taxonomy 版本

    "review_set_version": "annotation-review-v1.0",  # Review 样本版本

    "label_schema_version": "annotation-label-long-v1.0",  # 标签 Schema 版本

    "review_count": int(len(reviews)),  # Review 数量

    "label_slots_per_review": MAX_LABELS_PER_REVIEW,  # 每条 Review 预留标签数

    "total_label_slots": int(len(labels_long)),  # 总 Label Slot 数量

    "multi_label": True,  # 是否允许多标签

    "rating_is_polarity": False,  # 星级不等于 Polarity

    "evidence_span_required": True,  # 是否必须 Evidence Span

}  # Manifest 创建结束


# ============================================================
# 20. 保存 Manifest
# ============================================================

manifest_output = OUTPUT_DIR / "07_标注清单_v1_0.json"  # 定义 Manifest 输出路径

with open(manifest_output, "w", encoding="utf-8") as f:  # 打开 Manifest

    json.dump(  # 保存 Manifest

        manifest,  # Manifest 数据

        f,  # 文件对象

        ensure_ascii=False,  # 保留中文

        indent=2,  # 两格缩进

    )  # 保存完成


# ============================================================
# 21. 控制台检查
# ============================================================

print("\n================ 06B 冻结完成 ================\n")  # 输出完成提示

print(f"Review 数量：{len(reviews):,}")  # 输出 Review 数

print(f"Review ID 唯一数：{reviews['review_id'].nunique():,}")  # 输出唯一 Review 数

print(f"一级 Topic 数量：{len(TAXONOMY_V1_0['topics'])}")  # 输出一级 Topic 数


secondary_topic_count = sum(  # 开始统计二级 Topic

    len(primary_info["subtopics"])  # 当前一级 Topic 的二级标签数量

    for primary_info in TAXONOMY_V1_0["topics"].values()  # 遍历所有一级 Topic

)  # 二级 Topic 统计结束


print(f"二级 Topic 数量：{secondary_topic_count}")  # 输出二级 Topic 数

print(f"每条 Review Label Slot：{MAX_LABELS_PER_REVIEW}")  # 输出预留标签数

print(f"Long Format 总行数：{len(labels_long):,}")  # 应该是 360 × 4 = 1440


print("\n输出文件：")  # 输出文件标题

print(f"Taxonomy V1.0：{taxonomy_output}")  # Taxonomy

print(f"变更记录：{taxonomy_change_output}")  # Change Log

print(f"冻结 Review：{frozen_review_output}")  # Review Set

print(f"Long Format Label：{labels_long_output}")  # Label Template

print(f"Topic Reference：{topic_reference_output}")  # Topic Reference

print(f"标注规则：{guideline_output}")  # Guideline

print(f"Manifest：{manifest_output}")  # Manifest
