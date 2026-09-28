from uuid import uuid4


class SessionVersionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.contrib.auth import logout

        from .models import SessionVersion

        if request.user.is_authenticated:
            version = SessionVersion.objects.filter(user=request.user).values_list("version", flat=True).first() or 0
            if request.session.get("account_session_version", 0) != version:
                logout(request)
        return self.get_response(request)


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = str(uuid4())
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        return response
