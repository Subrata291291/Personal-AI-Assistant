import re
from time import perf_counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.exceptions import GoogleAPIError
from app.integrations.google import scopes as google_scopes
from app.repositories.oauth_credential_repository import (
    OAuthCredentialRepository,
)
from app.repositories.user_connection_repository import (
    UserConnectionRepository,
)
from app.core.request_metrics import record_timing


class CalendarService:
    """Read-only Google Calendar operations for an owned Google account."""

    CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
    CALENDAR_EVENTS_READ_SCOPE = google_scopes.CALENDAR_EVENTS_READ_SCOPE
    CALENDAR_LIST_READ_SCOPE = google_scopes.CALENDAR_LIST_READ_SCOPE
    CALENDAR_FREEBUSY_SCOPE = google_scopes.CALENDAR_FREEBUSY_SCOPE
    EVENT_READ_SCOPES = google_scopes.CALENDAR_EVENT_READ_SCOPES
    CALENDAR_LIST_READ_SCOPES = google_scopes.CALENDAR_LIST_READ_SCOPES
    FREEBUSY_READ_SCOPES = google_scopes.CALENDAR_FREEBUSY_SCOPES

    MAX_LIST_RESULTS = 2500
    MAX_PAGE_TOKEN_LENGTH = 4096
    MAX_CALENDARS_PER_PAGE = 250
    _RESOURCE_ID = re.compile(r"[A-Za-z0-9._@+#-]{1,1024}\Z")
    _EMAIL = re.compile(r"[^@\s]{1,254}@[^@\s]{1,254}\Z")

    def __init__(self, db: Session):
        self.db = db
        self.connection_repository = UserConnectionRepository(db)
        self.credential_repository = OAuthCredentialRepository(db)
        self.api_client = GoogleAPIClient()

    def _validate_connection(
        self,
        user_id: UUID,
        connection_id: UUID,
        allowed_scopes: frozenset[str],
    ) -> None:
        connection = self.connection_repository.get_active_google_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if connection is None:
            raise ValueError("Connected Google account was not found.")

        credential = self.credential_repository.get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if credential is None:
            raise ValueError("Google OAuth credentials were not found.")

        if not (set(credential.scopes or []) & allowed_scopes):
            raise PermissionError(
                "Google Calendar permission has not been granted. "
                "Connect Calendar or grant its requested access first."
            )

    @classmethod
    def _validate_resource_id(
        cls,
        resource_id: str,
        resource_name: str,
    ) -> str:
        if (
            not isinstance(resource_id, str)
            or resource_id in {".", ".."}
            or not cls._RESOURCE_ID.fullmatch(resource_id)
        ):
            raise ValueError(f"Invalid {resource_name}.")
        return resource_id

    @classmethod
    def _validate_time_zone(cls, time_zone: str) -> ZoneInfo:
        if (
            not isinstance(time_zone, str)
            or not time_zone.strip()
        ):
            raise ValueError("An IANA time_zone is required.")
        if len(time_zone) > 100:
            raise ValueError("Invalid IANA time_zone.")
        try:
            return ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Invalid IANA time_zone.") from exc

    @staticmethod
    def _require_aware(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                f"{field_name} must include a UTC offset."
            )
        return value.astimezone(timezone.utc)

    @classmethod
    def _validate_range(
        cls,
        time_min: datetime,
        time_max: datetime,
    ) -> tuple[datetime, datetime]:
        start = cls._require_aware(time_min, "time_min")
        end = cls._require_aware(time_max, "time_max")
        if end <= start:
            raise ValueError("time_max must be later than time_min.")
        return start, end

    @staticmethod
    def _rfc3339(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        )

    @classmethod
    def _localize_provider_datetime(
        cls,
        value: datetime,
        zone_name: str,
    ) -> datetime:
        zone = cls._validate_time_zone(zone_name)
        candidates: dict[timedelta | None, datetime] = {}
        for fold in (0, 1):
            candidate = value.replace(tzinfo=zone, fold=fold)
            round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
            if round_trip.replace(tzinfo=None) == value:
                candidates[candidate.utcoffset()] = candidate
        if len(candidates) != 1:
            raise ValueError(
                "Provider returned an ambiguous or nonexistent local time."
            )
        return next(iter(candidates.values()))

    async def _google_get(
        self,
        user_id: UUID,
        connection_id: UUID,
        endpoint: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        api_db = SessionLocal()
        try:
            return await self.api_client.request(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="GET",
                url=f"{self.CALENDAR_API_BASE}/{endpoint}",
                params=params,
                allow_concurrent_read=True,
            )
        finally:
            api_db.close()

    async def _freebusy_query(
        self,
        user_id: UUID,
        connection_id: UUID,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        api_db = SessionLocal()
        try:
            return await self.api_client.request(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="POST",
                url=f"{self.CALENDAR_API_BASE}/freeBusy",
                json_data=body,
                allow_concurrent_read=True,
            )
        finally:
            api_db.close()

    @classmethod
    def _normalize_event_time(
        cls,
        value: Any,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned an invalid event time.",
            )

        all_day = value.get("date")
        date_time = value.get("dateTime")
        zone_name = value.get("timeZone")
        if all_day is not None:
            if not isinstance(all_day, str) or date_time is not None:
                raise GoogleAPIError(
                    status_code=502,
                    message="Google Calendar returned an invalid all-day event.",
                )
            try:
                parsed_date = date.fromisoformat(all_day)
            except ValueError as exc:
                raise GoogleAPIError(
                    status_code=502,
                    message="Google Calendar returned an invalid all-day event.",
                ) from exc
            return {
                "date": parsed_date,
                "date_time": None,
                "time_zone": None,
            }

        if not isinstance(date_time, str):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned an invalid event time.",
            )
        try:
            parsed = datetime.fromisoformat(
                date_time[:-1] + "+00:00"
                if date_time.endswith("Z")
                else date_time
            )
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                if not isinstance(zone_name, str):
                    raise ValueError("Missing timezone for local event time.")
                parsed = cls._localize_provider_datetime(parsed, zone_name)
            elif zone_name is not None:
                cls._validate_time_zone(zone_name)
        except (ValueError, TypeError) as exc:
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned an invalid event timezone.",
            ) from exc

        return {
            "date": None,
            "date_time": parsed.astimezone(timezone.utc),
            "time_zone": zone_name,
        }

    @classmethod
    def _normalize_event(cls, raw: Any) -> dict[str, Any]:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("id"), str)
            or not raw["id"].strip()
        ):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned an invalid event.",
            )

        attendees = raw.get("attendees", [])
        if (
            not isinstance(attendees, list)
            or any(not isinstance(attendee, dict) for attendee in attendees)
        ):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned invalid event attendees.",
            )
        normalized_attendees = [
            {
                "email": attendee.get("email"),
                "display_name": attendee.get("displayName"),
                "response_status": attendee.get("responseStatus"),
                "self": attendee.get("self"),
                "organizer": attendee.get("organizer"),
            }
            for attendee in attendees
        ]

        recurrence = raw.get("recurrence")
        if recurrence is not None and (
            not isinstance(recurrence, list)
            or any(not isinstance(rule, str) for rule in recurrence)
        ):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned invalid event recurrence.",
            )
        return {
            "id": raw["id"],
            "summary": raw.get("summary"),
            "description": raw.get("description"),
            "location": raw.get("location"),
            "status": raw.get("status"),
            "etag": raw.get("etag"),
            "html_link": raw.get("htmlLink"),
            "start": cls._normalize_event_time(raw.get("start")),
            "end": cls._normalize_event_time(raw.get("end")),
            "attendees": normalized_attendees,
            "recurrence": recurrence if isinstance(recurrence, list) else None,
            "recurring_event_id": raw.get("recurringEventId"),
            "original_start_time": cls._normalize_event_time(
                raw.get("originalStartTime")
            ),
            "updated": raw.get("updated"),
        }

    @staticmethod
    def _event_overlaps_range(
        event: dict[str, Any],
        range_start: datetime,
        range_end: datetime,
        zone: ZoneInfo,
    ) -> bool:
        start = event.get("start")
        end = event.get("end")
        if not isinstance(start, dict):
            # Preserve compatibility for provider/test data that lacks timing
            # metadata; recognized timed and all-day events are filtered below.
            return True

        all_day_start = start.get("date")
        if isinstance(all_day_start, date):
            all_day_end = (
                end.get("date")
                if isinstance(end, dict)
                else None
            )
            if not isinstance(all_day_end, date):
                all_day_end = all_day_start + timedelta(days=1)
            local_start_date = range_start.astimezone(zone).date()
            local_end_date = range_end.astimezone(zone).date()
            return (
                all_day_start < local_end_date
                and all_day_end > local_start_date
            )

        event_start = start.get("date_time")
        event_end = (
            end.get("date_time")
            if isinstance(end, dict)
            else None
        )
        if not isinstance(event_start, datetime):
            return False
        if event_start.tzinfo is None or event_start.utcoffset() is None:
            return False
        event_start_utc = event_start.astimezone(timezone.utc)
        if isinstance(event_end, datetime) and (
            event_end.tzinfo is not None
            and event_end.utcoffset() is not None
        ):
            event_end_utc = event_end.astimezone(timezone.utc)
        else:
            event_end_utc = event_start_utc
        return event_start_utc < range_end and event_end_utc > range_start

    async def list_events(
        self,
        user_id: UUID,
        connection_id: UUID,
        calendar_id: str = "primary",
        max_results: int = 100,
        page_token: str | None = None,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        query: str | None = None,
        attendee: str | None = None,
        time_zone: str | None = None,
    ) -> dict[str, Any]:
        service_started_at = perf_counter()
        self._validate_connection(
            user_id,
            connection_id,
            self.EVENT_READ_SCOPES,
        )
        calendar_id = self._validate_resource_id(calendar_id, "calendar_id")
        if not 1 <= max_results <= self.MAX_LIST_RESULTS:
            raise ValueError(
                f"max_results must be between 1 and {self.MAX_LIST_RESULTS}."
            )
        if page_token is not None and (
            not page_token
            or len(page_token) > self.MAX_PAGE_TOKEN_LENGTH
            or any(ord(char) < 32 for char in page_token)
        ):
            raise ValueError("Invalid page_token.")
        if attendee is not None and (
            len(attendee) > 254
            or not self._EMAIL.fullmatch(attendee.strip())
        ):
            raise ValueError("Invalid attendee email.")
        if time_zone is not None:
            self._validate_time_zone(time_zone)
        if time_min is None:
            time_min = datetime.now(timezone.utc)
        else:
            time_min = self._require_aware(time_min, "time_min")
        if time_max is not None:
            time_max = self._require_aware(time_max, "time_max")
            if time_max <= time_min:
                raise ValueError("time_max must be later than time_min.")

        params: dict[str, Any] = {
            "maxResults": max_results,
            "timeMin": self._rfc3339(time_min),
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if time_max is not None:
            params["timeMax"] = self._rfc3339(time_max)
        if page_token:
            params["pageToken"] = page_token
        if query and query.strip():
            params["q"] = query.strip()
        if time_zone:
            params["timeZone"] = time_zone

        endpoint = (
            f"calendars/{quote(calendar_id, safe='')}/events"
        )
        result = await self._google_get(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=endpoint,
            params=params,
        )
        if not isinstance(result, dict):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned an invalid event list.",
            )

        raw_events = result.get("items", [])
        if not isinstance(raw_events, list):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned an invalid event list.",
            )
        if any(not isinstance(event, dict) for event in raw_events):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned an invalid event list.",
            )

        next_page_token = result.get("nextPageToken")
        if "nextPageToken" in result and (
            not isinstance(next_page_token, str)
            or not next_page_token
            or len(next_page_token) > self.MAX_PAGE_TOKEN_LENGTH
            or any(ord(char) < 32 for char in next_page_token)
        ):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned invalid pagination metadata.",
            )

        response_time_zone = result.get("timeZone")
        if "timeZone" in result:
            try:
                self._validate_time_zone(response_time_zone)
            except ValueError as exc:
                raise GoogleAPIError(
                    status_code=502,
                    message="Google Calendar returned invalid event timezone.",
                ) from exc

        events = [self._normalize_event(event) for event in raw_events]
        if time_max is not None:
            filter_zone = ZoneInfo(time_zone or "UTC")
            events = [
                event
                for event in events
                if self._event_overlaps_range(
                    event,
                    time_min,
                    time_max,
                    filter_zone,
                )
            ]
        if attendee:
            normalized_attendee = attendee.strip().casefold()
            events = [
                event
                for event in events
                if any(
                    (item.get("email") or "").casefold()
                    == normalized_attendee
                    for item in event["attendees"]
                )
            ]
        record_timing(
            "calendar_service",
            (perf_counter() - service_started_at) * 1000,
        )
        return {
            "calendar_id": calendar_id,
            "events": events,
            "next_page_token": next_page_token,
            "time_zone": response_time_zone or time_zone,
        }

    async def get_event(
        self,
        user_id: UUID,
        connection_id: UUID,
        event_id: str,
        calendar_id: str = "primary",
    ) -> dict[str, Any]:
        self._validate_connection(
            user_id,
            connection_id,
            self.EVENT_READ_SCOPES,
        )
        event_id = self._validate_resource_id(event_id, "event_id")
        calendar_id = self._validate_resource_id(calendar_id, "calendar_id")
        result = await self._google_get(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=(
                f"calendars/{quote(calendar_id, safe='')}/events/"
                f"{quote(event_id, safe='')}"
            ),
            params={},
        )
        return self._normalize_event(result)

    async def list_calendars(
        self,
        user_id: UUID,
        connection_id: UUID,
        max_results: int = 100,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        service_started_at = perf_counter()
        self._validate_connection(
            user_id,
            connection_id,
            self.CALENDAR_LIST_READ_SCOPES,
        )
        if not 1 <= max_results <= self.MAX_CALENDARS_PER_PAGE:
            raise ValueError(
                "max_results must be between 1 and "
                f"{self.MAX_CALENDARS_PER_PAGE}."
            )
        if page_token is not None and (
            not page_token
            or len(page_token) > self.MAX_PAGE_TOKEN_LENGTH
            or any(ord(char) < 32 for char in page_token)
        ):
            raise ValueError("Invalid page_token.")

        params: dict[str, Any] = {"maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        result = await self._google_get(
            user_id=user_id,
            connection_id=connection_id,
            endpoint="users/me/calendarList",
            params=params,
        )
        if not isinstance(result, dict):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned an invalid calendar list.",
            )
        items = result.get("items", [])
        if (
            not isinstance(items, list)
            or any(not isinstance(item, dict) for item in items)
        ):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned an invalid calendar list.",
            )
        next_page_token = result.get("nextPageToken")
        if "nextPageToken" in result and (
            not isinstance(next_page_token, str)
            or not next_page_token
            or len(next_page_token) > self.MAX_PAGE_TOKEN_LENGTH
            or any(ord(char) < 32 for char in next_page_token)
        ):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned invalid pagination metadata.",
            )

        calendars = []
        for item in items:
            calendar_id = item.get("id")
            summary = item.get("summary")
            if (
                not isinstance(calendar_id, str)
                or not calendar_id.strip()
                or not isinstance(summary, str)
                or not summary.strip()
            ):
                raise GoogleAPIError(
                    status_code=502,
                    message="Google Calendar returned an invalid calendar.",
                )
            self._validate_resource_id(calendar_id, "calendar_id")
            primary = item.get("primary")
            access_role = item.get("accessRole")
            time_zone = item.get("timeZone")
            if (
                primary is not None
                and not isinstance(primary, bool)
                or access_role is not None
                and not isinstance(access_role, str)
                or time_zone is not None
                and not isinstance(time_zone, str)
            ):
                raise GoogleAPIError(
                    status_code=502,
                    message="Google Calendar returned invalid calendar metadata.",
                )
            if time_zone is not None:
                try:
                    self._validate_time_zone(time_zone)
                except ValueError as exc:
                    raise GoogleAPIError(
                        status_code=502,
                        message="Google Calendar returned an invalid calendar timezone.",
                    ) from exc
            calendars.append({
                "id": calendar_id,
                "summary": summary,
                "primary": primary,
                "access_role": access_role,
                "time_zone": time_zone,
            })
        record_timing(
            "calendar_service",
            (perf_counter() - service_started_at) * 1000,
        )
        return {
            "calendars": calendars,
            "next_page_token": next_page_token,
        }

    async def validate_create_event_access(
        self,
        user_id: UUID,
        connection_id: UUID,
        calendar_id: str,
    ) -> None:
        self._validate_connection(
            user_id,
            connection_id,
            frozenset({google_scopes.CALENDAR_EVENTS_WRITE_SCOPE}),
        )
        calendar_id = self._validate_resource_id(
            calendar_id,
            "calendar_id",
        )
        await self._google_get(
            user_id=user_id,
            connection_id=connection_id,
            endpoint=(
                f"calendars/{quote(calendar_id, safe='')}/events"
            ),
            params={"maxResults": 1},
        )

    async def validate_event_write_access(
        self,
        user_id: UUID,
        connection_id: UUID,
        calendar_id: str,
        event_id: str,
    ) -> dict[str, Any]:
        self._validate_connection(
            user_id,
            connection_id,
            frozenset({google_scopes.CALENDAR_EVENTS_WRITE_SCOPE}),
        )
        calendar_id = self._validate_resource_id(
            calendar_id,
            "calendar_id",
        )
        event_id = self._validate_resource_id(event_id, "event_id")
        event = await self.get_event(
            user_id=user_id,
            connection_id=connection_id,
            event_id=event_id,
            calendar_id=calendar_id,
        )
        if not isinstance(event.get("etag"), str) or not event["etag"]:
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar event has no concurrency token.",
            )
        return event

    async def get_schedule(
        self,
        user_id: UUID,
        connection_id: UUID,
        start_date: date,
        period: str,
        time_zone: str,
        calendar_id: str = "primary",
        page_token: str | None = None,
    ) -> dict[str, Any]:
        zone = self._validate_time_zone(time_zone)
        if period not in {"day", "week", "month"}:
            raise ValueError("period must be 'day', 'week', or 'month'.")
        if period == "week":
            start_date = start_date - timedelta(days=start_date.weekday())
            day_count = 7
            end_date = start_date + timedelta(days=day_count)
        elif period == "month":
            start_date = start_date.replace(day=1)
            end_date = (
                start_date.replace(day=28) + timedelta(days=4)
            ).replace(day=1)
        else:
            day_count = 1
            end_date = start_date + timedelta(days=day_count)
        local_start = datetime.combine(start_date, time.min, tzinfo=zone)
        local_end = datetime.combine(
            end_date,
            time.min,
            tzinfo=zone,
        )
        events_result = await self.list_events(
            user_id=user_id,
            connection_id=connection_id,
            calendar_id=calendar_id,
            max_results=self.MAX_LIST_RESULTS,
            time_min=local_start,
            time_max=local_end,
            time_zone=time_zone,
            page_token=page_token,
        )
        events = events_result["events"]
        return {
            "calendar_id": events_result["calendar_id"],
            "date": start_date,
            "period": period,
            "time_zone": time_zone,
            "events": events,
            "conflicts": self.find_conflicts(events, zone),
            "conflicts_complete": events_result["next_page_token"] is None,
            "next_page_token": events_result["next_page_token"],
        }

    async def get_today_schedule(
        self,
        user_id: UUID,
        connection_id: UUID,
        time_zone: str,
        calendar_id: str = "primary",
        request_time: datetime | None = None,
    ) -> dict[str, Any]:
        zone = self._validate_time_zone(time_zone)
        reference_time = request_time or datetime.now(timezone.utc)
        reference_time = self._require_aware(
            reference_time,
            "request_time",
        )
        return await self.get_schedule(
            user_id=user_id,
            connection_id=connection_id,
            start_date=reference_time.astimezone(zone).date(),
            period="day",
            time_zone=zone.key,
            calendar_id=calendar_id,
        )

    async def query_freebusy(
        self,
        user_id: UUID,
        connection_id: UUID,
        calendar_ids: list[str],
        time_min: datetime,
        time_max: datetime,
        time_zone: str,
    ) -> dict[str, Any]:
        self._validate_connection(
            user_id,
            connection_id,
            self.FREEBUSY_READ_SCOPES,
        )
        zone = self._validate_time_zone(time_zone)
        if not calendar_ids or len(calendar_ids) > 50:
            raise ValueError("calendar_ids must contain between 1 and 50 IDs.")
        calendar_ids = [
            self._validate_resource_id(calendar_id, "calendar_id")
            for calendar_id in calendar_ids
        ]
        start, end = self._validate_range(time_min, time_max)
        result = await self._freebusy_query(
            user_id=user_id,
            connection_id=connection_id,
            body={
                "timeMin": self._rfc3339(start),
                "timeMax": self._rfc3339(end),
                "timeZone": zone.key,
                "items": [{"id": calendar_id} for calendar_id in calendar_ids],
            },
        )
        raw_calendars = result.get("calendars") or {}
        if not isinstance(raw_calendars, dict):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned an invalid FreeBusy response.",
            )
        calendars: dict[str, Any] = {}
        for calendar_id, item in raw_calendars.items():
            if not isinstance(item, dict):
                raise GoogleAPIError(
                    status_code=502,
                    message="Google Calendar returned an invalid FreeBusy calendar.",
                )
            busy = item.get("busy") or []
            errors = item.get("errors") or []
            if not isinstance(busy, list) or not isinstance(errors, list):
                raise GoogleAPIError(
                    status_code=502,
                    message="Google Calendar returned invalid FreeBusy details.",
                )
            normalized_busy = []
            for interval in busy:
                if not isinstance(interval, dict):
                    raise GoogleAPIError(
                        status_code=502,
                        message="Google Calendar returned an invalid busy interval.",
                    )
                busy_start = self._parse_rfc3339(interval.get("start"))
                busy_end = self._parse_rfc3339(interval.get("end"))
                if busy_end <= busy_start:
                    raise GoogleAPIError(
                        status_code=502,
                        message="Google Calendar returned an invalid busy interval.",
                    )
                normalized_busy.append({
                    "start": busy_start,
                    "end": busy_end,
                })
            normalized_errors = []
            for error in errors:
                if not isinstance(error, dict):
                    raise GoogleAPIError(
                        status_code=502,
                        message="Google Calendar returned invalid FreeBusy errors.",
                    )
                domain = error.get("domain")
                reason = error.get("reason")
                if (
                    domain is not None
                    and not isinstance(domain, str)
                    or reason is not None
                    and not isinstance(reason, str)
                ):
                    raise GoogleAPIError(
                        status_code=502,
                        message="Google Calendar returned invalid FreeBusy errors.",
                    )
                normalized_errors.append({
                    "domain": domain,
                    "reason": reason,
                })
            calendars[calendar_id] = {
                "busy": normalized_busy,
                "errors": normalized_errors,
            }
        return {
            "time_min": start,
            "time_max": end,
            "time_zone": zone.key,
            "calendars": calendars,
        }

    @staticmethod
    def _parse_rfc3339(value: Any) -> datetime:
        if not isinstance(value, str):
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned an invalid busy interval.",
            )
        try:
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith("Z") else value
            )
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("Missing UTC offset.")
            return parsed.astimezone(timezone.utc)
        except ValueError as exc:
            raise GoogleAPIError(
                status_code=502,
                message="Google Calendar returned an invalid busy interval.",
            ) from exc

    @classmethod
    def find_conflicts(
        cls,
        events: list[dict[str, Any]],
        time_zone: ZoneInfo,
    ) -> list[dict[str, str]]:
        intervals: list[tuple[datetime, datetime, str]] = []
        for event in events:
            start = cls._event_boundary(event.get("start"), time_zone)
            end = cls._event_boundary(event.get("end"), time_zone)
            if start is not None and end is not None and end > start:
                intervals.append((start, end, event["id"]))

        intervals.sort(key=lambda item: item[0])
        conflicts: list[dict[str, str]] = []
        for index, (start, end, event_id) in enumerate(intervals):
            for other_start, other_end, other_id in intervals[index + 1:]:
                if other_start >= end:
                    break
                overlap_start = max(start, other_start)
                overlap_end = min(end, other_end)
                if overlap_start < overlap_end:
                    conflicts.append({
                        "event_id": event_id,
                        "conflicting_event_id": other_id,
                        "overlap_start": cls._rfc3339(overlap_start),
                        "overlap_end": cls._rfc3339(overlap_end),
                    })
        return conflicts

    @classmethod
    def _event_boundary(
        cls,
        event_time: dict[str, Any] | None,
        zone: ZoneInfo,
    ) -> datetime | None:
        if not event_time:
            return None
        if event_time.get("date_time") is not None:
            value = event_time["date_time"]
            return cls._require_aware(value, "event time")
        if event_time.get("date") is not None:
            event_date = event_time["date"]
            if isinstance(event_date, str):
                event_date = date.fromisoformat(event_date)
            if isinstance(event_date, date):
                return datetime.combine(event_date, time.min, tzinfo=zone).astimezone(
                    timezone.utc
                )
        return None
