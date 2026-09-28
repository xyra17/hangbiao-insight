import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import JsonResponse
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.views import set_rollback


class Conflict(APIException):
    status_code = 409
    default_code = "version_conflict"
    default_detail = "内容已变化，请刷新后重新确认。"


def exception_handler(exc, context):
    if isinstance(exc, DjangoValidationError):
        exc = ValidationError("对象标识或输入格式不正确。")
    response = drf_exception_handler(exc, context)
    if response is None:
        set_rollback()
        request_id = getattr(context.get("request"), "request_id", "")
        # Exception text can contain provider responses or credentials; do not echo it.
        logging.getLogger(__name__).error("request_failed type=%s request_id=%s", type(exc).__name__, request_id)
        return Response({
            "code": "internal_error", "message": "服务暂时不可用，请凭请求编号联系管理员。",
            "field_errors": {}, "retryable": True, "request_id": request_id,
        }, status=500)
    if response is not None:
        details = response.data
        response.data = {
            "code": getattr(exc, "default_code", "request_error"),
            "message": str(details.get("detail", "请检查输入或刷新后重试。"))
            if isinstance(details, dict)
            else "请求未通过校验。",
            "field_errors": details,
            "retryable": response.status_code >= 500,
            "request_id": getattr(context.get("request"), "request_id", ""),
        }
    return response


def csrf_failure(request, reason=""):
    return JsonResponse({
        "code": "csrf_failed", "message": "登录校验已失效，请刷新页面后重试。",
        "field_errors": {}, "retryable": False, "request_id": getattr(request, "request_id", ""),
    }, status=403)
