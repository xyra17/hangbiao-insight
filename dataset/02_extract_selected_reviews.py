from pathlib import Path  # 导入 Path，用于处理输出目录和文件路径
from hashlib import sha1  # 导入 SHA1，用于生成稳定且唯一的 review_id
from datetime import datetime, timezone  # 导入时间工具，用于转换 Amazon 毫秒时间戳

import pandas as pd  # 导入 pandas，用于保存最终评论数据
from datasets import load_dataset  # 导入 Hugging Face datasets，用于流式读取 Amazon 评论
from tqdm import tqdm  # 导入 tqdm，用于显示扫描进度


# ============================================================
# 1. 基础目录
# ============================================================

BASE_DIR = Path(__file__).resolve().parent  # 获取当前 Python 文件所在目录

DATA_DIR = BASE_DIR / "data" / "01_标准化评论数据"  # 定义标准化评论数据输出目录

DATA_DIR.mkdir(parents=True, exist_ok=True)  # 如果 data 目录不存在，则自动创建


# ============================================================
# 2. Amazon 评论数据地址
# ============================================================

REVIEW_FILE = "hf://datasets/McAuley-Lab/Amazon-Reviews-2023/raw/review_categories/Cell_Phones_and_Accessories.jsonl"  # 定义原始评论文件地址


# ============================================================
# 3. 当前暂定的 5 个候选商品
# ============================================================

SELECTED_PRODUCTS = {  # 建立 parent_asin 与商品简称的映射
    "B0BYJ8ZVWH": "AILKIN_USB_Wall_Charger", # 墙充
    "B07P9S6R9Y": "Yootech_Wireless_Charger", # 无线充
    "B01G0X56YU": "WixGear_Car_Phone_Mount", # 车载支架
    "B0B1TCPH54": "JETech_Charging_Cable", # 数据线
    "B07H2V5YLH": "Ailun_Screen_Protector", # 屏幕保护膜
}  # 商品映射定义结束

PRODUCT_FILE_LABELS = {  # 文件名使用中文品类，数据字段仍保留稳定英文商品简称
    "B0BYJ8ZVWH": "AILKIN_USB墙充",
    "B07P9S6R9Y": "Yootech_无线充电器",
    "B01G0X56YU": "WixGear_车载手机支架",
    "B0B1TCPH54": "JETech_充电线",
    "B07H2V5YLH": "Ailun_屏幕保护膜",
}


# ============================================================
# 4. 生成稳定 review_id
# ============================================================

def build_review_id(row):  # 定义生成唯一 review_id 的函数

    parent_asin = str(row.get("parent_asin") or "")  # 获取商品主 ID

    asin = str(row.get("asin") or "")  # 获取具体商品变体 ASIN

    user_id = str(row.get("user_id") or "")  # 获取评论用户 ID

    timestamp = str(row.get("timestamp") or "")  # 获取评论时间戳

    text = str(row.get("text") or "")  # 获取评论正文

    raw_key = f"{parent_asin}|{asin}|{user_id}|{timestamp}|{text}"  # 把几个稳定字段拼成唯一字符串

    review_id = sha1(raw_key.encode("utf-8")).hexdigest()  # 对字符串计算 SHA1 哈希

    return review_id  # 返回最终稳定 review_id


# ============================================================
# 5. 时间戳转换函数
# ============================================================

def convert_timestamp(timestamp):  # 定义 Amazon 毫秒时间戳转换函数

    if timestamp is None:  # 如果原始时间为空

        return None  # 直接返回空值

    try:  # 尝试转换时间戳

        timestamp = int(timestamp)  # 把时间戳转换成整数

        dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)  # 从毫秒时间戳转换为 UTC 时间

        return dt.isoformat()  # 返回 ISO 8601 格式时间

    except (TypeError, ValueError, OverflowError):  # 捕获格式错误或非法时间

        return None  # 转换失败时返回空值


# ============================================================
# 6. 加载 Amazon 评论
# ============================================================

print("正在连接 Amazon Reviews 2023……")  # 控制台提示正在连接数据源

