from datasets import load_dataset

review_file = (
    "hf://datasets/McAuley-Lab/Amazon-Reviews-2023/"
    "raw/review_categories/Appliances.jsonl"
)

reviews = load_dataset(
    "json",
    data_files=review_file,
    split="train",
    streaming=True
)

print("数据连接成功！")

for i, row in enumerate(reviews.take(3)):
    print(f"\n===== 第 {i + 1} 条 =====")
    print(row)
    print("字段：", row.keys())