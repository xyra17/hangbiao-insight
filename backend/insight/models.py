import uuid
from datetime import time

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import F, Q


class Entity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class Workspace(Entity):
    name = models.CharField(max_length=120)
    is_test = models.BooleanField(default=False)
    timezone = models.CharField(max_length=64, default="Asia/Shanghai")


class Membership(Entity):
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    is_admin = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "user"], name="unique_membership")]


class SessionVersion(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, primary_key=True)
    version = models.PositiveBigIntegerField(default=0)


class LoginThrottleBucket(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    window_start = models.DateTimeField(db_index=True)
    attempts = models.PositiveIntegerField(default=0)


class Shop(Entity):
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)
    name = models.CharField(max_length=120)
    marketplace = models.CharField(max_length=20)
    timezone = models.CharField(max_length=64)
    currency = models.CharField(max_length=3)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name", "marketplace"], name="unique_shop")
        ]


class Product(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    child_asin = models.CharField(max_length=10)
    seller_sku = models.CharField(max_length=100)
    fulfillment = models.CharField(max_length=12, choices=[("FBA", "FBA"), ("FBM", "FBM")])
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "child_asin", "seller_sku", "fulfillment"], name="unique_analysis_object"
            )
        ]


class ShopGrant(Entity):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    roles = models.JSONField(default=list)
    all_products = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    products = models.ManyToManyField(Product, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "shop"], name="unique_shop_grant")]


class ProductMapping(Entity):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="mappings")
    name = models.CharField(max_length=200)
    parent_asin = models.CharField(max_length=10, blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True)
    version = models.PositiveIntegerField()
    reason = models.CharField(max_length=500)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "version"], name="unique_mapping_version"),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True) | Q(effective_to__gt=F("effective_from")),
                name="mapping_positive_window",
            ),
        ]


class AuditEntry(Entity):
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)
    shop = models.ForeignKey(Shop, null=True, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=100)
    target_id = models.CharField(max_length=100)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    request_id = models.CharField(max_length=100, blank=True)


class Job(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=80)
    idempotency_key = models.CharField(max_length=150)
    status = models.CharField(max_length=30, default="queued")
    attempts = models.PositiveIntegerField(default=0)
    lease_token = models.PositiveBigIntegerField(default=0)
    result = models.JSONField(default=dict)
    error = models.CharField(max_length=300, blank=True)
    finished_at = models.DateTimeField(null=True)
    payload = models.JSONField(default=dict)
    heartbeat_at = models.DateTimeField(null=True)
    next_run_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "actor", "kind", "idempotency_key"], name="unique_job_command"
            )
        ]


class Outbox(Entity):
    job = models.OneToOneField(Job, on_delete=models.PROTECT)
    sent_at = models.DateTimeField(null=True)


class Source(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=40)
    is_primary = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "kind"], condition=Q(is_primary=True), name="one_primary_source_per_kind"
            )
        ]


class SourceContract(Entity):
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="contracts")
    version = models.PositiveIntegerField()
    grain = models.CharField(max_length=30)
    column_mapping = models.JSONField()
    ignored_columns = models.JSONField(default=list)
    coverage_basis = models.CharField(max_length=30, default="business_date")
    return_event = models.CharField(max_length=20, default="received")
    empty_report_means_zero = models.BooleanField(default=False)
    approved_at = models.DateTimeField(null=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "version"], name="unique_source_contract")]


class RawFile(Entity):
    sha256 = models.CharField(max_length=64, unique=True)
    storage_key = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()


class ImportBatch(Entity):
    contract = models.ForeignKey(SourceContract, on_delete=models.PROTECT)
    raw_file = models.ForeignKey(RawFile, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    filename = models.CharField(max_length=255)
    processing_key = models.CharField(max_length=64, unique=True)
    coverage_start = models.DateField()
    coverage_end = models.DateField()
    generated_at = models.DateTimeField()
    declared_complete = models.BooleanField(default=False)
    declared_asins = models.JSONField(default=list)
    mapping_manifest = models.JSONField(default=dict)
    status = models.CharField(max_length=30, default="uploaded")
    total_rows = models.PositiveIntegerField(default=0)
    valid_rows = models.PositiveIntegerField(default=0)
    error_rows = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)
    published_revision = models.PositiveIntegerField(null=True)
    published_at = models.DateTimeField(null=True)
    parse_cursor = models.PositiveIntegerField(default=0)


