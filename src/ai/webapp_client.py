import base64
import hashlib
import hmac
import logging
import time
import uuid

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import API_PREFIX, INTERNAL_API_BASE_URL, PROFESSOR_BOT_TOKEN


class WebappBotApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: Any = None,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details
        self.request_id = request_id


class BotUser(BaseModel):
    model_config = ConfigDict(extra="allow")

    tg_id: int
    tg_ref_id: int | None = None
    tg_phone: str | None = None
    photo_url: str | None = None
    contact_id: int | None = None
    premium_requests: float = 0
    premium_until: datetime | None = None
    conversation_id: str | None = None
    last_used: str = "professor"
    input_tokens: int = 0
    output_tokens: int = 0
    blocked_until: datetime | None = None
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    utm_creative: str | None = None
    utm_payload_raw: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    name: str = ""
    surname: str = ""
    full_name: str = ""
    email: str = ""
    phone: str = ""
    contact_info: str = ""


class BotFeature(BaseModel):
    onec_id: str
    product_onec_id: str
    name: str
    code: str
    file_id: str | None = None
    price: Decimal
    balance: int


class BotProduct(BaseModel):
    id: int
    onec_id: str
    name: str
    code: str
    description: str | None = None
    usage: str | None = None
    expiration: str | None = None
    category_onec_id: str | None = None
    features: list[BotFeature] = Field(default_factory=list)


class BotUsedCode(BaseModel):
    id: int
    user_id: int
    code: str
    price: Decimal


class BotPromo(BaseModel):
    id: int
    code: str
    discount_pct: Decimal
    owner_name: str
    owner_pct: Decimal
    owner_amount_gained: Decimal
    lvl1_name: str | None = None
    lvl1_pct: Decimal
    lvl1_amount_gained: Decimal
    lvl2_name: str | None = None
    lvl2_pct: Decimal
    lvl2_amount_gained: Decimal
    times_used: int
    created_at: datetime
    updated_at: datetime


class BotCart(BaseModel):
    id: int
    user_id: int
    name: str | None = None
    phone: str
    email: str
    sum: Decimal
    delivery_sum: Decimal
    promo_code: str | None = None
    promo_gains: Decimal
    promo_gains_given: bool
    delivery_string: str
    commentary: str | None = None
    payment_method: str | None = None
    payment_provider: str | None = None
    payment_status: str | None = None
    payment_invoice_id: str | None = None
    payment_paid_at: datetime | None = None
    amocrm_lead_id: int | None = None
    delivery_created_at: datetime | None = None
    delivery_provider_ref: str | None = None
    is_active: bool
    is_paid: bool
    is_canceled: bool
    is_shipped: bool
    status: str | None = None
    yandex_request_id: str | None = None
    created_at: datetime
    updated_at: datetime
    user: BotUser | None = None


class BotBooleanResult(BaseModel):
    ok: bool


class BotIdResult(BaseModel):
    id: int


class BotTotalRequestsResult(BaseModel):
    total_requests: int


class BotUsageReportResult(BaseModel):
    period_label: str
    usages: list[dict[str, Any]]


class BotUserUsageTotalsResult(BaseModel):
    period: str
    user_id: int
    tg_phone: str | None = None
    by_bot: list[dict[str, Any]]
    totals: dict[str, Any]


class BotUtmFunnelRow(BaseModel):
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    utm_creative: str | None = None
    registrations: int = 0
    verified_users: int = 0
    paid_users: int = 0
    paid_orders: int = 0
    goods_revenue: float = 0.0
    delivery_revenue: float = 0.0
    total_revenue: float = 0.0
    ai_total_requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    ai_total_cost_usd: float = 0.0


class BotUtmFunnelUser(BaseModel):
    tg_id: int
    tg_phone: str | None = None
    created_at: datetime
    updated_at: datetime
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    utm_creative: str | None = None
    utm_payload_raw: str | None = None
    verified: bool = False
    paid_orders: int = 0
    goods_revenue: float = 0.0
    delivery_revenue: float = 0.0
    total_revenue: float = 0.0
    ai_total_requests: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    ai_total_cost_usd: float = 0.0


class BotUtmFunnelReportResult(BaseModel):
    period_label: str
    rows: list[BotUtmFunnelRow]
    users: list[BotUtmFunnelUser]


class BotSearchUsersResult(BaseModel):
    rows: list[BotUser]
    total: int


class BotSearchCartsResult(BaseModel):
    rows: list[BotCart]
    total: int


class BotTextResult(BaseModel):
    text: str


BotVerifyPrice = int | None | Literal["old", "not_found", "low"]


class BotVerifyOrderResult(BaseModel):
    status: Literal["ok", "not_found", "no_email", "smtp_failed", "low"]
    price: BotVerifyPrice
    email: str | None = None
    verification_code: str | None = None


