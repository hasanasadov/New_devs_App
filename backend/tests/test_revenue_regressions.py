import json
import sys
import types
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import redis.asyncio  # noqa: F401
except ModuleNotFoundError:
    redis_module = types.ModuleType("redis")
    redis_asyncio_module = types.ModuleType("redis.asyncio")
    redis_exceptions_module = types.ModuleType("redis.exceptions")

    class RedisError(Exception):
        pass

    class Redis:
        @staticmethod
        def from_url(_url):
            return object()

    redis_asyncio_module.Redis = Redis
    redis_exceptions_module.RedisError = RedisError
    sys.modules["redis"] = redis_module
    sys.modules["redis.asyncio"] = redis_asyncio_module
    sys.modules["redis.exceptions"] = redis_exceptions_module

try:
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError:
    sqlalchemy_module = types.ModuleType("sqlalchemy")
    sqlalchemy_module.text = lambda statement: statement
    sys.modules["sqlalchemy"] = sqlalchemy_module

import app.services.cache as cache_module
from app.services.cache import get_revenue_summary
from app.services.reservations import calculate_monthly_revenue, format_money, money_to_cents


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value


class FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class CapturingSession:
    def __init__(self, value):
        self.value = value
        self.statement = ""
        self.params = {}

    async def execute(self, statement, params):
        self.statement = str(statement)
        self.params = params
        return FakeScalarResult(self.value)


class RevenueRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_revenue_cache_is_tenant_scoped(self):
        fake_redis = FakeRedis()
        calls = []

        async def fake_calculate(property_id, tenant_id, month=None, year=None):
            calls.append((property_id, tenant_id, month, year))
            total = "100.00" if tenant_id == "tenant-a" else "250.00"
            return {
                "property_id": property_id,
                "tenant_id": tenant_id,
                "total": total,
                "total_cents": int(Decimal(total) * 100),
                "currency": "USD",
                "count": 1,
                "period": None,
            }

        with patch.object(cache_module, "redis_client", fake_redis), patch(
            "app.services.reservations.calculate_total_revenue",
            side_effect=fake_calculate,
        ):
            tenant_a_first = await get_revenue_summary("prop-001", "tenant-a")
            tenant_b = await get_revenue_summary("prop-001", "tenant-b")
            tenant_a_second = await get_revenue_summary("prop-001", "tenant-a")

        self.assertEqual(tenant_a_first["total"], "100.00")
        self.assertEqual(tenant_b["total"], "250.00")
        self.assertEqual(tenant_a_second["total"], "100.00")
        self.assertEqual(calls, [
            ("prop-001", "tenant-a", None, None),
            ("prop-001", "tenant-b", None, None),
        ])
        self.assertEqual(
            json.loads(fake_redis.store["revenue:tenant-a:prop-001:all:all"])["tenant_id"],
            "tenant-a",
        )

    async def test_monthly_revenue_uses_property_timezone_boundaries(self):
        session = CapturingSession(Decimal("1250.000"))

        total = await calculate_monthly_revenue(
            "prop-001",
            "tenant-a",
            3,
            2024,
            db_session=session,
        )

        self.assertEqual(total, Decimal("1250.000"))
        self.assertIn("AT TIME ZONE p.timezone", session.statement)
        self.assertEqual(session.params["start_date"], datetime(2024, 3, 1))
        self.assertEqual(session.params["end_date"], datetime(2024, 4, 1))
        self.assertEqual(session.params["tenant_id"], "tenant-a")

    def test_money_rounding_uses_decimal_half_up(self):
        self.assertEqual(format_money(Decimal("10.005")), "10.01")
        self.assertEqual(format_money(Decimal("10.004")), "10.00")
        self.assertEqual(money_to_cents(Decimal("10.005")), 1001)


if __name__ == "__main__":
    unittest.main()