class StagedRow(Entity):
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT, related_name="rows")
    line = models.PositiveIntegerField()
    raw = models.JSONField()
    normalized = models.JSONField(default=dict)
    business_key = models.CharField(max_length=64, blank=True)
    payload_hash = models.CharField(max_length=64, blank=True)
    error = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["batch", "line"], name="unique_staged_line")]


class CoveragePartition(Entity):
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    batch = models.OneToOneField(ImportBatch, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    start = models.DateField()
    end = models.DateField()
    status = models.CharField(max_length=20)
    # Explicit ASINs are the declared reporting scope, not the set of observed rows.
    child_asins = models.JSONField(default=list)


class VersionedFact(Entity):
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT)
    raw_row = models.OneToOneField(StagedRow, on_delete=models.PROTECT)
    business_key = models.CharField(max_length=64)
    payload_hash = models.CharField(max_length=64)
    revision = models.PositiveIntegerField()
    child_asin = models.CharField(max_length=10)
    seller_sku = models.CharField(max_length=100, blank=True)
    fulfillment = models.CharField(max_length=12, blank=True)
    product = models.ForeignKey(Product, null=True, on_delete=models.PROTECT)
    mapping = models.ForeignKey(ProductMapping, null=True, on_delete=models.PROTECT)
    business_date = models.DateField()

    class Meta:
        abstract = True
        constraints = [
            models.UniqueConstraint(
                fields=["source", "business_key", "revision"], name="%(class)s_fact_revision"
            )
        ]
        indexes = [
            models.Index(fields=["source", "business_date", "child_asin"]),
            models.Index(fields=["source", "child_asin", "revision"]),
        ]


class SalesTrafficDaily(VersionedFact):
    ordered_units = models.PositiveIntegerField(null=True)
    order_count = models.PositiveIntegerField(null=True)
    sessions = models.PositiveIntegerField(null=True)
    buybox_percent = models.DecimalField(max_digits=7, decimal_places=4, null=True)
    sales_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True)
    currency = models.CharField(max_length=3, blank=True)


class OrderLine(VersionedFact):
    record_id = models.CharField(max_length=150)
    order_id = models.CharField(max_length=150)
    quantity = models.PositiveIntegerField()
    shipped_date = models.DateField(null=True)
    shipped_at = models.DateTimeField(null=True)
    shipped_quantity = models.PositiveIntegerField(null=True)
    original_line_id = models.CharField(max_length=150, blank=True)
    is_exchange = models.BooleanField(default=False)
    status = models.CharField(max_length=40)


class RefundTransaction(VersionedFact):
    record_id = models.CharField(max_length=150)
    order_line_id = models.CharField(max_length=150)
    transaction_chain_id = models.CharField(max_length=150)
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    currency = models.CharField(max_length=3)
    quantity = models.PositiveIntegerField(null=True)
    occurred_at = models.DateTimeField(null=True)


class ReturnLifecycle(VersionedFact):
    record_id = models.CharField(max_length=150)
    order_line_id = models.CharField(max_length=150, blank=True)
    quantity = models.PositiveIntegerField()
    reason = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=40)
    requested_date = models.DateField(null=True)
    received_date = models.DateField(null=True)
    requested_at = models.DateTimeField(null=True)
    received_at = models.DateTimeField(null=True)


class Review(VersionedFact):
    search_tokens = models.TextField(blank=True)
    search_index_version = models.CharField(max_length=80, blank=True)
    search_vector = SearchVectorField(null=True)

    class Meta(VersionedFact.Meta):
        abstract = False
        indexes = VersionedFact.Meta.indexes + [GinIndex(fields=["search_vector"], name="review_search_gin")]

    record_id = models.CharField(max_length=150)
    title = models.CharField(max_length=500, blank=True)
    text = models.TextField()
    rating = models.PositiveSmallIntegerField()
    language = models.CharField(max_length=20)
    collected_at = models.DateTimeField()
    is_deleted = models.BooleanField(default=False)
    verified_purchase = models.BooleanField(null=True)
    is_vine = models.BooleanField(null=True)


class SupportCase(VersionedFact):
    search_tokens = models.TextField(blank=True)
    search_index_version = models.CharField(max_length=80, blank=True)
    search_vector = SearchVectorField(null=True)

    class Meta(VersionedFact.Meta):
        abstract = False
        indexes = VersionedFact.Meta.indexes + [GinIndex(fields=["search_vector"], name="supportcase_search_gin")]

    record_id = models.CharField(max_length=150)
    order_line_id = models.CharField(max_length=150, blank=True)
    text = models.TextField()
    status = models.CharField(max_length=40)


