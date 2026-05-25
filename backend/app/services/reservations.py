from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional

from sqlalchemy import text

CENT = Decimal("0.01")


class PropertyNotFoundForTenant(Exception):
    """Raised when a tenant requests a property outside its tenant scope."""


def quantize_money(amount: Any) -> Decimal:
    """Round monetary values to cents using the standard finance rule."""
    if amount is None:
        return Decimal("0.00")
    return Decimal(str(amount)).quantize(CENT, rounding=ROUND_HALF_UP)


def format_money(amount: Any) -> str:
    return f"{quantize_money(amount):.2f}"


def money_to_cents(amount: Any) -> int:
    return int(quantize_money(amount) * 100)


async def calculate_monthly_revenue(
    property_id: str,
    tenant_id: str,
    month: int,
    year: int,
    db_session=None,
) -> Decimal:
    """
    Calculate revenue for a calendar month in the property's local timezone.
    """
    if month < 1 or month > 12:
        raise ValueError("month must be between 1 and 12")

    start_date = datetime(year, month, 1)
    if month < 12:
        end_date = datetime(year, month + 1, 1)
    else:
        end_date = datetime(year + 1, 1, 1)

    query = text("""
        SELECT COALESCE(SUM(r.total_amount), 0) AS total
        FROM properties p
        LEFT JOIN reservations r
          ON r.property_id = p.id
         AND r.tenant_id = p.tenant_id
         AND r.check_in_date >= (CAST(:start_date AS timestamp) AT TIME ZONE p.timezone)
         AND r.check_in_date < (CAST(:end_date AS timestamp) AT TIME ZONE p.timezone)
        WHERE p.id = :property_id
          AND p.tenant_id = :tenant_id
    """)

    async def execute(session):
        result = await session.execute(query, {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "start_date": start_date,
            "end_date": end_date,
        })
        return Decimal(str(result.scalar_one_or_none() or "0"))

    if db_session is not None:
        return await execute(db_session)

    from app.core.database_pool import db_pool

    await db_pool.initialize()
    async with db_pool.get_session() as session:
        return await execute(session)


async def calculate_total_revenue(
    property_id: str,
    tenant_id: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Aggregate tenant-scoped revenue from the database.
    """
    from app.core.database_pool import db_pool

    await db_pool.initialize()

    async with db_pool.get_session() as session:
        if month is None and year is None:
            query = text("""
                SELECT
                    p.id AS property_id,
                    COALESCE(SUM(r.total_amount), 0) AS total_revenue,
                    COUNT(r.id) AS reservation_count,
                    COALESCE(MAX(r.currency), 'USD') AS currency
                FROM properties p
                LEFT JOIN reservations r
                  ON r.property_id = p.id
                 AND r.tenant_id = p.tenant_id
                WHERE p.id = :property_id
                  AND p.tenant_id = :tenant_id
                GROUP BY p.id
            """)
            params = {"property_id": property_id, "tenant_id": tenant_id}
        else:
            if month is None or year is None:
                raise ValueError("month and year must be provided together")
            if month < 1 or month > 12:
                raise ValueError("month must be between 1 and 12")

            start_date = datetime(year, month, 1)
            if month < 12:
                end_date = datetime(year, month + 1, 1)
            else:
                end_date = datetime(year + 1, 1, 1)

            query = text("""
                SELECT
                    p.id AS property_id,
                    COALESCE(SUM(r.total_amount), 0) AS total_revenue,
                    COUNT(r.id) AS reservation_count,
                    COALESCE(MAX(r.currency), 'USD') AS currency
                FROM properties p
                LEFT JOIN reservations r
                  ON r.property_id = p.id
                 AND r.tenant_id = p.tenant_id
                 AND r.check_in_date >= (CAST(:start_date AS timestamp) AT TIME ZONE p.timezone)
                 AND r.check_in_date < (CAST(:end_date AS timestamp) AT TIME ZONE p.timezone)
                WHERE p.id = :property_id
                  AND p.tenant_id = :tenant_id
                GROUP BY p.id
            """)
            params = {
                "property_id": property_id,
                "tenant_id": tenant_id,
                "start_date": start_date,
                "end_date": end_date,
            }

        result = await session.execute(query, params)
        row = result.fetchone()

    if not row:
        raise PropertyNotFoundForTenant(property_id)

    total_revenue = quantize_money(row.total_revenue)
    return {
        "property_id": property_id,
        "tenant_id": tenant_id,
        "total": f"{total_revenue:.2f}",
        "total_cents": money_to_cents(total_revenue),
        "currency": row.currency or "USD",
        "count": int(row.reservation_count or 0),
        "period": {"month": month, "year": year} if month and year else None,
    }