reviews = load_dataset(  # 开始创建流式评论数据集

    "json",  # 指定原始文件格式为 JSONL

    data_files=REVIEW_FILE,  # 指定数据文件地址

    split="train",  # 把该文件作为 train split 读取

    streaming=True,  # 使用 streaming，避免加载 2000 多万条数据到内存

)  # 数据集初始化结束

print("评论数据连接成功。")  # 提示数据已经成功连接


# ============================================================
# 7. 准备数据容器
# ============================================================

selected_reviews = []  # 保存最终筛选出来的评论

seen_review_ids = set()  # 保存已经出现过的 review_id，用于去重

total_rows = 0  # 统计总共扫描了多少条原始评论

selected_rows = 0  # 统计最终抽取了多少条评论

empty_text_rows = 0  # 统计因为正文为空而删除的评论

duplicate_rows = 0  # 统计重复评论数量


# ============================================================
# 8. 扫描全部评论，只抽取目标商品
# ============================================================

print("\n开始抽取目标商品评论……")  # 提示开始正式扫描

for row in tqdm(reviews, desc="抽取评论"):  # 逐条扫描 Amazon 评论

    total_rows += 1  # 原始评论扫描数量加 1

    parent_asin = row.get("parent_asin")  # 获取当前评论所属 parent_asin

    if parent_asin not in SELECTED_PRODUCTS:  # 判断是否属于我们的 5 个目标商品

        continue  # 不是目标商品则立即跳过

    text = row.get("text")  # 获取评论正文

    if text is None or not str(text).strip():  # 判断正文是否为空

        empty_text_rows += 1  # 空正文数量加 1

        continue  # 不保存没有正文的评论

    review_id = build_review_id(row)  # 为当前评论生成稳定 review_id

    if review_id in seen_review_ids:  # 判断该评论是否已经出现过

        duplicate_rows += 1  # 重复评论计数加 1

        continue  # 重复评论直接跳过

    seen_review_ids.add(review_id)  # 将当前 review_id 加入去重集合

    created_at = convert_timestamp(row.get("timestamp"))  # 把原始毫秒时间戳转换为标准时间

    rating = row.get("rating")  # 获取评论评分

    title = row.get("title")  # 获取评论标题

    asin = row.get("asin")  # 获取具体 ASIN

    user_id = row.get("user_id")  # 获取匿名用户 ID

    helpful_vote = row.get("helpful_vote")  # 获取 Helpful Vote 数量

    verified_purchase = row.get("verified_purchase")  # 获取是否为验证购买

    product_name = SELECTED_PRODUCTS[parent_asin]  # 获取我们给商品定义的简称

    selected_reviews.append({  # 构造我们自己的标准 Review Schema

        "review_id": review_id,  # 保存稳定评论 ID

        "sku": parent_asin,  # 使用 parent_asin 作为当前项目里的 SKU 主键

        "source_asin": asin,  # 保存 Amazon 原始 ASIN

        "product_name": product_name,  # 保存商品可读名称

        "rating": rating,  # 保存评分

        "title": title,  # 保存评论标题

        "text": str(text).strip(),  # 保存清理前后空格后的正文

        "created_at": created_at,  # 保存标准化评论时间

        "user_id": user_id,  # 保存匿名用户 ID

        "helpful_vote": helpful_vote,  # 保存 Helpful Vote

        "verified_purchase": verified_purchase,  # 保存验证购买状态

        "source": "amazon_reviews_2023",  # 标记当前评论的数据来源

        "category": "Cell_Phones_and_Accessories",  # 标记 Amazon 数据品类

    })  # 当前评论结构构造结束

    selected_rows += 1  # 成功抽取评论数量加 1


# ============================================================
# 9. 转换为 DataFrame
# ============================================================

print("\n开始整理 Review Dataset V1……")  # 提示开始整理最终数据

df = pd.DataFrame(selected_reviews)  # 把评论列表转换成 pandas DataFrame


# ============================================================
# 10. 时间字段转换
# ============================================================