class InventorySnapshot(VersionedFact):
    available = models.PositiveIntegerField(null=True)
    reserved = models.PositiveIntegerField(null=True)
    inbound = models.PositiveIntegerField(null=True)
    unsellable = models.PositiveIntegerField(null=True)
    total = models.PositiveIntegerField(null=True)
    listing_status = models.CharField(max_length=40, blank=True)


class RatingSnapshot(VersionedFact):
    rating = models.DecimalField(max_digits=3, decimal_places=2)
    rating_count = models.PositiveIntegerField(null=True)


class OperationEvent(VersionedFact):
    record_id = models.CharField(max_length=150)
    event_type = models.CharField(max_length=50)
    description = models.TextField()
    ends_on = models.DateField(null=True)
    price = models.DecimalField(max_digits=20, decimal_places=4, null=True)
    currency = models.CharField(max_length=3, blank=True)
    mechanism = models.CharField(max_length=200, blank=True)


class DirectorySource(Entity):
    source = models.OneToOneField(Source, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    # Registration is a server-admin command; the browser never supplies an arbitrary root.
    root = models.CharField(max_length=1000)
    pattern = models.CharField(max_length=100, default="*.csv")
    enabled = models.BooleanField(default=True)
    observations = models.JSONField(default=dict)
    error = models.CharField(max_length=500, blank=True)
    auto_publish = models.BooleanField(default=False)
    scan_interval_seconds = models.PositiveIntegerField(default=86400)
    next_scan_at = models.DateTimeField(null=True)


class MetricSnapshot(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    child_asin = models.CharField(max_length=10)
    product = models.ForeignKey(Product, null=True, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    fingerprint = models.CharField(max_length=64, unique=True)
    rule_version = models.CharField(max_length=40)
    source_manifest = models.JSONField()
    payload = models.JSONField()
    local_date = models.DateField()


class ParentSnapshot(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    parent_asin = models.CharField(max_length=10)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    fingerprint = models.CharField(max_length=64, unique=True)
    rule_version = models.CharField(max_length=40)
    source_manifest = models.JSONField()
    mapping_versions = models.JSONField()
    payload = models.JSONField()
    local_date = models.DateField()


class ObservationLead(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    child_asin = models.CharField(max_length=10)
    object_key = models.CharField(max_length=100)
    product = models.ForeignKey(Product, null=True, on_delete=models.PROTECT)
    family = models.CharField(max_length=40)
    metric = models.CharField(max_length=80)
    title = models.CharField(max_length=200)
    status = models.CharField(max_length=20, default="open")
    snapshot = models.ForeignKey(MetricSnapshot, on_delete=models.PROTECT)
    fingerprint = models.CharField(max_length=64, unique=True)
    detail = models.JSONField()
    last_seen = models.DateTimeField()


class SemanticRun(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    child_asin = models.CharField(max_length=10)
    product = models.ForeignKey(Product, null=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, default="queued")
    provider = models.CharField(max_length=40)
    model_name = models.CharField(max_length=120)
    prompt_version = models.CharField(max_length=80)
    taxonomy_version = models.CharField(max_length=80)
    source_manifest = models.JSONField(default=dict)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    total_reviews = models.PositiveIntegerField(default=0)
    accepted_claims = models.PositiveIntegerField(default=0)
    rejected_claims = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True)
    finished_at = models.DateTimeField(null=True)


class SemanticClaim(Entity):
    run = models.ForeignKey(SemanticRun, on_delete=models.PROTECT, related_name="claims")
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    review = models.ForeignKey("Review", on_delete=models.PROTECT)
    product = models.ForeignKey(Product, null=True, on_delete=models.PROTECT)
    prediction_id = models.CharField(max_length=80)
    primary_topic = models.CharField(max_length=80)
    secondary_topic = models.CharField(max_length=100)
    polarity = models.CharField(max_length=20)
    evidence_span = models.TextField()
    prediction_status = models.CharField(max_length=30)
    quality_flags = models.JSONField(default=list)
    provider = models.CharField(max_length=40)
    model_name = models.CharField(max_length=120)
    prompt_version = models.CharField(max_length=80)
    taxonomy_version = models.CharField(max_length=80)
    raw_response_ref = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "prediction_id"], name="unique_semantic_prediction")
        ]


class StandardReport(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    child_asin = models.CharField(max_length=10)
    product = models.ForeignKey(Product, null=True, on_delete=models.PROTECT)
    metric_snapshot = models.ForeignKey(MetricSnapshot, null=True, on_delete=models.PROTECT)
    semantic_run = models.ForeignKey(SemanticRun, null=True, on_delete=models.PROTECT)
    report_version = models.PositiveIntegerField(default=1)
    previous_report = models.ForeignKey("self", null=True, on_delete=models.PROTECT)
    input_versions = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default="draft")
    payload = models.JSONField(default=dict)
    publication_key = models.CharField(max_length=64, unique=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["shop", "child_asin", "product", "report_version"],
            nulls_distinct=False, name="unique_report_scope_version",
        )]


class ActionTask(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    event = models.ForeignKey("BusinessEvent", null=True, on_delete=models.PROTECT)
    source_report = models.ForeignKey(StandardReport, null=True, on_delete=models.PROTECT)
    child_asin = models.CharField(max_length=10)
    product = models.ForeignKey(Product, null=True, on_delete=models.PROTECT)
    title = models.CharField(max_length=240)
    status = models.CharField(max_length=20, default="open")
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="assigned_action_tasks"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_action_tasks"
    )
    due_at = models.DateTimeField(null=True)
    review_at = models.DateTimeField(null=True)
    review_status = models.CharField(max_length=20, default="not_scheduled")
    review_result = models.TextField(blank=True)
    review_comparison = models.JSONField(default=dict)
    action_type = models.CharField(max_length=40, default="investigation")
    question = models.TextField(blank=True)
    completion_criteria = models.TextField(blank=True)
    evidence_refs = models.JSONField(default=list)
    observed_metrics = models.JSONField(default=list)
    effective_at = models.DateTimeField(null=True)
    is_formal = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)