logger = logging.getLogger("webapp_client")
SLOW_REQUEST_THRESHOLD_MS = 3000


def _internal_base_url() -> str:
    if INTERNAL_API_BASE_URL:
        return INTERNAL_API_BASE_URL.rstrip("/")
    raise WebappBotApiError("INTERNAL_API_BASE_URL is not configured")


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    repeats = (len(data) + len(key) - 1) // len(key)
    key_stream = (key * repeats)[:len(data)]
    return bytes(a ^ b for a, b in zip(data, key_stream))


def _derive_encryption_key(bot_token: str, timestamp: str, nonce: str) -> bytes:
    seed = f"{timestamp}:{nonce}:{bot_token}:bot-auth-v1".encode("utf-8")
    return hashlib.sha256(seed).digest()


def _encrypt_token(bot_token: str, timestamp: str, nonce: str) -> str:
    if not bot_token:
        raise WebappBotApiError("PROFESSOR_BOT_TOKEN is not configured")
    token_bytes = bot_token.encode("utf-8")
    key = _derive_encryption_key(bot_token, timestamp, nonce)
    encrypted = _xor_bytes(token_bytes, key)
    return base64.urlsafe_b64encode(encrypted).decode("ascii")


def _auth_headers() -> dict[str, str]:
    bot_token = PROFESSOR_BOT_TOKEN
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    token_enc = _encrypt_token(bot_token, timestamp, nonce)
    payload = f"{timestamp}:{nonce}:{token_enc}"
    signature = hmac.new(bot_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-Bot-Timestamp": timestamp,
        "X-Bot-Nonce": nonce,
        "X-Bot-Token-Enc": token_enc,
        "X-Bot-Signature": signature,
    }


def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump(exclude_unset=True))
    if hasattr(value, "dict"):
        return _to_jsonable(value.dict(exclude_unset=True))
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


def _parse_error_response(resp: httpx.Response) -> tuple[str, str | None, Any, str | None]:
    request_id = resp.headers.get("X-Request-ID")
    try:
        data = resp.json()
    except Exception:
        text = (resp.text or "").strip() or f"HTTP {resp.status_code}"
        return text, None, None, request_id

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            request_id = data.get("request_id") or request_id
            message = str(error.get("message") or error.get("code") or f"HTTP {resp.status_code}")
            return message, error.get("code"), error.get("details"), request_id
        detail = data.get("detail")
        if isinstance(detail, str):
            return detail, None, None, request_id
        if detail is not None:
            return f"HTTP {resp.status_code}", None, detail, request_id

    return (resp.text or "").strip() or f"HTTP {resp.status_code}", None, None, request_id


def _model_validate(model_cls, data: Any, *, operation: str):
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise WebappBotApiError(
            f"Invalid response payload for {operation}",
            code="invalid_response",
            details=exc.errors(),
        ) from exc


