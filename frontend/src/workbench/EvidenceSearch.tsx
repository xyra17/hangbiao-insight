import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Form,
  Input,
  Select,
  Tag,
  Typography,
} from "antd";
import { useMutation } from "@tanstack/react-query";
import { request, type Product } from "./api";

type SearchResult = {
  retrieval_id: string;
  match_status?: string;
  retrieval_complete?: boolean;
  relevance_judgment?: {
    enabled?: boolean;
    complete?: boolean;
    status?: string;
  };
  limitations: string[];
  items: {
    fact_id: string;
    raw_row_id: string;
    record_id: string;
    business_date: string;
    title: string;
    text: string;
    revision: number;
    match_kind?: string;
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
    <Card className="wb-evidence-search" title="查询评论与售后原文">
      <p className="wb-field-note">
        在授权对象与日期范围内查询原文。未完成检索不能作为确定无答案。
      </p>
      <Form
        name="evidence-search"
        layout="vertical"
        initialValues={{ kind: "reviews" }}
        onFinish={(values) => search.mutate(values)}
      >
        <div className="wb-search-grid">
          <Form.Item
            name="child_asin"
            label="查询商品"
            rules={[{ required: true }]}
          >
            <Select
              showSearch
              options={[...new Set(products.map((p) => p.child_asin))].map(
                (asin) => ({ value: asin, label: asin }),
              )}
            />
          </Form.Item>
          <Form.Item name="kind" label="证据来源">
            <Select
              options={[
                { value: "reviews", label: "评论" },
                { value: "support", label: "售后工单" },
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
            className="wb-search-query"
            name="query"
            label="问题词或原文短语"
            rules={[{ required: true }]}
          >
            <Input maxLength={200} placeholder="例如：充电慢" />
          </Form.Item>
        </div>
        <Collapse
          className="wb-search-filters"
          size="small"
          items={[
            {
              key: "filters",
              label: "更多筛选：SKU、语言、主题与极性",
              forceRender: true,
              children: (
                <div className="wb-search-grid">
                  <Form.Item name="product" label="SKU范围">
                    <Select
                      allowClear
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
                      options={[
                        { value: "negative", label: "负向" },
                        { value: "positive", label: "正向" },
                        { value: "mixed", label: "混合" },
                        { value: "neutral", label: "中性" },
                      ]}
                    />
                  </Form.Item>
                </div>
              ),
            },
          ]}
        />
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
          {search.data.relevance_judgment?.enabled &&
            search.data.relevance_judgment.complete === false && (
              <Alert
                type="warning"
                showIcon
                title="语义相关性判定未完成"
                description="供应商不可用、预算限制或候选不确定；不能作为确定无答案。"
              />
            )}
          {search.data.retrieval_complete === false && (
            <Alert
              type="warning"
              showIcon
              title="检索未完成，不能据此判断没有证据"
            />
          )}
          {search.data.match_status === "scope_context" && (
            <Alert
              type="warning"
              showIcon
              title="仅范围上下文，未找到相关主题证据"
              description="以下原文用于了解当前授权范围，不计为相关命中。"
            />
          )}
          {search.data.items.map((item) => (
            <Card size="small" key={item.fact_id} style={{ marginBottom: 8 }}>
              <Tag
                color={
                  item.match_kind === "scope_context"
                    ? "gold"
                    : item.match_kind === "relevant_hit"
                      ? "cyan"
                      : "default"
                }
              >
                {item.match_kind === "scope_context"
                  ? "仅范围上下文"
                  : item.match_kind === "relevant_hit"
                    ? "相关命中"
                    : "相关性待确认"}
              </Tag>
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
          {searched &&
            !search.data.items.length &&
            search.data.retrieval_complete !== false && (
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
