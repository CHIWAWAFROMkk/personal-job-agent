from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock
import time
from typing import Callable, Iterator, Literal, TypeVar
import uuid
from urllib.parse import urlsplit

from pydantic import Field

from job_agent.models.base import StrictModel
from job_agent.services.profile_store import write_json_atomic


ConnectorKind = Literal["ai", "search", "maps"]
_T = TypeVar("_T")

# The process lock serializes threads; a sibling lock file serializes desktop/CLI
# processes. Reservations live in the JSON record so a crash never frees quota.
_USAGE_LOCK = RLock()
_LOCK_TIMEOUT_SECONDS = 30
_ACTIVE_RESERVATIONS: set[str] = set()


class ApiQuotaExceededError(ValueError):
    """The local monthly allowance cannot admit another provider request."""


class ApiUsageUnavailableError(ValueError):
    """An existing usage record cannot be trusted; new calls must stop."""


def response_token_counts(response: object) -> dict[str, int]:
    """Read optional SDK usage without letting malformed token metadata erase a call."""
    if isinstance(response, tuple) and len(response) == 3:
        return {
            "input_tokens": response[1] if type(response[1]) is int and response[1] >= 0 else 0,
            "output_tokens": response[2] if type(response[2]) is int and response[2] >= 0 else 0,
        }
    try:
        details = getattr(response, "usage", None)
    except Exception:
        details = None
    def count(name: str) -> int:
        try:
            value = getattr(details, name, None)
        except Exception:
            return 0
        return value if type(value) is int and value >= 0 else 0
    return {
        "input_tokens": count("input_tokens") or count("prompt_tokens"),
        "output_tokens": count("output_tokens") or count("completion_tokens"),
    }


def _definitive_auth_rejection(exc: Exception, connector: ConnectorKind, provider: str) -> bool:
    """Only documented first-party 401 authentication rejects free a quota slot."""
    if connector != "ai" or provider not in {"openai", "deepseek"}:
        return False
    try:
        from openai import AuthenticationError
    except ImportError:
        return False
    if not isinstance(exc, AuthenticationError):
        # CLI job matching wraps SDK errors for a safe user-facing message.
        # Inspect only that known wrapper's explicit cause, not arbitrary
        # exception chains or text that happens to contain "401".
        try:
            from job_agent.services.openai_matcher import OpenAIMatcherError
        except ImportError:
            return False

        if not isinstance(exc, OpenAIMatcherError) or not isinstance(exc.__cause__, AuthenticationError):
            return False
        exc = exc.__cause__
    rejected = (
        isinstance(exc, AuthenticationError)
        and exc.status_code == 401
        and getattr(exc.response, "status_code", None) == 401
    )
    if not rejected:
        return False
    if provider == "deepseek":
        # DeepSeek permits a custom base URL in settings. Its documented 401
        # semantics only justify releasing a slot at the official endpoint.
        try:
            request_url = str(getattr(getattr(exc.response, "request", None), "url", ""))
            parsed = urlsplit(request_url)
            return parsed.scheme == "https" and parsed.hostname == "api.deepseek.com" and parsed.port in (None, 443)
        except (AttributeError, RuntimeError, ValueError):
            return False
    return True


