import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Select,
  Space,
  Typography,
} from "antd";
import { useMutation } from "@tanstack/react-query";
import { request, type Product } from "./api";

type SearchResult = {
  retrieval_id: string;
  limitations: string[];
  items: {
    fact_id: string;
    raw_row_id: string;
    record_id: string;
    business_date: string;
    title: string;
    text: string;
    revision: number;
  }[];
};

export default function EvidenceSearch({
  shopId,
  products,
}: {
  shopId: string;
  products: Product[];
}) {
  const [searched, setSearched] = useState(false);
  const search = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      request<SearchResult>("evidence-search/", "POST", {
        ...Object.fromEntries(
          Object.entries(values).filter(([, v]) => v !== "" && v !== undefined),
        ),
        shop: shopId,
      }),
    onSuccess: () => setSearched(true),
  });
  return (
    <Card title="查询评论与售后原文" style={{ marginBottom: 20 }}>
      <Form
        name="evidence-search"
        layout="vertical"
        initialValues={{ kind: "reviews" }}
        onFinish={(values) => search.mutate(values)}
      >
        <Space wrap align="start">
          <Form.Item
            name="child_asin"
            label="查询商品"
            rules={[{ required: true }]}
          >
            <Select
              style={{ width: 220 }}
              showSearch
              options={[...new Set(products.map((p) => p.child_asin))].map(
                (asin) => ({ value: asin, label: asin }),
              )}
            />
          </Form.Item>
          <Form.Item name="kind" label="证据来源">
            <Select
              style={{ width: 150 }}
              options={[
                { value: "reviews", label: "评论" },
                { value: "support", label: "售后工单" },
              ]}
            />
          </Form.Item>
          <Form.Item name="product" label="SKU范围">
            <Select
              allowClear
              style={{ width: 180 }}
              options={products.map((p) => ({
                value: p.id,
                label: `${p.child_asin} / ${p.seller_sku}`,
              }))}
            />
          </Form.Item>
          <Form.Item name="language" label="评论语言">
            <Input placeholder="如 en、zh" maxLength={20} />
          </Form.Item>
          <Form.Item name="topic" label="主题代码">
            <Input placeholder="如 stopped_working" maxLength={100} />
          </Form.Item>
          <Form.Item name="polarity" label="极性">
            <Select
              allowClear
              style={{ width: 120 }}
              options={[
                { value: "negative", label: "负向" },
                { value: "positive", label: "正向" },
                { value: "mixed", label: "混合" },
                { value: "neutral", label: "中性" },
              ]}
            />
          </Form.Item>
          <Form.Item name="start" label="开始日期" rules={[{ required: true }]}>
            <Input type="date" />
          </Form.Item>
          <Form.Item name="end" label="结束日期" rules={[{ required: true }]}>
            <Input type="date" />
          </Form.Item>
          <Form.Item
            name="query"
            label="问题词或原文短语"
            rules={[{ required: true }]}
          >
            <Input maxLength={200} placeholder="例如：充电慢" />
          </Form.Item>
        </Space>
        <Button type="primary" htmlType="submit" loading={search.isPending}>
          查询证据
        </Button>
      </Form>
      {search.error && (
        <Alert type="error" showIcon title={search.error.message} />
      )}
      {search.data && (
        <>
          <Typography.Paragraph style={{ marginTop: 16 }}>
            检索记录：{search.data.retrieval_id}
          </Typography.Paragraph>
          {search.data.items.map((item) => (
            <Card size="small" key={item.fact_id} style={{ marginBottom: 8 }}>
              <Typography.Text strong>
                {item.title || item.record_id}
              </Typography.Text>
              <Typography.Paragraph style={{ whiteSpace: "pre-wrap" }}>
                {item.text}
              </Typography.Paragraph>
              <Typography.Text type="secondary">
                {item.business_date} · 来源版本 {item.revision} · 原始行{" "}
                {item.raw_row_id}
              </Typography.Text>
            </Card>
          ))}
          {searched && !search.data.items.length && (
            <Alert
              type="info"
              title="当前范围没有命中；这不代表该问题不存在，可调整关键词或核对数据覆盖。"
            />
          )}
          {search.data.limitations.map((item) => (
            <Typography.Paragraph type="secondary" key={item}>
              {item}
            </Typography.Paragraph>
          ))}
        </>
      )}
    </Card>
  );
}
