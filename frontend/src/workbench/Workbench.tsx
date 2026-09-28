import { lazy, Suspense, useEffect, useState } from "react";
import {
  Alert,
  App,
  Button,
  ConfigProvider,
  Drawer,
  Empty,
  Form,
  Input,
  Layout,
  Menu,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import zhCN from "antd/locale/zh_CN";
import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useNavigate,
} from "react-router-dom";
import {
  Boxes,
  ClipboardCheck,
  Database,
  FileText,
  LogOut,
  Plus,
  Radar,
} from "lucide-react";
import {
  latestMapping,
  request,
  type Job,
  type Page,
  type Product,
  type Session,
  type Shop,
} from "./api";
import "./workbench.css";
const ReportsPage = lazy(() => import("./ReportsPage"));
const ActionsPage = lazy(() => import("./ActionsPage"));
const InvestigationPage = lazy(() => import("./InvestigationPage"));

const client = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: true } },
});
const SourcePage = lazy(() => import("./SourcePage"));
const EventPage = lazy(() => import("./EventPage"));
const SettingsPage = lazy(() => import("./SettingsPage"));
const { Title, Text } = Typography;
const statusNames: Record<string, string> = {
  queued: "等待执行",
  running: "执行中",
  completed: "已完成",
  cancelled: "已取消",
  failed: "失败",
};

function Login({ onLogin }: { onLogin: () => void }) {
  const login = useMutation({
    mutationFn: (data: { username: string; password: string }) =>
      request("login/", "POST", data),
    onSuccess: onLogin,
  });
  return (
    <main className="wb-login">
      <section className="wb-login-intro">
        <div className="wb-brand">
          <Radar size={30} />
          航标
        </div>
        <h1>
          从发现异常，
          <br />
          到有依据地处理。
        </h1>
        <p>
          在授权店铺内核查商品、经营数据和客户反馈，保留每次判断与处理记录。
        </p>
      </section>
      <section className="wb-login-form">
        <Title level={2}>登录工作台</Title>
        <p>使用管理员为你创建的账号。</p>
        {login.error && (
          <Alert type="error" showIcon title={login.error.message} />
        )}
        <Form layout="vertical" onFinish={(data) => login.mutate(data)}>
          <Form.Item
            label="用户名"
            name="username"
            rules={[{ required: true, message: "请输入用户名" }]}
          >
            <Input autoComplete="username" autoFocus />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            rules={[{ required: true, message: "请输入密码" }]}
          >
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Button
            type="primary"
            htmlType="submit"
            block
            loading={login.isPending}
          >
            登录
          </Button>
        </Form>
      </section>
    </main>
  );
}