class WebappBotClient:
    def __init__(self, timeout_seconds: float = 8.0, connect_timeout_seconds: float = 3.0):
        self.internal_prefix = f"{API_PREFIX}/internal"
        self.timeout = httpx.Timeout(timeout=timeout_seconds, connect=connect_timeout_seconds)

    def _url(self, path: str) -> str:
        return f"{_internal_base_url()}{self.internal_prefix}{path}"

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        request_id = uuid.uuid4().hex[:10]
        started = time.monotonic()
        payload_jsonable = _to_jsonable(json_body) if json_body is not None else None
        payload_keys = ",".join(sorted(payload_jsonable.keys())) if isinstance(payload_jsonable, dict) else "none"
        logger.info(
            "Internal API start | id=%s | operation=%s | method=%s | payload_keys=[%s]",
            request_id,
            operation,
            method,
            payload_keys,
        )

        headers = _auth_headers()
        headers["X-Request-ID"] = request_id
        request_kwargs: dict[str, Any] = {"headers": headers}
        if payload_jsonable is not None:
            request_kwargs["json"] = payload_jsonable
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.request(method, self._url(path), **request_kwargs)
        except httpx.TimeoutException as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "Internal API timeout | id=%s | operation=%s | elapsed_ms=%d | err=%s",
                request_id,
                operation,
                elapsed_ms,
                exc,
            )
            raise WebappBotApiError(f"Internal API timeout for {operation}: {exc}") from exc
        except httpx.HTTPError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "Internal API transport error | id=%s | operation=%s | elapsed_ms=%d | err=%s",
                request_id,
                operation,
                elapsed_ms,
                exc,
            )
            raise WebappBotApiError(f"Internal API transport error for {operation}: {exc}") from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            message, code, details, response_request_id = _parse_error_response(resp)
            logger.warning(
                "Internal API http error | id=%s | operation=%s | status=%d | elapsed_ms=%d | code=%s | message=%r",
                request_id,
                operation,
                resp.status_code,
                elapsed_ms,
                code,
                message,
            )
            if response_request_id:
                message = f"{message} (request_id={response_request_id})"
            raise WebappBotApiError(
                message,
                status_code=resp.status_code,
                code=code,
                details=details,
                request_id=response_request_id,
            )

        try:
            data = resp.json()
        except Exception as exc:
            logger.warning(
                "Internal API invalid JSON | id=%s | operation=%s | status=%d | elapsed_ms=%d",
                request_id,
                operation,
                resp.status_code,
                elapsed_ms,
            )
            raise WebappBotApiError(f"Internal API returned invalid JSON for {operation}") from exc

        if elapsed_ms >= SLOW_REQUEST_THRESHOLD_MS:
            logger.warning(
                "Internal API slow | id=%s | operation=%s | status=%d | elapsed_ms=%d",
                request_id,
                operation,
                resp.status_code,
                elapsed_ms,
            )
        else:
            logger.info(
                "Internal API ok | id=%s | operation=%s | status=%d | elapsed_ms=%d",
                request_id,
                operation,
                resp.status_code,
                elapsed_ms,
            )

        return data

    async def get_user(self, column_name: str, raw_value: Any) -> BotUser | None:
        data = await self._request_json(
            "POST",
            "/users/lookup",
            operation="get_user",
            json_body={"column_name": column_name, "raw_value": raw_value},
        )
        return None if data is None else _model_validate(BotUser, data, operation="get_user")

    async def get_users(self) -> list[BotUser]:
        data = await self._request_json("GET", "/users", operation="get_users")
        return [_model_validate(BotUser, row, operation="get_users") for row in (data or [])]

    async def upsert_user(self, data: Any) -> BotUser:
        payload = await self._request_json("POST", "/users", operation="upsert_user", json_body=data)
        return _model_validate(BotUser, payload, operation="upsert_user")

    async def update_user(self, tg_id: int, data: Any) -> BotUser | None:
        payload = await self._request_json(
            "PATCH",
            f"/users/{tg_id}",
            operation="update_user",
            json_body=data,
        )
        return None if payload is None else _model_validate(BotUser, payload, operation="update_user")

    async def update_user_name(self, tg_id: int, first_name: str | None = None, last_name: str | None = None) -> bool:
        payload = await self._request_json(
            "PATCH",
            f"/users/{tg_id}/name",
            operation="update_user_name",
            json_body={"first_name": first_name, "last_name": last_name},
        )
        result = _model_validate(BotBooleanResult, payload, operation="update_user_name")
        return result.ok

    async def increment_tokens(self, tg_id: int, input_inc: int = 0, output_inc: int = 0) -> bool:
        payload = await self._request_json(
            "POST",
            "/usage/tokens/increment",
            operation="increment_tokens",
            json_body={"tg_id": tg_id, "input_inc": input_inc, "output_inc": output_inc},
        )
        result = _model_validate(BotBooleanResult, payload, operation="increment_tokens")
        return result.ok

    async def write_usage(
        self,
        user_id: int,
        input_tokens: int,
        output_tokens: int,
        bot: str,
        usage_date: date | None = None,
        cached_input_tokens: int | None = None,
    ) -> BotIdResult:
        payload = await self._request_json(
            "POST",
            "/usage/entries",
            operation="write_usage",
            json_body={
                "user_id": user_id,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "bot": bot,
                "usage_date": usage_date,
            },
        )
        return _model_validate(BotIdResult, payload, operation="write_usage")

    async def get_user_total_requests(self, user_id: int, bots: list[str] | tuple[str, ...] | None = None) -> int:
        payload = await self._request_json(
            "POST",
            "/usage/total-requests",
            operation="get_user_total_requests",
            json_body={"user_id": user_id, "bots": list(bots) if bots else None},
        )
        result = _model_validate(BotTotalRequestsResult, payload, operation="get_user_total_requests")
        return result.total_requests

    async def get_usages(self, start_date: date, end_date: date | None = None, bot: str | None = None) -> tuple[str, list[dict[str, Any]]]:
        payload = await self._request_json(
            "POST",
            "/usage/report",
            operation="get_usages",
            json_body={"start_date": start_date, "end_date": end_date, "bot": bot},
        )
        result = _model_validate(BotUsageReportResult, payload, operation="get_usages")
        return result.period_label, result.usages

    async def get_user_usage_totals(
        self,
        user_id: int,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> BotUserUsageTotalsResult:
        payload = await self._request_json(
            "POST",
            "/usage/user-totals",
            operation="get_user_usage_totals",
            json_body={"user_id": user_id, "start_date": start_date, "end_date": end_date},
        )
        return _model_validate(BotUserUsageTotalsResult, payload, operation="get_user_usage_totals")

    async def get_utm_funnel_report(self, start_date: date, end_date: date) -> BotUtmFunnelReportResult:
        payload = await self._request_json(
            "POST",
            "/reports/utm-funnel",
            operation="get_utm_funnel_report",
            json_body={"start_date": start_date, "end_date": end_date},
        )
        return _model_validate(BotUtmFunnelReportResult, payload, operation="get_utm_funnel_report")

    async def get_product_with_features(self, onec_id: str) -> BotProduct | None:
        payload = await self._request_json("GET", f"/catalog/products/{onec_id}", operation="get_product_with_features")
        return None if payload is None else _model_validate(BotProduct, payload, operation="get_product_with_features")

    async def get_used_code_by_code(self, code: str) -> BotUsedCode | None:
        payload = await self._request_json("GET", f"/catalog/used-codes/{code}", operation="get_used_code_by_code")
        return None if payload is None else _model_validate(BotUsedCode, payload, operation="get_used_code_by_code")

    async def create_used_code(self, data: Any) -> BotUsedCode:
        payload = await self._request_json("POST", "/catalog/used-codes", operation="create_used_code", json_body=data)
        return _model_validate(BotUsedCode, payload, operation="create_used_code")

    async def list_promos(self) -> list[BotPromo]:
        payload = await self._request_json("GET", "/catalog/promos", operation="list_promos")
        return [_model_validate(BotPromo, row, operation="list_promos") for row in (payload or [])]

    async def get_carts(self, exclude_starting: bool = True) -> list[BotCart]:
        payload = await self._request_json(
            "POST",
            "/carts/list",
            operation="get_carts",
            json_body={"exclude_starting": exclude_starting},
        )
        return [_model_validate(BotCart, row, operation="get_carts") for row in (payload or [])]

    async def get_user_carts(self, user_id: int, is_active: bool | None = None, exclude_starting: bool = True) -> list[BotCart]:
        payload = await self._request_json(
            "POST",
            "/carts/by-user",
            operation="get_user_carts",
            json_body={"user_id": user_id, "is_active": is_active, "exclude_starting": exclude_starting},
        )
        return [_model_validate(BotCart, row, operation="get_user_carts") for row in (payload or [])]

    async def get_carts_by_date(self, dt: datetime) -> list[BotCart]:
        payload = await self._request_json(
            "POST",
            "/carts/by-date",
            operation="get_carts_by_date",
            json_body={"dt": dt},
        )
        return [_model_validate(BotCart, row, operation="get_carts_by_date") for row in (payload or [])]

    async def get_cart_by_id(self, cart_id: int) -> BotCart | None:
        payload = await self._request_json("GET", f"/carts/{cart_id}", operation="get_cart_by_id")
        return None if payload is None else _model_validate(BotCart, payload, operation="get_cart_by_id")

    async def search_users(self, by: str, value: Any, page: int | None = None, limit: int | None = None) -> tuple[list[BotUser], int]:
        payload = await self._request_json(
            "POST",
            "/users/search",
            operation="search_users",
            json_body={"by": by, "value": value, "page": page, "limit": limit},
        )
        result = _model_validate(BotSearchUsersResult, payload, operation="search_users")
        return result.rows, result.total

    async def search_carts(self, value: Any, page: int | None = None, limit: int | None = None) -> tuple[list[BotCart], int]:
        payload = await self._request_json(
            "POST",
            "/carts/search",
            operation="search_carts",
            json_body={"value": value, "page": page, "limit": limit},
        )
        result = _model_validate(BotSearchCartsResult, payload, operation="search_carts")
        return result.rows, result.total

    async def user_carts_analytics_text(self, user_id: int, days: int = 30, top_n: int = 5, recent_n: int = 8) -> str:
        payload = await self._request_json(
            "POST",
            "/carts/analytics/user-carts",
            operation="user_carts_analytics_text",
            json_body={"user_id": user_id, "days": days, "top_n": top_n, "recent_n": recent_n},
        )
        result = _model_validate(BotTextResult, payload, operation="user_carts_analytics_text")
        return result.text

    async def cart_analysis_text(self, cart_id: int) -> str:
        payload = await self._request_json(
            "POST",
            "/carts/analysis",
            operation="cart_analysis_text",
            json_body={"cart_id": cart_id},
        )
        result = _model_validate(BotTextResult, payload, operation="cart_analysis_text")
        return result.text

    async def verify_order_code(self, code: str | int) -> BotVerifyOrderResult:
        payload = await self._request_json(
            "POST",
            "/orders/verify",
            operation="verify_order_code",
            json_body={"code": code},
        )
        return _model_validate(BotVerifyOrderResult, payload, operation="verify_order_code")


webapp_client = WebappBotClient()
