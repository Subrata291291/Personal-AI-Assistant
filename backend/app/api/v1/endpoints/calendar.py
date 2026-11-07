from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import EmailStr
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require_integration_enabled
from app.db.session import get_db
from app.integrations.google.exceptions import GoogleAPIError
from app.schemas.calendar import (
    CalendarListResponse,
    CalendarEvent,
    CalendarEventListResponse,
    CalendarScheduleResponse,
    FreeBusyRequest,
    FreeBusyResponse,
)
from app.services.calendar_service import CalendarService


router = APIRouter(
    dependencies=[Depends(require_integration_enabled("calendar"))],
)


def _get_user_id(current_user: dict) -> UUID:
    try:
        return UUID(str(current_user["id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unable to identify authenticated user.",
        ) from exc


def _handle_calendar_error(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND
            if str(exc) in {
                "Connected Google account was not found.",
                "Google OAuth credentials were not found.",
            }
            else status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if isinstance(exc, GoogleAPIError):
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Google Calendar resource not found.",
            ) from exc
        if exc.status_code == 403:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Google denied access to this Calendar resource.",
            ) from exc
        if exc.status_code == 429 or exc.retryable:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Google Calendar is temporarily unavailable.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google Calendar request failed.",
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unable to process Calendar request.",
    ) from exc


@router.get(
    "/calendars",
    response_model=CalendarListResponse,
)
async def list_calendars(
    response: Response,
    current_user: CurrentUser,
    connection_id: UUID,
    max_results: int = Query(default=100, ge=1, le=250),
    page_token: str | None = Query(default=None, max_length=4096),
    db: Session = Depends(get_db),
):
    """List calendars available to an owned Google connection."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await CalendarService(db).list_calendars(
            user_id=_get_user_id(current_user),
            connection_id=connection_id,
            max_results=max_results,
            page_token=page_token,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_calendar_error(exc)


@router.get(
    "/events",
    response_model=CalendarEventListResponse,
)
async def list_calendar_events(
    response: Response,
    current_user: CurrentUser,
    connection_id: UUID,
    calendar_id: str = Query(default="primary", max_length=1024),
    max_results: int = Query(default=100, ge=1, le=2500),
    page_token: str | None = Query(default=None, max_length=4096),
    time_min: datetime | None = None,
    time_max: datetime | None = None,
    query: str | None = Query(default=None, max_length=500),
    attendee: EmailStr | None = None,
    time_zone: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
):
    """List upcoming or filtered events from an owned Google calendar."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await CalendarService(db).list_events(
            user_id=_get_user_id(current_user),
            connection_id=connection_id,
            calendar_id=calendar_id,
            max_results=max_results,
            page_token=page_token,
            time_min=time_min,
            time_max=time_max,
            query=query,
            attendee=str(attendee) if attendee else None,
            time_zone=time_zone,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_calendar_error(exc)


@router.get("/events/{event_id}", response_model=CalendarEvent)
async def get_calendar_event(
    event_id: str,
    response: Response,
    current_user: CurrentUser,
    connection_id: UUID,
    calendar_id: str = Query(default="primary", max_length=1024),
    db: Session = Depends(get_db),
):
    """Retrieve one event from an owned Google calendar."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await CalendarService(db).get_event(
            user_id=_get_user_id(current_user),
            connection_id=connection_id,
            event_id=event_id,
            calendar_id=calendar_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_calendar_error(exc)


@router.get(
    "/schedule/today",
    response_model=CalendarScheduleResponse,
)
async def get_today_schedule(
    response: Response,
    current_user: CurrentUser,
    connection_id: UUID,
    time_zone: str = Query(..., max_length=100),
    calendar_id: str = Query(default="primary", max_length=1024),
    db: Session = Depends(get_db),
):
    """Retrieve today's schedule in the requested IANA timezone."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await CalendarService(db).get_today_schedule(
            user_id=_get_user_id(current_user),
            connection_id=connection_id,
            time_zone=time_zone,
            calendar_id=calendar_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_calendar_error(exc)


@router.get(
    "/schedule",
    response_model=CalendarScheduleResponse,
)
async def get_calendar_schedule(
    response: Response,
    current_user: CurrentUser,
    connection_id: UUID,
    schedule_date: date = Query(alias="date"),
    period: str = Query(default="day", pattern="^(day|week|month)$"),
    time_zone: str = Query(..., max_length=100),
    calendar_id: str = Query(default="primary", max_length=1024),
    page_token: str | None = Query(default=None, max_length=4096),
    db: Session = Depends(get_db),
):
    """Retrieve a daily, Monday-based weekly, or monthly schedule."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await CalendarService(db).get_schedule(
            user_id=_get_user_id(current_user),
            connection_id=connection_id,
            start_date=schedule_date,
            period=period,
            time_zone=time_zone,
            calendar_id=calendar_id,
            page_token=page_token,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_calendar_error(exc)


@router.post("/freebusy", response_model=FreeBusyResponse)
async def get_calendar_freebusy(
    payload: FreeBusyRequest,
    response: Response,
    current_user: CurrentUser,
    db: Session = Depends(get_db),
):
    """Query read-only Google FreeBusy availability."""
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return await CalendarService(db).query_freebusy(
            user_id=_get_user_id(current_user),
            connection_id=payload.connection_id,
            calendar_ids=payload.calendar_ids,
            time_min=payload.time_min,
            time_max=payload.time_max,
            time_zone=payload.time_zone,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _handle_calendar_error(exc)