df["created_at"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")  # 把 created_at 转换成 pandas 时间类型


# ============================================================
# 11. 删除时间异常记录
# ============================================================

invalid_date_count = int(df["created_at"].isna().sum())  # 统计无法解析时间的评论数量

df = df.dropna(subset=["created_at"]).copy()  # 删除没有有效评论时间的记录


# ============================================================
# 12. 按商品和时间排序
# ============================================================

df = df.sort_values(["sku", "created_at"]).reset_index(drop=True)  # 按商品和评论时间排序并重新生成索引


# ============================================================
# 13. 保存完整 Review Dataset V1
# ============================================================

combined_csv = DATA_DIR / "评论数据集_v1.csv"  # 定义完整 CSV 输出路径

combined_parquet = DATA_DIR / "评论数据集_v1.parquet"  # 定义完整 Parquet 输出路径

df.to_csv(combined_csv, index=False, encoding="utf-8-sig")  # 保存 CSV，方便直接查看

df.to_parquet(combined_parquet, index=False)  # 保存 Parquet，方便后续 Python 和数据分析快速读取


# ============================================================
# 14. 分商品保存评论
# ============================================================

for sku, product_name in SELECTED_PRODUCTS.items():  # 遍历 5 个候选商品

    sku_df = df[df["sku"] == sku].copy()  # 筛出当前 SKU 的所有评论

    sku_output = DATA_DIR / f"{sku}_{PRODUCT_FILE_LABELS[sku]}.csv"  # 定义当前商品单独 CSV 的文件名

    sku_df.to_csv(sku_output, index=False, encoding="utf-8-sig")  # 保存当前商品的独立评论数据


# ============================================================
# 15. 生成商品级基础统计
# ============================================================

summary_df = df.groupby(["sku", "product_name"]).agg(  # 按商品聚合评论数据

    review_count=("review_id", "count"),  # 统计每个商品最终有效评论数量

    avg_rating=("rating", "mean"),  # 计算每个商品平均评分

    first_review=("created_at", "min"),  # 获取最早评论时间

    last_review=("created_at", "max"),  # 获取最晚评论时间

    verified_ratio=("verified_purchase", "mean"),  # 计算验证购买评论比例

).reset_index()  # 把商品重新变成普通字段


# ============================================================
# 16. 增加评分分布
# ============================================================

for rating_value in [1, 2, 3, 4, 5]:  # 遍历 1～5 星

    rating_counts = df[df["rating"] == rating_value].groupby("sku").size()  # 统计每个商品对应星级评论数量

    summary_df[f"rating_{rating_value}_count"] = summary_df["sku"].map(rating_counts).fillna(0).astype(int)  # 把星级数量加入商品统计表


# ============================================================
# 17. 保存商品基础统计
# ============================================================

summary_output = DATA_DIR / "评论数据集汇总_v1.csv"  # 定义商品统计表保存路径

summary_df.to_csv(summary_output, index=False, encoding="utf-8-sig")  # 保存商品级基础统计


# ============================================================
# 18. 输出运行结果
# ============================================================

print("\n================ 抽取完成 ================\n")  # 打印完成标题

print(f"扫描原始评论：{total_rows:,}")  # 输出扫描评论总数

print(f"抽取目标评论：{selected_rows:,}")  # 输出成功抽取数量

print(f"删除空正文：{empty_text_rows:,}")  # 输出空正文删除数量

print(f"删除重复评论：{duplicate_rows:,}")  # 输出重复评论数量

print(f"删除异常时间：{invalid_date_count:,}")  # 输出异常时间记录数量

print(f"最终 Review Dataset：{len(df):,} 条")  # 输出最终数据集规模

print("\n各商品数据量：")  # 提示接下来输出商品统计

print(summary_df.to_string(index=False))  # 在控制台展示各 SKU 基础情况

print(f"\n完整 CSV：{combined_csv}")  # 输出完整 CSV 地址

print(f"完整 Parquet：{combined_parquet}")  # 输出完整 Parquet 地址

print(f"统计结果：{summary_output}")  # 输出商品统计文件地址
