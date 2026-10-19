import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rest_framework import serializers

from .models import AuditEntry, Job, Product, ProductMapping, Shop


class ShopSerializer(serializers.ModelSerializer):
    is_test = serializers.BooleanField(source="workspace.is_test", read_only=True)

    class Meta:
        model = Shop
        fields = ["id", "workspace", "name", "marketplace", "timezone", "currency", "is_test"]
        read_only_fields = fields

    def validate_timezone(self, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise serializers.ValidationError("请填写有效时区，如 America/Los_Angeles。")
        return value

    def validate_currency(self, value):
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise serializers.ValidationError("币种须为三位大写代码。")
        return value


class MappingSerializer(serializers.ModelSerializer):
    owner_name = serializers.CharField(source="owner.username", default=None, allow_null=True, read_only=True)

    class Meta:
        model = ProductMapping
        fields = [
            "id",
            "name",
            "parent_asin",
            "owner",
            "owner_name",
            "effective_from",
            "effective_to",
            "version",
            "reason",
        ]
        read_only_fields = fields


class ProductSerializer(serializers.ModelSerializer):
    mappings = MappingSerializer(many=True, read_only=True)

    class Meta:
        model = Product
        fields = ["id", "shop", "child_asin", "seller_sku", "fulfillment", "version", "mappings"]
        read_only_fields = fields


class MappingCommandSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    parent_asin = serializers.RegexField(r"^[A-Z0-9]{10}$", required=False, allow_blank=True, default="")
    owner = serializers.IntegerField(required=False, allow_null=True, default=None)
    effective_from = serializers.DateField()
    reason = serializers.CharField(max_length=500)
    expected_version = serializers.IntegerField(min_value=1, required=False)


class ProductCommandSerializer(MappingCommandSerializer):
    shop = serializers.UUIDField()
    child_asin = serializers.RegexField(r"^[A-Z0-9]{10}$")
    seller_sku = serializers.CharField(max_length=100)
    fulfillment = serializers.ChoiceField(choices=["FBA", "FBM"])


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class JobSerializer(serializers.ModelSerializer):
    result = serializers.DictField(child=serializers.JSONField(), read_only=True)

    class Meta:
        model = Job
        fields = [
            "id",
            "shop",
            "kind",
            "status",
            "attempts",
            "result",
            "error",
            "created_at",
            "started_at",
            "heartbeat_at",
            "finished_at",
        ]
        read_only_fields = fields


class JobCommandSerializer(serializers.Serializer):
    shop = serializers.UUIDField()
    idempotency_key = serializers.CharField(max_length=150)


class AuditSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.username", read_only=True)

    class Meta:
        model = AuditEntry
        fields = ["id", "actor_name", "action", "target_id", "before", "after", "request_id", "created_at"]
        read_only_fields = fields