function ProductPage({ shop, canEdit }: { shop: Shop; canEdit: boolean }) {
  const cache = useQueryClient();
  const { message } = App.useApp();
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState<Product | "new" | null>(null);
  const [history, setHistory] = useState<Product | null>(null);
  const [form] = Form.useForm();
  const products = useQuery({
    queryKey: ["products", shop.id, page],
    queryFn: () =>
      request<Page<Product>>(`products/?shop=${shop.id}&page=${page}`),
  });
  const members = useQuery({
    queryKey: ["members", shop.id],
    queryFn: () =>
      request<{ items: { id: number; username: string }[] }>(
        `shops/${shop.id}/members/`,
      ),
    enabled: canEdit,
  });
  const save = useMutation({
    mutationFn: (data: Record<string, unknown>) =>
      editing === "new"
        ? request<Product>("products/", "POST", { ...data, shop: shop.id })
        : request<Product>(`products/${editing?.id}/mappings/`, "POST", {
            ...data,
            expected_version: editing?.version,
          }),
    onSuccess: () => {
      setEditing(null);
      void cache.invalidateQueries({ queryKey: ["products", shop.id] });
      void message.success("映射已保存，历史记录已保留。");
    },
  });
  function open(product: Product | "new") {
    save.reset();
    form.resetFields();
    setEditing(product);
    form.setFieldsValue(
      product === "new"
        ? {
            fulfillment: "FBA",
            effective_from: new Date().toLocaleDateString("en-CA"),
          }
        : { ...latestMapping(product), effective_from: "", reason: "" },
    );
  }
  return (
    <>
      <div className="wb-page-heading">
        <div>
          <Title level={2}>商品映射</Title>
          <p>确认商品属于哪个站点、如何履约，以及由谁负责。</p>
        </div>
        {canEdit && (
          <Button
            type="primary"
            icon={<Plus size={16} />}
            onClick={() => open("new")}
          >
            新增商品映射
          </Button>
        )}
      </div>
      <div className="wb-context">
        <span>{shop.marketplace} 站</span>
        <span>业务时区：{shop.timezone}</span>
        <span>币种：{shop.currency}</span>
        <span>同一 ASIN 的不同 SKU 分别维护</span>
      </div>
      {products.error && (
        <Alert
          type="error"
          showIcon
          title={products.error.message}
          action={<Button onClick={() => void products.refetch()}>重试</Button>}
        />
      )}
      <Table<Product>
        rowKey="id"
        loading={products.isLoading}
        dataSource={products.data?.results ?? []}
        scroll={{ x: 950 }}
        pagination={{
          current: page,
          pageSize: 50,
          total: products.data?.count,
          onChange: setPage,
          showSizeChanger: false,
        }}
        locale={{
          emptyText: (
            <Empty description="该店铺尚无商品映射。请由数据管理员建立映射后导入报表。" />
          ),
        }}
        columns={[
          {
            title: "商品 / 子 ASIN",
            render: (_, p) => (
              <>
                <strong>{latestMapping(p)?.name}</strong>
                <div className="wb-secondary">{p.child_asin}</div>
              </>
            ),
          },
          { title: "SKU", dataIndex: "seller_sku" },
          { title: "履约", dataIndex: "fulfillment" },
          {
            title: "父 ASIN",
            render: (_, p) => latestMapping(p)?.parent_asin || "未设置",
          },
          {
            title: "负责人",
            render: (_, p) =>
              latestMapping(p)?.owner_name || <Tag color="gold">未分派</Tag>,
          },
          {
            title: "最新映射生效日",
            render: (_, p) => latestMapping(p)?.effective_from,
          },
          {
            title: "操作",
            render: (_, p) => (
              <Space>
                <Button type="link" onClick={() => setHistory(p)}>
                  查看历史
                </Button>
                {canEdit && (
                  <Button type="link" onClick={() => open(p)}>
                    更新映射
                  </Button>
                )}
              </Space>
            ),
          },
        ]}
      />
      <Drawer
        title={editing === "new" ? "新增商品映射" : "更新商品映射"}
        open={!!editing}
        onClose={() => setEditing(null)}
        size="large"
        destroyOnHidden
      >
        <p>历史映射会保留。变更从指定日期生效，统计按业务发生日的映射追溯。</p>
        {save.error && (
          <Alert
            type="error"
            showIcon
            title={save.error.message}
            className="wb-form-alert"
          />
        )}
        <Form
          form={form}
          layout="vertical"
          onFinish={(data) => save.mutate(data)}
        >
          {editing === "new" && (
            <>
              <Form.Item
                name="child_asin"
                label="子 ASIN"
                rules={[
                  { required: true },
                  {
                    pattern: /^[A-Z0-9]{10}$/,
                    message: "请输入 10 位大写字母或数字",
                  },
                ]}
              >
                <Input />
              </Form.Item>
              <Form.Item
                name="seller_sku"
                label="SKU"
                rules={[{ required: true }]}
              >
                <Input />
              </Form.Item>
              <Form.Item
                name="fulfillment"
                label="履约方式"
                rules={[{ required: true }]}
              >
                <Select options={[{ value: "FBA" }, { value: "FBM" }]} />
              </Form.Item>
            </>
          )}
          <Form.Item name="name" label="商品名称" rules={[{ required: true }]}>
            <Input maxLength={200} />
          </Form.Item>
          <Form.Item
            name="parent_asin"
            label="父 ASIN"
            rules={[
              {
                pattern: /^[A-Z0-9]{10}$/,
                message: "请输入 10 位大写字母或数字",
              },
            ]}
          >
            <Input maxLength={10} />
          </Form.Item>
          <Form.Item name="owner" label="负责人">
            <Select
              allowClear
              loading={members.isLoading}
              placeholder="未分派"
              options={members.data?.items.map((u) => ({
                value: u.id,
                label: u.username,
              }))}
            />
          </Form.Item>
          <Form.Item
            name="effective_from"
            label="生效日期"
            rules={[{ required: true }]}
          >
            <Input type="date" />
          </Form.Item>
          <Form.Item
            name="reason"
            label="变更原因"
            rules={[{ required: true }]}
          >
            <Input.TextArea maxLength={500} rows={3} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={save.isPending}>
            保存映射
          </Button>
        </Form>
      </Drawer>
      <Drawer
        title="映射历史"
        open={!!history}
        onClose={() => setHistory(null)}
        size="large"
      >
        {history && (
          <>
            <Text>
              {history.child_asin} / {history.seller_sku}
            </Text>
            {[...history.mappings]
              .sort((a, b) => b.version - a.version)
              .map((m) => (
                <section className="wb-history" key={m.id}>
                  <Tag>版本 {m.version}</Tag>
                  <strong>{m.name}</strong>
                  <p>
                    {m.effective_from} 起
                    {m.effective_to
                      ? `，至 ${m.effective_to} 前`
                      : "，后续未设置终止日"}
                  </p>
                  <p>
                    父 ASIN：{m.parent_asin || "未设置"}；负责人：
                    {m.owner_name || "未分派"}
                  </p>
                  <p>变更原因：{m.reason}</p>
                </section>
              ))}
          </>
        )}
      </Drawer>
    </>
  );
}

