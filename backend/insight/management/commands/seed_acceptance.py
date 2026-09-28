import json
import os
import secrets
from datetime import date
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from insight.models import Membership, Shop, ShopGrant, Workspace
from insight.services import create_product


class Command(BaseCommand):
    help = "创建隔离的本地验收工作区；不改写已有用户密码或正式数据。"

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("验收种子只允许在本地开发配置运行。")
        workspace, _ = Workspace.objects.get_or_create(name="航标验收工作区", is_test=True)
        users, credentials = {}, []
        for username in ["accept_admin", "accept_operator_a", "accept_operator_b"]:
            user, created = get_user_model().objects.get_or_create(username=username)
            if created:
                password = secrets.token_urlsafe(18)
                user.set_password(password)
                user.save()
                credentials.append({"username": username, "password": password})
            users[username] = user
            Membership.objects.get_or_create(
                workspace=workspace, user=user, defaults={"is_admin": username == "accept_admin"}
            )
        for label, operator in [("测试店铺 A", "accept_operator_a"), ("测试店铺 B", "accept_operator_b")]:
            shop, _ = Shop.objects.get_or_create(
                workspace=workspace,
                name=label,
                marketplace="US",
                defaults={"timezone": "America/Los_Angeles", "currency": "USD"},
            )
            ShopGrant.objects.get_or_create(shop=shop, user=users[operator], defaults={"roles": ["operator"]})
            if not shop.product_set.exists():
                create_product(
                    users["accept_admin"],
                    shop,
                    {
                        "child_asin": "B000TEST01",
                        "seller_sku": "ACCEPT-001",
                        "fulfillment": "FBA",
                        "name": "验收样本商品（合成）",
                        "parent_asin": "",
                        "owner": users[operator].pk,
                        "effective_from": date(2026, 1, 1),
                        "reason": "创建隔离验收样本",
                    },
                    "seed-acceptance",
                )
        if credentials:
            target = Path(settings.BASE_DIR).parent / ".local" / "acceptance-accounts.json"
            target.parent.mkdir(mode=0o700, exist_ok=True)
            # Exclusive creation prevents overwriting credentials from an earlier acceptance run.
            if target.exists():
                target = target.with_name(f"acceptance-accounts-{secrets.token_hex(4)}.json")
            with target.open("x") as handle:
                os.chmod(target, 0o600)
                json.dump(credentials, handle, ensure_ascii=False, indent=2)
            self.stdout.write(f"验收账号已创建，凭据仅保存到 {target}，未在输出中显示。")
        else:
            self.stdout.write("验收账号和样本已存在；未修改密码。")
