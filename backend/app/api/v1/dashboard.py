from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import Dict, Any
from app.services.cache import get_revenue_summary
from app.core.auth import authenticate_request as get_current_user
from app.models.auth import AuthenticatedUser
from app.services.reservations import PropertyNotFoundForTenant

router = APIRouter()

@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    month: int | None = Query(default=None, ge=1, le=12),
    year: int | None = Query(default=None, ge=1900, le=3000),
    current_user: AuthenticatedUser = Depends(get_current_user)
) -> Dict[str, Any]:
    tenant_id = current_user.tenant_id
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant context is required for revenue data",
        )

    if (month is None) != (year is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="month and year must be provided together",
        )

    try:
        revenue_data = await get_revenue_summary(property_id, tenant_id, month=month, year=year)
    except PropertyNotFoundForTenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Property not found for tenant",
        )

    total_revenue = Decimal(str(revenue_data["total"]))

    return {
        "property_id": revenue_data["property_id"],
        "tenant_id": revenue_data["tenant_id"],
        "total_revenue": float(total_revenue),
        "total_revenue_decimal": f"{total_revenue:.2f}",
        "total_revenue_cents": revenue_data["total_cents"],
        "currency": revenue_data["currency"],
        "reservations_count": revenue_data["count"],
        "period": revenue_data["period"],
    }