function JobPage({ shop, canEdit }: { shop: Shop; canEdit: boolean }) {
  const cache = useQueryClient();
  const jobs = useQuery({
    queryKey: ["jobs", shop.id],
    queryFn: () => request<Page<Job>>(`jobs/?shop=${shop.id}`),
    refetchInterval: 5000,
  });
  const probe = useMutation({
    mutationFn: () =>
      request("jobs/probe/", "POST", {
        shop: shop.id,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  return (
    <>
      <div className="wb-page-heading">
        <div>
          <Title level={2}>后台任务</Title>
          <p>任务会保存执行状态，离开页面后仍可回来查看。</p>
        </div>
        {canEdit && (
          <Button onClick={() => probe.mutate()} loading={probe.isPending}>
            检查后台服务
          </Button>
        )}
      </div>
      {(jobs.error || probe.error) && (
        <Alert type="error" title={(jobs.error || probe.error)?.message} />
      )}
      <Table<Job>
        rowKey="id"
        dataSource={jobs.data?.results}
        loading={jobs.isLoading}
        columns={[
          {
            title: "任务",
            render: (_, j) => (j.kind === "probe" ? "后台服务检查" : j.kind),
          },
          {
            title: "状态",
            render: (_, j) => <Tag>{statusNames[j.status] ?? j.status}</Tag>,
          },
          {
            title: "发起时间",
            render: (_, j) => new Date(j.created_at).toLocaleString("zh-CN"),
          },
          {
            title: "结果",
            render: (_, j) =>
              j.error ||
              (typeof j.result.message === "string"
                ? j.result.message
                : "等待执行结果"),
          },
        ]}
      />
    </>
  );
}

function Shell() {
  const cache = useQueryClient();
  const navigate = useNavigate();
  useEffect(() => {
    const expired = (event: Event) => {
      cache.clear();
      cache.setQueryData(["session"], (event as CustomEvent<Session>).detail);
    };
    window.addEventListener("workbench-session-expired", expired);
    return () =>
      window.removeEventListener("workbench-session-expired", expired);
  }, [cache]);
  const session = useQuery({
    queryKey: ["session"],
    queryFn: () => request<Session>("session/"),
  });
  const shops = useQuery({
    queryKey: ["shops"],
    queryFn: () => request<Page<Shop>>("shops/"),
    enabled: !!session.data?.authenticated,
  });
  const [shopId, setShopId] = useState<string>();
  const preferenceKey = session.data?.username
    ? `insight-shop:${session.data.username}`
    : undefined;
  const preferredShop =
    shopId ?? (preferenceKey ? sessionStorage.getItem(preferenceKey) : null);
  const selected =
    shops.data?.results.find((s) => s.id === preferredShop) ??
    shops.data?.results[0];
  const capabilities = useQuery({
    queryKey: ["capabilities", selected?.id],
    queryFn: () =>
      request<{ roles: string[] }>(`shops/${selected?.id}/capabilities/`),
    enabled: !!selected,
  });
  const products = useQuery({
    queryKey: ["all-products", selected?.id],
    queryFn: async () => {
      const rows: Product[] = [];
      for (let page = 1; ; page += 1) {
        const result = await request<Page<Product>>(
          `products/?shop=${selected?.id}&page=${page}`,
        );
        rows.push(...result.results);
        if (!result.next) return rows;
      }
    },
    enabled: !!selected,
  });
  const logout = useMutation({
    mutationFn: () => request("logout/", "POST"),
    onSuccess: () => {
      cache.clear();
      setShopId(undefined);
      navigate("/products");
    },
  });
  useEffect(() => {
    if (!session.data?.authenticated) setShopId(undefined);
  }, [session.data?.authenticated]);
  if (session.isLoading)
    return (
      <div className="wb-centered">
        <Spin tip="正在确认登录状态" size="large">
          <div />
        </Spin>
      </div>
    );
  if (session.error)
    return (
      <div className="wb-centered">
        <Alert
          type="error"
          title={session.error.message}
          action={
            <Button onClick={() => void session.refetch()}>重新连接</Button>
          }
        />
      </div>
    );
  if (!session.data?.authenticated)
    return (
      <Login
        onLogin={() => {
          cache.clear();
          void session.refetch();
        }}
      />
    );
  const canEdit = capabilities.data?.roles.includes("data_admin") ?? false;
  return (
    <Layout className="wb-layout">
      <Layout.Sider
        width={208}
        breakpoint="lg"
        collapsedWidth={64}
        className="wb-sidebar"
      >
        <div className="wb-brand">
          <Radar />
          航标
        </div>
        <Menu
          mode="inline"
          selectedKeys={[location.pathname]}
          onClick={({ key }) => navigate(key)}
          items={[
            {
              key: "/events",
              icon: <Radar size={18} />,
              label: "事件队列",
            },
            {
              key: "/reports",
              icon: <FileText size={18} />,
              label: "调查报告",
            },
            {
              key: "/investigations",
              icon: <Radar size={18} />,
              label: "自主调查",
            },
            {
              key: "/actions",
              icon: <ClipboardCheck size={18} />,
              label: "动作与复查",
            },
            { key: "/products", icon: <Boxes size={18} />, label: "商品映射" },
            {
              key: "/settings",
              icon: <Boxes size={18} />,
              label: "店铺与权限",
            },
            { key: "/sources", icon: <Database size={18} />, label: "数据源" },
            {
              key: "/jobs",
              icon: <ClipboardCheck size={18} />,
              label: "后台任务",
            },
          ]}
        />
        <div className="wb-sidebar-note">
          每个判断有依据
          <br />
          每次处理可追溯
        </div>
      </Layout.Sider>
      <Layout>
        <header className="wb-header">
          <Space wrap>
            <Select
              aria-label="选择店铺"
              value={selected?.id}
              loading={shops.isLoading}
              className="wb-shop-select"
              options={shops.data?.results.map((s) => ({
                value: s.id,
                label: `${s.name} / ${s.marketplace}`,
              }))}
              onChange={(value) => {
                setShopId(value);
                if (preferenceKey) sessionStorage.setItem(preferenceKey, value);
                cache.removeQueries({ queryKey: ["products"] });
              }}
            />
            {selected?.is_test && <Tag color="blue">验收测试数据</Tag>}
          </Space>
          <Space>
            <span>{session.data.username}</span>
            <Button
              icon={<LogOut size={16} />}
              onClick={() => logout.mutate()}
              loading={logout.isPending}
            >
              退出
            </Button>
          </Space>
        </header>
        <Layout.Content className="wb-content">
          {(shops.error || capabilities.error || logout.error) && (
            <Alert
              type="error"
              title={
                (shops.error || capabilities.error || logout.error)?.message
              }
            />
          )}
          {location.pathname === "/settings" ? (
            <Suspense fallback={<Spin />}>
              <SettingsPage workspaceId={selected?.workspace} />
            </Suspense>
          ) : selected ? (
            <Suspense fallback={<Spin />}>
              <Routes>
                <Route
                  path="/events"
                  element={
                    <EventPage
                      key={selected.id}
                      shop={selected}
                      canEdit={canEdit}
                    />
                  }
                />
                <Route
                  path="/reports"
                  element={
                    <ReportsPage
                      key={selected.id}
                      shop={selected}
                      products={products.data ?? []}
                    />
                  }
                />
                <Route
                  path="/investigations"
                  element={
                    <InvestigationPage
                      key={selected.id}
                      shop={selected}
                      products={products.data ?? []}
                    />
                  }
                />
                <Route
                  path="/actions"
                  element={
                    <ActionsPage
                      key={selected.id}
                      shop={selected}
                      products={products.data ?? []}
                      canEdit={canEdit}
                    />
                  }
                />
                <Route
                  path="/products"
                  element={
                    <ProductPage
                      key={selected.id}
                      shop={selected}
                      canEdit={canEdit}
                    />
                  }
                />
                <Route
                  path="/sources"
                  element={
                    <SourcePage
                      key={selected.id}
                      shop={selected}
                      canEdit={canEdit}
                    />
                  }
                />
                <Route
                  path="/jobs"
                  element={<JobPage shop={selected} canEdit={canEdit} />}
                />
                <Route path="*" element={<Navigate to="/products" replace />} />
              </Routes>
            </Suspense>
          ) : (
            !shops.isLoading && (
              <Empty description="尚未获得店铺权限，请联系数据管理员。" />
            )
          )}
        </Layout.Content>
      </Layout>
    </Layout>
  );
}

export default function Workbench() {
  return (
    <ConfigProvider
      locale={zhCN}
      button={{ autoInsertSpace: false }}
      theme={{
        token: {
          colorPrimary: "#285b83",
          borderRadius: 6,
          fontFamily: '"PingFang SC", "Microsoft YaHei", sans-serif',
          colorBgLayout: "#f3f6fa",
        },
      }}
    >
      <App>
        <QueryClientProvider client={client}>
          <BrowserRouter>
            <Shell />
          </BrowserRouter>
        </QueryClientProvider>
      </App>
    </ConfigProvider>
  );
}
