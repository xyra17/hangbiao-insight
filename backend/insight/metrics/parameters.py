from decimal import Decimal

# Display values use percentages/percentage points; algorithms convert explicitly.
PARAMETERS = {
    "sales_change_pct": ("销量相对变化（%）", 30, 1, 1000),
    "sales_gap": ("销量差额（件）", 10, 1, 1000000),
    "sales_min_baseline": ("基线最少件数", 40, 1, 1000000),
    "traffic_drop_pct": ("流量下降（%）", 30, 1, 100),
    "traffic_min_baseline": ("基线最少 Sessions", 200, 1, 10000000),
    "conversion_min_sessions": ("转化比较每窗最少 Sessions", 100, 1, 10000000),
    "conversion_drop_pct": ("转化相对下降（%）", 20, 1, 100),
    "conversion_drop_pp": ("转化绝对下降（百分点）", 2, 0.1, 100),
    "cohort_min_orders": ("队列每窗最少成熟订单", 100, 1, 1000000),
    "cohort_min_affected": ("队列当前最少相关订单", 5, 1, 1000000),
    "cohort_increase_pp": ("队列上升（百分点）", 2, 0.1, 100),
    "observation_days": ("队列观察期（天）", 30, 1, 180),
    "occurrence_increase_pct": ("发生量相对增加（%）", 50, 1, 10000),
    "occurrence_gap": ("发生量最少增量（条）", 5, 1, 1000000),
    "rating_drop": ("页面星级下降（星）", 0.2, 0.1, 4),
    "rating_compare_days": ("星级对照快照最长间隔（天）", 7, 1, 90),
    "buybox_below_pct": ("购物车占比低于（%）", 80, 1, 100),
    "buybox_drop_pp": ("购物车下降（百分点）", 20, 0.1, 100),
}
DEFAULTS = {key: value[1] for key, value in PARAMETERS.items()}
INTEGER_FIELDS = {key for key in DEFAULTS if not key.endswith(("_pct", "_pp")) and key != "rating_drop"}


def numbers(values=None):
    return {key: Decimal(str(value)) for key, value in (DEFAULTS | (values or {})).items()}