class ActionExecution(Entity):
    task = models.ForeignKey(ActionTask, on_delete=models.PROTECT, related_name="executions")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    notes = models.TextField()
    evidence_refs = models.JSONField(default=list)
    completed_at = models.DateTimeField()


class ChangeProposal(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    child_asin = models.CharField(max_length=10)
    product = models.ForeignKey(Product, null=True, on_delete=models.PROTECT)
    operation = models.CharField(max_length=40)
    target = models.JSONField()
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    evidence_refs = models.JSONField(default=list)
    status = models.CharField(max_length=20, default="pending")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requested_proposals"
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="reviewed_proposals"
    )
    review_note = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1)
    idempotency_key = models.CharField(max_length=150)

    payload_hash = models.CharField(max_length=64, blank=True)
    approved_payload_hash = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField(null=True)
    execution_result = models.JSONField(default=dict)
    executed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "requested_by", "idempotency_key"], name="unique_change_proposal_command"
            )
        ]


class InvestigationRun(Entity):
    previous_run = models.ForeignKey("self", null=True, on_delete=models.PROTECT, related_name="revisions")
    read_only_refresh = models.BooleanField(default=False)
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    query = models.TextField()
    child_asin = models.CharField(max_length=10, blank=True)
    product = models.ForeignKey(Product, null=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=30, default="queued")
    route = models.CharField(max_length=30, default="investigation")
    max_steps = models.PositiveSmallIntegerField(default=6)
    steps_used = models.PositiveSmallIntegerField(default=0)
    budget_units = models.PositiveSmallIntegerField(default=6)
    stop_reason = models.CharField(max_length=300, blank=True)
    state = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    checkpoint_version = models.PositiveIntegerField(default=0)
    input_versions = models.JSONField(default=dict)
    lease_token = models.PositiveBigIntegerField(default=0)


class InvestigationStep(Entity):
    run = models.ForeignKey(InvestigationRun, on_delete=models.PROTECT, related_name="steps")
    sequence = models.PositiveSmallIntegerField()
    node = models.CharField(max_length=60)
    tool_name = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=20)
    input = models.JSONField(default=dict)
    output = models.JSONField(default=dict)
    evidence_refs = models.JSONField(default=list)
    logical_key = models.CharField(max_length=100, blank=True)
    state_snapshot = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "sequence"], name="unique_investigation_step"),
            models.UniqueConstraint(fields=["run", "logical_key"], condition=~Q(logical_key=""), name="unique_investigation_logical_step"),
        ]


class EventGroup(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    child_asin = models.CharField(max_length=10)
    product = models.ForeignKey(Product, null=True, on_delete=models.PROTECT)
    title = models.CharField(max_length=240)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)