@contextmanager
def _locked_usage(path: Path) -> Iterator[Path]:
    """Serialize every usage read/modify/write across threads and processes."""
    if not _USAGE_LOCK.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
        raise TimeoutError("API 用量记录繁忙，请稍后重试。")
    stream = None
    locked = False
    try:
        try:
            path = path.expanduser().resolve(strict=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = path.with_name(path.name + ".lock")
            stream = lock_path.open("a+b")
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while not locked:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("API 用量记录繁忙，请稍后重试。") from exc
                    time.sleep(0.05)
        except TimeoutError:
            raise
        except OSError as exc:
            raise ApiUsageUnavailableError(
                "本机 API 用量记录无法锁定；为避免超出上限，云端请求已暂停。"
            ) from exc
        yield path
    finally:
        if stream is not None:
            try:
                if locked:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        _USAGE_LOCK.release()


class ApiUsageReservation:
    """A durable quota slot that remains charged if the process crashes."""

    def __init__(
        self, path: Path, connector: ConnectorKind, provider: str,
        units: int, reservation_id: str,
    ) -> None:
        self.path = path
        self.connector = connector
        self.provider = provider
        self.units = units
        self.reservation_id = reservation_id
        self._active = True
        self._keep_on_exit = False

    def __enter__(self) -> ApiUsageReservation:
        return self

    @property
    def active(self) -> bool:
        return self._active

    def call(self, invoke: Callable[[], _T]) -> _T:
        """Settle at the provider boundary, before parsing its response."""
        if not self._active:
            raise RuntimeError("API 用量预留已结算或释放。")
        try:
            response = invoke()
        except Exception as exc:
            self.handle_provider_failure(exc)
            raise
        self.commit(**response_token_counts(response))
        return response

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        if self._active and not self._keep_on_exit:
            self.release()

    def release(self) -> None:
        """Release the slot after a failed or cancelled provider request."""
        if self._keep_on_exit:
            raise RuntimeError("请求可能已经计费，预留只能在核对服务商账单后人工处理。")
        with _locked_usage(self.path) as path:
            if self._active:
                usage = _load_api_usage_strict(path)
                if self.reservation_id not in usage.pending:
                    raise ApiUsageUnavailableError("API 用量预留记录丢失，云端请求已暂停。")
                del usage.pending[self.reservation_id]
                _write_usage(path, usage)
                self._active = False
                _ACTIVE_RESERVATIONS.discard(self.reservation_id)

    def handle_provider_failure(self, exc: Exception) -> None:
        """Release a definite auth rejection; retain uncertain provider outcomes."""
        if _definitive_auth_rejection(exc, self.connector, self.provider):
            self.release()
        else:
            self.mark_uncertain()

    def mark_uncertain(self) -> None:
        """Keep a dispatched call charged when no response proves its outcome."""
        # A failed lock/read before updating the state must not make __exit__
        # release a call that may already have reached the provider.
        self._keep_on_exit = True
        _ACTIVE_RESERVATIONS.discard(self.reservation_id)
        with _locked_usage(self.path) as path:
            if not self._active:
                return
            usage = _load_api_usage_strict(path)
            pending = usage.pending.get(self.reservation_id)
            if pending is None:
                raise ApiUsageUnavailableError("API 用量预留记录丢失，云端请求已暂停。")
            pending.state = "uncertain"
            try:
                _write_usage(path, usage)
            finally:
                # If writing fails the prior reservation is still persisted.
                self._active = False
                _ACTIVE_RESERVATIONS.discard(self.reservation_id)

    def commit(
        self, *, successful_requests: int = 1,
        input_tokens: int = 0, output_tokens: int = 0,
        uncertain_requests: int = 0,
    ) -> MonthlyApiUsage:
        """Record successful calls before making the reserved capacity available."""
        if not self._active:
            raise RuntimeError("API 用量预留已结算或释放。")
        # Even invalid settlement metadata can follow a real provider call.
        # Preserve the slot instead of silently granting it again.
        self._keep_on_exit = True
        _ACTIVE_RESERVATIONS.discard(self.reservation_id)
        if not 0 <= successful_requests <= self.units or not 0 <= uncertain_requests <= self.units - successful_requests:
            raise ValueError("成功请求数必须在预留名额范围内。")
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("API 用量不能为负数。")
        with _locked_usage(self.path) as path:
            usage = _load_api_usage_strict(path)
            pending = usage.pending.get(self.reservation_id)
            if pending is None or pending.connector != self.connector or pending.units != self.units:
                raise ApiUsageUnavailableError("API 用量预留记录丢失，云端请求已暂停。")
            counter = getattr(usage, self.connector)
            setattr(usage, self.connector, counter.model_copy(update={
                "provider": self.provider,
                "successful_requests": counter.successful_requests + successful_requests,
                "input_tokens": counter.input_tokens + input_tokens,
                "output_tokens": counter.output_tokens + output_tokens,
                "last_used_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }))
            if uncertain_requests:
                pending.units = uncertain_requests
                pending.state = "uncertain"
            else:
                del usage.pending[self.reservation_id]
            # On write failure the durable pending slot remains in the old file.
            # Never release it from __exit__ after an unsuccessful settlement.
            try:
                _write_usage(path, usage)
            except Exception:
                self._active = False
                _ACTIVE_RESERVATIONS.discard(self.reservation_id)
                raise
            self._active = False
            self._keep_on_exit = False
            _ACTIVE_RESERVATIONS.discard(self.reservation_id)
            return usage


def reserve_api_usage(
    path: Path, connector: ConnectorKind, provider: str,
    monthly_quota: int | None, *, units: int = 1,
) -> ApiUsageReservation:
    """Persist a cross-process quota slot before any provider request begins."""
    if units < 1 or monthly_quota is not None and monthly_quota < 1:
        raise ValueError("API 用量预留数量和月度上限必须为正数。")
    with _locked_usage(path) as path:
        usage = _load_api_usage_strict(path)
        successful = getattr(usage, connector).successful_requests
        in_flight = sum(item.units for item in usage.pending.values() if item.connector == connector)
        if monthly_quota is not None and successful + in_flight + units > monthly_quota:
            label = {"ai": "AI", "search": "搜索", "maps": "地图"}[connector]
            raise ApiQuotaExceededError(f"本月{label}请求额度已用完。")
        reservation_id = uuid.uuid4().hex
        usage.pending[reservation_id] = ApiUsagePending(
            connector=connector, provider=provider.strip(), units=units,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        _write_usage(path, usage)
        _ACTIVE_RESERVATIONS.add(reservation_id)
        return ApiUsageReservation(path, connector, provider.strip(), units, reservation_id)


class ApiUsageCounter(StrictModel):
    provider: str = ""
    successful_requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    last_used_at: str | None = None


class ApiUsagePending(StrictModel):
    connector: ConnectorKind
    provider: str
    units: int = Field(ge=1)
    started_at: str
    state: Literal["in_flight", "uncertain"] = "in_flight"


class MonthlyApiUsage(StrictModel):
    schema_version: Literal["1", "2"] = "2"
    month: str
    ai: ApiUsageCounter = Field(default_factory=ApiUsageCounter)
    search: ApiUsageCounter = Field(default_factory=ApiUsageCounter)
    maps: ApiUsageCounter = Field(default_factory=ApiUsageCounter)
    pending: dict[str, ApiUsagePending] = Field(default_factory=dict)


def _write_usage(path: Path, usage: MonthlyApiUsage) -> None:
    usage.schema_version = "2"
    write_json_atomic(usage.model_dump(mode="json"), path)


def _month_now() -> str:
    return date.today().strftime("%Y-%m")


def _load_api_usage_strict(path: Path) -> MonthlyApiUsage:
    """Only an absent file starts fresh; a damaged file must fail closed."""
    try:
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            return MonthlyApiUsage(month=_month_now())
        if not stat.S_ISREG(path_stat.st_mode):
            raise OSError("usage path is not a file")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("usage record must be an object")
        if not {"schema_version", "month", "ai", "search", "maps"} <= payload.keys():
            raise ValueError("usage record is missing required fields")
        if payload["schema_version"] == "2" and "pending" not in payload:
            raise ValueError("usage record is missing pending reservations")
        for connector in ("ai", "search", "maps"):
            if not isinstance(payload[connector], dict) or "successful_requests" not in payload[connector]:
                raise ValueError("usage record is missing request counts")
        usage = MonthlyApiUsage.model_validate(payload)
        if not isinstance(usage.month, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", usage.month):
            raise ValueError("usage month is invalid")
        datetime.strptime(usage.month, "%Y-%m")
    except (OSError, ValueError) as exc:
        raise ApiUsageUnavailableError(
            "本机 API 用量记录无法读取；为避免超出上限，云端请求已暂停。"
        ) from exc
    month = _month_now()
    if usage.month > month:
        raise ApiUsageUnavailableError(
            "本机 API 用量月份晚于当前系统月份；为避免超出上限，云端请求已暂停。"
        )
    # An in-flight call may finish just after midnight. Carry its reservation
    # forward so the next month's quota cannot admit a competing call first.
    return usage if usage.month == month else MonthlyApiUsage(month=month, pending=usage.pending)


def load_api_usage(path: Path) -> MonthlyApiUsage:
    with _locked_usage(path) as path:
        return _load_api_usage_strict(path)


def record_api_usage(
    path: Path,
    connector: ConnectorKind,
    provider: str,
    *,
    successful_requests: int = 1,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> MonthlyApiUsage:
    if successful_requests < 0 or input_tokens < 0 or output_tokens < 0:
        raise ValueError("API 用量不能为负数。")
    with _locked_usage(path) as path:
        usage = _load_api_usage_strict(path)
        counter = getattr(usage, connector)
        updated = counter.model_copy(
            update={
                "provider": provider.strip(),
                "successful_requests": counter.successful_requests + successful_requests,
                "input_tokens": counter.input_tokens + input_tokens,
                "output_tokens": counter.output_tokens + output_tokens,
                "last_used_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
        setattr(usage, connector, updated)
        _write_usage(path, usage)
        return usage


def public_api_usage(path: Path, limits: dict[ConnectorKind, int | None]) -> dict[str, object]:
    with _locked_usage(path) as path:
        try:
            usage = _load_api_usage_strict(path)
        except ApiUsageUnavailableError:
            usage = None
        connectors: dict[str, object] = {}
        for connector in ("ai", "search", "maps"):
            counter = getattr(usage, connector) if usage is not None else None
            limit = limits.get(connector)  # type: ignore[arg-type]
            pending_total = (
                sum(item.units for item in usage.pending.values() if item.connector == connector)
                if usage is not None else 0
            )
            in_flight = (
                sum(item.units for key, item in usage.pending.items()
                    if item.connector == connector and key in _ACTIVE_RESERVATIONS)
                if usage is not None else 0
            )
            remaining = (
                max(0, limit - counter.successful_requests - pending_total)
                if limit is not None and counter is not None
                else None
            )
            connectors[connector] = {
                "provider": counter.provider if counter is not None else "",
                "record_status": "ok" if counter is not None else "unavailable",
                "successful_requests": counter.successful_requests if counter is not None else None,
                "in_flight_requests": in_flight,
                "pending_requests": pending_total,
                "other_or_unreconciled_requests": pending_total - in_flight,
                "uncertain_requests": (
                    sum(item.units for item in usage.pending.values()
                        if item.connector == connector and item.state == "uncertain")
                    if usage is not None else None
                ),
                "input_tokens": counter.input_tokens if counter is not None else None,
                "output_tokens": counter.output_tokens if counter is not None else None,
                "last_used_at": counter.last_used_at if counter is not None else None,
                "local_monthly_limit": limit,
                "local_remaining": remaining,
                "balance_source": "local_limit" if limit is not None else "provider_console",
            }
    has_unreconciled = usage is not None and any(
        key not in _ACTIVE_RESERVATIONS for key in usage.pending
    )
    return {
        "month": usage.month if usage is not None else _month_now(),
        "connectors": connectors,
        "notice": (
            "本机 API 用量记录无法读取，云端请求已暂停；服务商真实余额请查看其控制台。"
            if usage is None else
            "有其他进程正在调用，或异常中断留下未结算预留；这些名额仍占额度。"
            "若长时间不消失，请先核对服务商控制台，再备份用量记录并人工处理对应预留；"
            "调用进行中不要处理，也不要清空整份用量记录。"
            if has_unreconciled else
            "这里只统计本软件成功发出的请求。服务商账户真实余额以其控制台为准；"
            "设置本机月度上限后可显示可控剩余额度。"
        ),
    }
