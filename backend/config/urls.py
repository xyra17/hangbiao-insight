from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView
from rest_framework.routers import DefaultRouter

from insight.actions import ActionTaskViewSet, ChangeProposalViewSet
from insight.administration import WorkspaceViewSet
from insight.ingestion.api import BatchViewSet, SourceViewSet
from insight.investigation.api import InvestigationViewSet
from insight.metrics.api import (
    EventViewSet,
    FactRowView,
    NotificationViewSet,
    ObservationViewSet,
    ScheduleViewSet,
    SnapshotViewSet,
)
from insight.metrics.configuration import RuleApplyView, RuleConfigurationView, RulePreviewView
from insight.metrics.parent_api import ParentSnapshotViewSet
from insight.semantic.api import EvidenceSearchView, ReportViewSet, SemanticRunViewSet
from insight.views import (
    AuditViewSet,
    HealthView,
    JobViewSet,
    LoginView,
    LogoutView,
    ProductViewSet,
    SessionView,
    ShopViewSet,
)

router = DefaultRouter()
router.register("workspaces", WorkspaceViewSet, basename="workspace")
router.register("shops", ShopViewSet, basename="shop")
router.register("products", ProductViewSet, basename="product")
router.register("jobs", JobViewSet, basename="job")
router.register("audit", AuditViewSet, basename="audit")
router.register("sources", SourceViewSet, basename="source")
router.register("imports", BatchViewSet, basename="import")
router.register("snapshots", SnapshotViewSet, basename="snapshot")
router.register("parent-snapshots", ParentSnapshotViewSet, basename="parent-snapshot")
router.register("semantic-runs", SemanticRunViewSet, basename="semantic-run")
router.register("reports", ReportViewSet, basename="report")
router.register("action-tasks", ActionTaskViewSet, basename="action-task")
router.register("change-proposals", ChangeProposalViewSet, basename="change-proposal")
router.register("investigations", InvestigationViewSet, basename="investigation")
router.register("events", EventViewSet, basename="event")
router.register("observations", ObservationViewSet, basename="observation")
router.register("notifications", NotificationViewSet, basename="notification")
router.register("scan-schedules", ScheduleViewSet, basename="scan-schedule")
urlpatterns = [
    path("api/v1/evidence-search/", EvidenceSearchView.as_view()),
    path("api/v1/rule-configurations/<uuid:shop_id>/", RuleConfigurationView.as_view()),
    path("api/v1/rule-configurations/preview/", RulePreviewView.as_view()),
    path("api/v1/rule-configurations/apply/", RuleApplyView.as_view()),
    path("api/v1/fact-rows/<uuid:row_id>/", FactRowView.as_view()),
    path("api/v1/session/", SessionView.as_view()),
    path("api/v1/login/", LoginView.as_view()),
    path("api/v1/logout/", LogoutView.as_view()),
    path("api/v1/health/", HealthView.as_view()),
    path("api/v1/schema/", SpectacularAPIView.as_view()),
    path("api/v1/", include(router.urls)),
]