class BusinessEvent(Entity):
    group = models.ForeignKey(EventGroup, null=True, on_delete=models.PROTECT, related_name="events")
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    child_asin = models.CharField(max_length=10)
    object_key = models.CharField(max_length=100)
    product = models.ForeignKey(Product, null=True, on_delete=models.PROTECT)
    family = models.CharField(max_length=40)
    title = models.CharField(max_length=200)
    status = models.CharField(max_length=30, default="pending")
    priority = models.PositiveSmallIntegerField()
    magnitude = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    impact = models.DecimalField(max_digits=20, decimal_places=4, null=True)
    last_cutoff = models.DateField()
    last_seen = models.DateTimeField()
    last_checked_at = models.DateTimeField(null=True)
    needs_data = models.BooleanField(default=False)
    consecutive_days = models.PositiveIntegerField(default=1)
    version = models.PositiveIntegerField(default=1)
    reopened_from = models.ForeignKey("self", null=True, on_delete=models.PROTECT)
    closed_at = models.DateTimeField(null=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="owned_events")
    closure_reason = models.CharField(max_length=30, blank=True)
    closure_note = models.TextField(blank=True)
    closure_evidence = models.JSONField(default=list)
    duplicate_of = models.ForeignKey("self", null=True, on_delete=models.PROTECT, related_name="duplicates")
    snoozed_until = models.DateTimeField(null=True)
    snooze_reason = models.CharField(max_length=500, blank=True)
    notified_magnitude = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    notified_magnitude_ratio = models.JSONField(default=list)
    notified_priority = models.PositiveSmallIntegerField(default=3)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "object_key", "family"],
                condition=~Q(status="closed"),
                name="one_open_event_per_family",
            )
        ]


class EventEvidence(Entity):
    event = models.ForeignKey(BusinessEvent, on_delete=models.PROTECT, related_name="evidence")
    snapshot = models.ForeignKey(MetricSnapshot, on_delete=models.PROTECT)
    metric = models.CharField(max_length=80)
    detail = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "snapshot", "metric"], name="unique_event_evidence")
        ]


class Notification(Entity):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    event = models.ForeignKey(BusinessEvent, null=True, on_delete=models.PROTECT)
    task = models.ForeignKey(ActionTask, null=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=40)
    read_at = models.DateTimeField(null=True)
    data_version = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=(Q(event__isnull=False, task__isnull=True) | Q(event__isnull=True, task__isnull=False)), name="notification_has_one_target"),
            models.UniqueConstraint(fields=["user", "task", "kind", "data_version"], name="unique_task_notification"),
            models.UniqueConstraint(
                fields=["user", "event", "kind", "data_version"], name="unique_event_notification"
            )
        ]


class DailyScanSchedule(Entity):
    shop = models.OneToOneField(Shop, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    enabled = models.BooleanField(default=True)
    at = models.TimeField(default=time(8, 0))
    last_run_date = models.DateField(null=True)
    version = models.PositiveIntegerField(default=1)
    error = models.CharField(max_length=300, blank=True)


class RuleConfiguration(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    values = models.JSONField()
    reason = models.CharField(max_length=500)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["shop", "version"], name="unique_rule_configuration")]


class InvestigationCommandReceipt(Entity):
    run = models.ForeignKey(InvestigationRun, on_delete=models.PROTECT, related_name="command_receipts")
    logical_action_id = models.CharField(max_length=100)
    payload_hash = models.CharField(max_length=64)
    evidence_fingerprint = models.CharField(max_length=64, db_index=True)
    task = models.ForeignKey(ActionTask, on_delete=models.PROTECT)
    result = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "logical_action_id"], name="unique_investigation_write_command")]


class EventGroupingReceipt(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=150)
    payload_hash = models.CharField(max_length=64)
    result = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["shop", "actor", "idempotency_key"], name="unique_event_group_command")]


class DerivedRefresh(Entity):
    shop = models.ForeignKey(Shop, on_delete=models.PROTECT)
    batch = models.ForeignKey(ImportBatch, on_delete=models.PROTECT)
    artifact_type = models.CharField(max_length=30)
    artifact_id = models.UUIDField()
    replacement_id = models.UUIDField(null=True)
    status = models.CharField(max_length=30, default="queued")
    detail = models.CharField(max_length=500, blank=True)
    job = models.OneToOneField(Job, null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["batch", "artifact_type", "artifact_id"], name="unique_artifact_refresh")]


class SemanticReviewResult(Entity):
    run = models.ForeignKey(SemanticRun, on_delete=models.PROTECT, related_name="review_results")
    review = models.ForeignKey(Review, on_delete=models.PROTECT)
    status = models.CharField(max_length=20)
    detail = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "review"], name="unique_semantic_review_result")]
