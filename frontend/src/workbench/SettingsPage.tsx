import { useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Empty,
  Form,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { allPages, request, type Product, type Shop } from "./api";
import type { components } from "./schema";
type Workspace = components["schemas"]["Workspace"];
type Member = components["schemas"]["WorkspaceMember"];
type Grant = components["schemas"]["Grant"];
const roleLabels: Record<string, string> = {
  operator: "运营",
  quality: "品控",
  support: "客服",
  data_admin: "数据管理",
};
export default function SettingsPage({
  workspaceId,
}: {
  workspaceId?: string;
}) {
  const cache = useQueryClient();
  const [chosen, setChosen] = useState<string>();
  const workspaces = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => allPages<Workspace>("workspaces/"),
  });
  const admins = workspaces.data?.filter((w) => w.is_admin) ?? [];
  const workspace =
    admins.find((w) => w.id === (chosen ?? workspaceId)) ?? admins[0];
  const members = useQuery({
    queryKey: ["workspace-members", workspace?.id],
    queryFn: () => allPages<Member>(`workspaces/${workspace?.id}/members/`),
    enabled: !!workspace,
  });
  const grants = useQuery({
    queryKey: ["workspace-grants", workspace?.id],
    queryFn: () => allPages<Grant>(`workspaces/${workspace?.id}/grants/`),
    enabled: !!workspace,
  });
  const shops = useQuery({
    queryKey: ["settings-shops", workspace?.id],
    queryFn: async () =>
      (await allPages<Shop>("shops/")).filter(
        (s) => s.workspace === workspace?.id,
      ),
    enabled: !!workspace,
  });
  const [mode, setMode] = useState<
    "member" | "shop" | "grant" | "password" | "sessions"
  >();
  const [credentialForm] = Form.useForm();
  const [targetMember, setTargetMember] = useState<Member>();
  const [memberForm] = Form.useForm();
  const [shopForm] = Form.useForm();
  const [grantForm] = Form.useForm();
  const grantShop = Form.useWatch("shop", grantForm);
  const allProducts = Form.useWatch("all_products", grantForm);
  const products = useQuery({
    queryKey: ["grant-products", grantShop],
    queryFn: () => allPages<Product>(`products/?shop=${grantShop}`),
    enabled: !!grantShop && mode === "grant",
  });
  const refresh = () => {
    for (const key of [
      "workspaces",
      "workspace-members",
      "workspace-grants",
      "settings-shops",
      "shops",
      "capabilities",
      "products",
      "members",
    ])
      void cache.invalidateQueries({ queryKey: [key] });
  };
  const save = useMutation({
    mutationFn: ({
      action,
      data,
    }: {
      action: string;
      data: Record<string, unknown>;
    }) => request(`workspaces/${workspace?.id}/${action}/`, "POST", data),
    onSuccess: () => {
      setMode(undefined);
      credentialForm.resetFields();
      refresh();
    },
  });
  const changeRole = useMutation({
    mutationFn: (member: Member) =>
      request(`workspaces/${workspace?.id}/member-role/`, "POST", {
        member: member.id,
        expected_version: member.version,
        is_admin: !member.is_admin,
      }),
    onSuccess: refresh,
  });
  function editGrant(user: number, shop: string) {
    const existing = grants.data?.find(
      (g) => g.user === user && g.shop === shop,
    );
    grantForm.setFieldsValue({
      user,
      shop,
      roles: existing?.roles ?? [],
      all_products: existing?.all_products ?? true,
      products: existing?.products ?? [],
      expected_version: existing?.version ?? 0,
    });
  }
  if (workspaces.error)
    return <Alert type="error" title={workspaces.error.message} />;
  if (!workspace && !workspaces.isLoading)
    return <Empty description="店铺与成员授权由工作区管理员维护。" />;
  return (
    <>
      <div className="wb-page-heading">
        <div>
          <Typography.Title level={2}>店铺与成员权限</Typography.Title>
          <p>明确每个人能查看的店铺、商品与可执行操作。</p>
        </div>
        <Select
          aria-label="管理工作区"
          style={{ minWidth: 220 }}
          value={workspace?.id}
          options={admins.map((w) => ({ value: w.id, label: w.name }))}
          onChange={(v) => {
            setChosen(v);
            setMode(undefined);
          }}
        />
      </div>
      {(members.error || grants.error || shops.error || changeRole.error) && (
        <Alert
          type="error"
          title={
            (members.error || grants.error || shops.error || changeRole.error)
              ?.message
          }
        />
      )}
      <Space style={{ marginBottom: 20 }}>
        <Button
          type="primary"
          disabled={!workspace}
          onClick={() => {
            memberForm.resetFields();
            save.reset();
            setMode("member");
          }}
        >
          新增成员账号
        </Button>
        <Button
          disabled={!workspace}
          onClick={() => {
            shopForm.resetFields();
            save.reset();
            setMode("shop");
          }}
        >
          登记店铺
        </Button>
        <Button onClick={refresh}>刷新授权</Button>
      </Space>
      <Table<Member>
        rowKey="id"
        dataSource={members.data}
        loading={members.isLoading}
        pagination={false}
        columns={[
          { title: "成员", dataIndex: "username" },
          {
            title: "工作区身份",
            render: (_, m) => <Tag>{m.is_admin ? "管理员" : "普通成员"}</Tag>,
          },
          {
            title: "店铺授权",
            render: (_, m) =>
              m.is_admin ? (
                "全部店铺与商品"
              ) : (
                <Space wrap>
                  {grants.data
                    ?.filter((g) => g.user === m.user && g.roles.length > 0)
                    .map((g) => (
                      <Tag key={g.id}>
                        {shops.data?.find((s) => s.id === g.shop)?.name ??
                          "店铺"}{" "}
                        ·{" "}
                        {g.roles
                          .map((r: string) => roleLabels[r] ?? r)
                          .join("、")}{" "}
                        ·{" "}
                        {g.all_products
                          ? "全部商品"
                          : `${g.products.length} 个商品`}
                      </Tag>
                    ))}
                </Space>
              ),
          },
          {
            title: "操作",
            render: (_, m) => (
              <Space>
                <Button
                  disabled={
                    m.is_admin || !shops.data?.length || grants.isLoading
                  }
                  onClick={() => {
                    save.reset();
                    setMode("grant");
                    editGrant(m.user, shops.data![0].id);
                  }}
                >
                  配置店铺权限
                </Button>
                <Button
                  onClick={() => {
                    credentialForm.resetFields();
                    save.reset();
                    setTargetMember(m);
                    setMode("password");
                  }}
                >
                  重置密码
                </Button>
                <Button
                  onClick={() => {
                    credentialForm.resetFields();
                    save.reset();
                    setTargetMember(m);
                    setMode("sessions");
                  }}
                >
                  强制退出
                </Button>
                <Button
                  loading={changeRole.isPending}
                  onClick={() => changeRole.mutate(m)}
                >
                  {m.is_admin ? "改为普通成员" : "设为管理员"}
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <Typography.Title level={4} style={{ marginTop: 32 }}>
        已登记店铺
      </Typography.Title>
      <Table<Shop>
        rowKey="id"
        dataSource={shops.data}
        pagination={false}
        columns={[
          { title: "店铺", dataIndex: "name" },
          { title: "站点", dataIndex: "marketplace" },
          { title: "业务时区", dataIndex: "timezone" },
          { title: "币种", dataIndex: "currency" },
        ]}
      />
      <Drawer
        title={
          mode === "password"
            ? "重置成员密码"
            : mode === "sessions"
              ? "撤销成员登录会话"
              : mode === "member"
                ? "新增成员账号"
                : mode === "shop"
                  ? "登记店铺"
                  : "配置店铺权限"
        }
        open={!!mode}
        onClose={() => {
          setMode(undefined);
          credentialForm.resetFields();
        }}
        destroyOnHidden
      >
        {save.error && <Alert type="error" title={save.error.message} />}
        {(mode === "password" || mode === "sessions") && targetMember && (
          <Form
            form={credentialForm}
            layout="vertical"
            onFinish={(data) =>
              save.mutate({
                action:
                  mode === "password" ? "reset-password" : "revoke-sessions",
                data: {
                  ...data,
                  member: targetMember.id,
                  expected_version: targetMember.version,
                },
              })
            }
          >
            <Alert
              type="warning"
              title={`将使 ${targetMember.username} 的所有已有登录会话失效。`}
            />
            <p>操作作用于整个账号。此操作不会撤销业务授权或取消后台任务。</p>
            {mode === "password" && (
              <Form.Item
                name="password"
                label="新密码"
                rules={[{ required: true, min: 12 }]}
              >
                <Input.Password autoComplete="new-password" />
              </Form.Item>
            )}
            <Form.Item
              name="reason"
              label="操作原因"
              rules={[{ required: true, whitespace: true, max: 300 }]}
            >
              <Input.TextArea maxLength={300} />
            </Form.Item>
            <Button
              type="primary"
              danger
              htmlType="submit"
              loading={save.isPending}
            >
              {mode === "password" ? "确认重置并退出" : "确认强制退出"}
            </Button>
          </Form>
        )}
        {mode === "member" && (
          <Form
            form={memberForm}
            layout="vertical"
            initialValues={{ is_admin: false }}
            onFinish={(data) => save.mutate({ action: "new-member", data })}
          >
            <Form.Item
              name="username"
              label="用户名"
              rules={[{ required: true }]}
            >
              <Input autoComplete="off" />
            </Form.Item>
            <Form.Item
              name="password"
              label="初始密码"
              rules={[{ required: true, min: 12 }]}
            >
              <Input.Password autoComplete="new-password" />
            </Form.Item>
            <Form.Item name="is_admin" valuePropName="checked">
              <Checkbox>授予工作区管理员权限（全部店铺与商品）</Checkbox>
            </Form.Item>
            <p className="wb-secondary">
              普通成员创建后，还需配置店铺和商品范围。请通过团队认可的安全方式交付初始密码。
            </p>
            <Button type="primary" htmlType="submit" loading={save.isPending}>
              创建成员
            </Button>
          </Form>
        )}
        {mode === "shop" && (
          <Form
            form={shopForm}
            layout="vertical"
            initialValues={{
              marketplace: "US",
              timezone: "America/Los_Angeles",
              currency: "USD",
            }}
            onFinish={(data) => save.mutate({ action: "new-shop", data })}
          >
            <Form.Item
              name="name"
              label="店铺名称"
              rules={[{ required: true }]}
            >
              <Input />
            </Form.Item>
            <Form.Item
              name="marketplace"
              label="站点代码"
              rules={[{ required: true }]}
            >
              <Input />
            </Form.Item>
            <Form.Item
              name="timezone"
              label="业务时区"
              rules={[{ required: true }]}
            >
              <Input />
            </Form.Item>
            <Form.Item
              name="currency"
              label="币种"
              rules={[{ required: true }]}
            >
              <Input />
            </Form.Item>
            <p className="wb-secondary">
              请按实际站点核对时区与币种，它们决定日期窗口和金额口径。
            </p>
            <Button type="primary" htmlType="submit" loading={save.isPending}>
              保存店铺
            </Button>
          </Form>
        )}
        {mode === "grant" && (
          <Form
            form={grantForm}
            layout="vertical"
            onFinish={(data) =>
              save.mutate({
                action: "set-grant",
                data: {
                  ...data,
                  products: data.all_products ? [] : data.products,
                },
              })
            }
          >
            <Form.Item name="user" hidden>
              <Input />
            </Form.Item>
            <Form.Item name="expected_version" hidden>
              <Input />
            </Form.Item>
            <Form.Item
              name="shop"
              label="授权店铺"
              rules={[{ required: true }]}
            >
              <Select
                showSearch
                optionFilterProp="label"
                options={shops.data?.map((s) => ({
                  value: s.id,
                  label: `${s.name} / ${s.marketplace}`,
                }))}
                onChange={(id) =>
                  editGrant(grantForm.getFieldValue("user"), id)
                }
              />
            </Form.Item>
            <Form.Item name="roles" label="角色">
              <Select
                mode="multiple"
                options={Object.entries(roleLabels).map(([value, label]) => ({
                  value,
                  label,
                }))}
              />
            </Form.Item>
            <Form.Item name="all_products" valuePropName="checked">
              <Checkbox>允许访问该店铺全部商品</Checkbox>
            </Form.Item>
            {!allProducts && (
              <Form.Item name="products" label="允许的商品">
                <Select
                  mode="multiple"
                  loading={products.isLoading}
                  options={products.data?.map((p) => ({
                    value: p.id,
                    label: `${p.child_asin} / ${p.seller_sku} / ${p.fulfillment}`,
                  }))}
                />
              </Form.Item>
            )}
            {products.error && (
              <Alert type="error" title={products.error.message} />
            )}
            <p className="wb-secondary">
              清空角色会撤销该店铺权限。整店报表导入需要“数据管理”及“全部商品”权限。
            </p>
            <Button type="primary" htmlType="submit" loading={save.isPending}>
              保存授权
            </Button>
          </Form>
        )}
      </Drawer>
    </>
  );
}
