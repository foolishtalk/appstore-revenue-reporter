#!/usr/bin/env python3
"""Download App Store Sales and Trends reports and send a revenue summary."""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key


API_URL = "https://api.appstoreconnect.apple.com/v1/salesReports"
APPLE_REPORT_TIMEZONE = ZoneInfo("America/Los_Angeles")
JWT_AUDIENCE = "appstoreconnect-v1"
REPORT_VERSION = "1_0"
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
MAX_WECOM_MESSAGE_BYTES = 3800
REPORTER_DOWNLOAD_URL = "https://itunespartner.apple.com/assets/reporter.zip"
REPORTER_JAR_SHA256 = "f0c1bcf46ca527e017ecff9ca42e53af9d915c1d0c49f009df9c6ed0418caa2e"
REPORTER_SALES_URL = "https://reportingitc-reporter.apple.com/reportservice/sales/v1"
REPORTER_FINANCE_URL = "https://reportingitc-reporter.apple.com/reportservice/finance/v1"
REPORTER_RETRYABLE_CODES = {"110", "111", "117", "119", "211", "212"}
FX_API_URL = "https://api.frankfurter.dev/v2/rates"
FX_SOURCE_NAME = "Frankfurter v2 (aggregated central-bank reference rates)"
FX_RATE_QUANTUM = Decimal("0.000000000001")


class ReporterError(RuntimeError):
    """An expected, user-actionable reporter error."""


@dataclass(frozen=True)
class AppStoreConfig:
    issuer_id: str
    key_id: str
    vendor_number: str
    private_key: ec.EllipticCurvePrivateKey

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "AppStoreConfig":
        required = (
            "ASC_ISSUER_ID",
            "ASC_KEY_ID",
            "ASC_PRIVATE_KEY_BASE64",
            "ASC_VENDOR_NUMBER",
        )
        missing = [name for name in required if not env.get(name, "").strip()]
        if missing:
            raise ReporterError("缺少必填环境变量：" + ", ".join(missing))

        encoded_key = "".join(env["ASC_PRIVATE_KEY_BASE64"].split())
        try:
            pem = base64.b64decode(encoded_key, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ReporterError("ASC_PRIVATE_KEY_BASE64 不是有效的 Base64 内容") from exc

        try:
            private_key = load_pem_private_key(pem, password=None)
        except (TypeError, ValueError) as exc:
            raise ReporterError("ASC_PRIVATE_KEY_BASE64 解码后不是有效的 .p8 私钥") from exc

        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
            private_key.curve, ec.SECP256R1
        ):
            raise ReporterError("App Store Connect 私钥必须是 P-256 椭圆曲线私钥")

        return cls(
            issuer_id=env["ASC_ISSUER_ID"].strip(),
            key_id=env["ASC_KEY_ID"].strip(),
            vendor_number=env["ASC_VENDOR_NUMBER"].strip(),
            private_key=private_key,
        )


@dataclass(frozen=True)
class ReporterConfig:
    access_token: str
    vendor_number: str
    account_number: str | None = None
    reporter_jar: Path | None = None
    java_executable: str = "java"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "ReporterConfig":
        required = ("ASC_REPORTER_ACCESS_TOKEN", "ASC_VENDOR_NUMBER")
        missing = [name for name in required if not env.get(name, "").strip()]
        if missing:
            raise ReporterError("Reporter 认证缺少必填环境变量：" + ", ".join(missing))

        access_token = env["ASC_REPORTER_ACCESS_TOKEN"].strip()
        vendor_number = env["ASC_VENDOR_NUMBER"].strip()
        account_number = env.get("ASC_REPORTER_ACCOUNT", "").strip() or None
        for name, value in (
            ("ASC_REPORTER_ACCESS_TOKEN", access_token),
            ("ASC_VENDOR_NUMBER", vendor_number),
            ("ASC_REPORTER_ACCOUNT", account_number or ""),
        ):
            if "\n" in value or "\r" in value:
                raise ReporterError(f"{name} 不能包含换行符")
        if not vendor_number.isdigit():
            raise ReporterError("ASC_VENDOR_NUMBER 应为纯数字 Vendor Number")
        if account_number is not None and not account_number.isdigit():
            raise ReporterError("ASC_REPORTER_ACCOUNT 应为纯数字 Account Number")

        jar_value = env.get("ASC_REPORTER_JAR", "").strip()
        return cls(
            access_token=access_token,
            vendor_number=vendor_number,
            account_number=account_number,
            reporter_jar=Path(jar_value).expanduser() if jar_value else None,
            java_executable=env.get("ASC_REPORTER_JAVA", "java").strip() or "java",
        )


def get_auth_method(env: Mapping[str, str] = os.environ) -> str:
    method = env.get("ASC_AUTH_METHOD", "reporter").strip().lower().replace("-", "_")
    if method not in {"reporter", "api_key"}:
        raise ReporterError("ASC_AUTH_METHOD 只支持 reporter 或 api_key")
    return method


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    start_date: date
    end_date: date

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1


@dataclass(frozen=True)
class MonthlyFxRates:
    """Frozen CNY conversion rates for one Apple report month."""

    report_month: str
    source_start_date: date
    source_end_date: date
    rates: Mapping[str, Decimal]
    observation_counts: Mapping[str, int]


@dataclass(frozen=True)
class DailyTotals:
    """Paid net units and proceeds parsed from one Apple daily report."""

    amounts: Mapping[str, Decimal]
    units: Decimal


def build_jwt(config: AppStoreConfig, now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else now
    payload = {
        "iss": config.issuer_id,
        "aud": JWT_AUDIENCE,
        "iat": issued_at,
        "exp": issued_at + 5 * 60,
    }
    return jwt.encode(
        payload,
        config.private_key,
        algorithm="ES256",
        headers={"kid": config.key_id, "typ": "JWT"},
    )


def report_end_date(now: datetime | None = None) -> date:
    """Return the latest normally available complete Apple reporting day."""
    current = now or datetime.now(tz=APPLE_REPORT_TIMEZONE)
    if current.tzinfo is None:
        raise ValueError("now 必须包含时区")
    pacific_now = current.astimezone(APPLE_REPORT_TIMEZONE)
    # Apple says reports are generally available by 08:00 PT. Before then,
    # yesterday's report can still return Reporter error 210.
    days_back = 1 if pacific_now.hour >= 8 else 2
    return pacific_now.date() - timedelta(days=days_back)


def build_periods(end_date: date) -> list[Period]:
    return [
        Period("yesterday", "最新可用日报日", end_date, end_date),
        Period("last_7_days", "最近 7 天", end_date - timedelta(days=6), end_date),
        Period("last_30_days", "最近 30 天", end_date - timedelta(days=29), end_date),
        Period(
            "last_quarter",
            "最近一个季度（滚动 90 天）",
            end_date - timedelta(days=89),
            end_date,
        ),
    ]


def iter_dates(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def comparison_period(period: Period) -> Period:
    previous_end = period.start_date - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period.days - 1)
    return Period(
        f"previous_{period.key}",
        f"{period.label}的上一周期",
        previous_start,
        previous_end,
    )


def required_start_date(end_date: date) -> date:
    longest_period = build_periods(end_date)[-1]
    return comparison_period(longest_period).start_date


def _month_key(value: date) -> str:
    return value.strftime("%Y-%m")


def _previous_month_range(report_month: str) -> tuple[date, date]:
    try:
        report_month_start = date.fromisoformat(f"{report_month}-01")
    except ValueError as exc:
        raise ReporterError(f"无效的汇率报表月份：{report_month}") from exc
    source_end = report_month_start - timedelta(days=1)
    return source_end.replace(day=1), source_end


def _parse_cached_fx_rates(path: Path, report_month: str) -> MonthlyFxRates:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("report_month") != report_month:
            raise ValueError("report_month 不匹配")
        source_period = payload["source_period"]
        raw_rates = payload["rates"]
        raw_counts = payload.get("observation_counts", {})
        if not isinstance(source_period, dict) or not isinstance(raw_rates, dict):
            raise ValueError("字段类型无效")
        source_start = date.fromisoformat(str(source_period["start_date"]))
        source_end = date.fromisoformat(str(source_period["end_date"]))
        expected_start, expected_end = _previous_month_range(report_month)
        if (source_start, source_end) != (expected_start, expected_end):
            raise ValueError("source_period 不是报表月的上一个自然月")
        rates = {str(currency): Decimal(str(rate)) for currency, rate in raw_rates.items()}
        if any(rate <= 0 for rate in rates.values()):
            raise ValueError("汇率必须大于 0")
        counts = {
            str(currency): int(count)
            for currency, count in raw_counts.items()
            if str(currency) in rates
        }
    except (OSError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ReporterError(f"汇率缓存文件无效：{path}") from exc
    rates["CNY"] = Decimal("1")
    return MonthlyFxRates(report_month, source_start, source_end, rates, counts)


def _write_cached_fx_rates(path: Path, table: MonthlyFxRates) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "report_month": table.report_month,
        "base_currency": "CNY",
        "method": "previous-calendar-month average of daily source-currency-to-CNY rates",
        "source": {"name": FX_SOURCE_NAME, "url": FX_API_URL},
        "source_period": {
            "start_date": table.source_start_date.isoformat(),
            "end_date": table.source_end_date.isoformat(),
        },
        "rates": {
            currency: format(rate, "f")
            for currency, rate in sorted(table.rates.items())
        },
        "observation_counts": dict(sorted(table.observation_counts.items())),
    }
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary_path.replace(path)


def _fetch_monthly_fx_rates(
    report_month: str,
    currencies: set[str],
    *,
    session: requests.Session,
    timeout_seconds: float = 30,
    max_attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Decimal], dict[str, int]]:
    foreign_currencies = sorted(currencies.difference({"CNY"}))
    if not foreign_currencies:
        return {}, {}
    invalid = [currency for currency in foreign_currencies if not re.fullmatch(r"[A-Z]{3}", currency)]
    if invalid:
        raise ReporterError("Apple 日报包含无效币种代码：" + ", ".join(invalid))

    source_start, source_end = _previous_month_range(report_month)
    params = {
        "base": "CNY",
        "quotes": ",".join(foreign_currencies),
        "from": source_start.isoformat(),
        "to": source_end.isoformat(),
    }
    response: requests.Response | None = None
    for attempt in range(max_attempts):
        try:
            response = session.get(FX_API_URL, params=params, timeout=timeout_seconds)
        except requests.RequestException as exc:
            if attempt + 1 == max_attempts:
                raise ReporterError(f"获取 {report_month} 报表月的 CNY 汇率失败") from exc
            sleep(_retry_delay(None, attempt))
            continue
        if response.status_code == 200:
            break
        if response.status_code in RETRYABLE_HTTP_STATUSES and attempt + 1 < max_attempts:
            sleep(_retry_delay(response, attempt))
            continue
        raise ReporterError(
            f"获取 {report_month} 报表月的 CNY 汇率失败（HTTP {response.status_code}）"
        )
    if response is None or response.status_code != 200:
        raise AssertionError("unreachable")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ReporterError("汇率接口返回了无法解析的 JSON") from exc
    if not isinstance(payload, list):
        raise ReporterError("汇率接口返回的数据结构无效")

    observations: defaultdict[str, list[Decimal]] = defaultdict(list)
    requested = set(foreign_currencies)
    for item in payload:
        if not isinstance(item, dict):
            continue
        currency = str(item.get("quote", "")).upper()
        if currency not in requested or item.get("base") != "CNY":
            continue
        try:
            cny_to_currency = Decimal(str(item["rate"]))
        except (KeyError, InvalidOperation) as exc:
            raise ReporterError(f"汇率接口返回了无效的 {currency} 汇率") from exc
        if cny_to_currency <= 0:
            raise ReporterError(f"汇率接口返回了非正数的 {currency} 汇率")
        # The API gives 1 CNY -> quote currency. Invert each daily observation
        # first, then average, so the stored rate is 1 source currency -> CNY.
        observations[currency].append(Decimal("1") / cny_to_currency)

    missing = sorted(requested.difference(observations))
    if missing:
        raise ReporterError(
            f"汇率源缺少 {report_month} 报表月所需币种：" + ", ".join(missing)
        )
    rates = {
        currency: (sum(values, Decimal("0")) / Decimal(len(values))).quantize(
            FX_RATE_QUANTUM, rounding=ROUND_HALF_UP
        )
        for currency, values in observations.items()
    }
    return rates, {currency: len(values) for currency, values in observations.items()}


def load_monthly_fx_rates(
    report_month: str,
    currencies: set[str],
    cache_dir: Path,
    *,
    session: requests.Session,
) -> MonthlyFxRates:
    cache_path = cache_dir / f"{report_month}.json"
    source_start, source_end = _previous_month_range(report_month)
    if cache_path.exists():
        cached = _parse_cached_fx_rates(cache_path, report_month)
        rates = dict(cached.rates)
        counts = dict(cached.observation_counts)
    else:
        rates = {"CNY": Decimal("1")}
        counts = {}

    missing = currencies.difference(rates)
    fetched_rates, fetched_counts = _fetch_monthly_fx_rates(
        report_month, missing, session=session
    )
    rates.update(fetched_rates)
    counts.update(fetched_counts)
    table = MonthlyFxRates(report_month, source_start, source_end, rates, counts)
    if fetched_rates:
        _write_cached_fx_rates(cache_path, table)
    return table


def load_fx_tables(
    daily_totals: Mapping[date, DailyTotals | Mapping[str, Decimal] | None],
    cache_dir: Path,
    *,
    session: requests.Session | None = None,
) -> dict[str, MonthlyFxRates]:
    currencies_by_month: defaultdict[str, set[str]] = defaultdict(set)
    for report_date, totals in daily_totals.items():
        if totals is None:
            continue
        amounts = totals.amounts if isinstance(totals, DailyTotals) else totals
        if not amounts:
            continue
        currencies_by_month[_month_key(report_date)].update(
            currency for currency, amount in amounts.items() if amount != 0
        )
    client = session or requests.Session()
    return {
        report_month: load_monthly_fx_rates(
            report_month, currencies, cache_dir, session=client
        )
        for report_month, currencies in sorted(currencies_by_month.items())
    }


def _response_error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list):
        return ""
    details = []
    for error in errors[:3]:
        if not isinstance(error, dict):
            continue
        text = error.get("detail") or error.get("title") or error.get("code")
        if text:
            details.append(str(text))
    return "; ".join(details)


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After", "").strip()
        try:
            return min(max(float(retry_after), 0.0), 60.0)
        except ValueError:
            pass
    return float(2**attempt)


def _verify_reporter_jar(data: bytes, expected_sha256: str = REPORTER_JAR_SHA256) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ReporterError(
            "Apple Reporter.jar 的 SHA-256 与已审核版本不一致；"
            "请先人工确认 Apple 是否发布了新版 Reporter，再更新校验值"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as jar:
            manifest = jar.read("META-INF/MANIFEST.MF")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ReporterError("下载到的 Reporter.jar 文件结构无效") from exc
    if b"Main-Class: com.apple.gbi.autoingest.client.Reporter" not in manifest:
        raise ReporterError("下载到的 Reporter.jar 不包含预期的 Apple Reporter 入口")


def ensure_reporter_jar(
    cache_path: Path = Path(".cache/apple-reporter/Reporter.jar"),
    *,
    session: requests.Session | None = None,
    timeout_seconds: float = 60,
) -> Path:
    """Download and verify Apple's official Reporter.jar when it is not cached."""
    if cache_path.exists():
        try:
            data = cache_path.read_bytes()
        except OSError as exc:
            raise ReporterError(f"无法读取 ASC_REPORTER_JAR：{cache_path}") from exc
        _verify_reporter_jar(data)
        return cache_path.resolve()

    client = session or requests.Session()
    try:
        response = client.get(REPORTER_DOWNLOAD_URL, timeout=timeout_seconds)
    except requests.RequestException as exc:
        raise ReporterError("下载 Apple 官方 Reporter 工具失败") from exc
    if response.status_code != 200:
        raise ReporterError(f"下载 Apple 官方 Reporter 工具失败（HTTP {response.status_code}）")
    if len(response.content) > 5 * 1024 * 1024:
        raise ReporterError("Apple Reporter 下载文件异常地超过 5 MB")

    try:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            jar_data = archive.read("Reporter/Reporter.jar")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ReporterError("Apple Reporter 下载包不是预期的 ZIP 文件") from exc
    _verify_reporter_jar(jar_data)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = cache_path.with_suffix(".tmp")
    temporary_path.write_bytes(jar_data)
    temporary_path.chmod(0o600)
    temporary_path.replace(cache_path)
    return cache_path.resolve()


def resolve_reporter_jar(config: ReporterConfig) -> Path:
    if config.reporter_jar is None:
        return ensure_reporter_jar()
    path = config.reporter_jar.resolve()
    if not path.is_file():
        raise ReporterError(f"ASC_REPORTER_JAR 指定的文件不存在：{path}")
    try:
        _verify_reporter_jar(path.read_bytes())
    except OSError as exc:
        raise ReporterError(f"无法读取 ASC_REPORTER_JAR：{path}") from exc
    return path


def _parse_reporter_error(output: str) -> tuple[str | None, str, float | None]:
    match = re.search(r"<Error>.*?</Error>", output, flags=re.DOTALL)
    if not match:
        return None, "", None
    try:
        root = ElementTree.fromstring(match.group(0))
    except ElementTree.ParseError:
        return None, "", None
    code = (root.findtext("Code") or "").strip() or None
    message = (root.findtext("Message") or "").strip()
    retry_text = (root.findtext("Retry") or "").strip()
    try:
        retry_seconds = min(max(float(retry_text) / 1000, 0.0), 60.0)
    except ValueError:
        retry_seconds = None
    return code, message, retry_seconds


class ReporterClient:
    """Small wrapper around Apple's official Reporter.jar Robot mode."""

    def __init__(
        self,
        config: ReporterConfig,
        reporter_jar: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
        timeout_seconds: float = 90,
    ) -> None:
        self.config = config
        self.reporter_jar = reporter_jar
        self.runner = runner
        self.sleep = sleep
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._work_dir: Path | None = None
        self._properties_path: Path | None = None
        self._java_path = config.java_executable

    def __enter__(self) -> "ReporterClient":
        java_path = shutil.which(self.config.java_executable)
        if java_path is None:
            raise ReporterError(
                "未找到 Java；Reporter 方式需要 Java 8 或更高版本，"
                "也可以用 ASC_REPORTER_JAVA 指定 java 路径"
            )
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="appstore-reporter-")
        self._work_dir = Path(self._temporary_directory.name)
        self._properties_path = self._work_dir / "Reporter.properties"
        lines = [
            f"AccessToken={self.config.access_token}",
            "Mode=Robot.XML",
            f"SalesUrl={REPORTER_SALES_URL}",
            f"FinanceUrl={REPORTER_FINANCE_URL}",
        ]
        if self.config.account_number:
            lines.insert(1, f"Account={self.config.account_number}")
        self._properties_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._properties_path.chmod(0o600)
        self._java_path = java_path
        return self

    def __exit__(self, *_: object) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
        self._temporary_directory = None
        self._work_dir = None
        self._properties_path = None

    def download_daily_report(self, report_date: date) -> bytes | None:
        if self._work_dir is None or self._properties_path is None:
            raise RuntimeError("ReporterClient 必须在 with 语句中使用")
        date_value = report_date.strftime("%Y%m%d")
        expected_path = self._work_dir / (
            f"S_D_{self.config.vendor_number}_{date_value}.txt.gz"
        )
        command = [
            self._java_path,
            "-jar",
            str(self.reporter_jar),
            f"p={self._properties_path.name}",
            "m=Robot.XML",
            "Sales.getReport",
            f"{self.config.vendor_number},",
            "Sales,",
            "Summary,",
            "Daily,",
            date_value,
        ]

        for attempt in range(self.max_attempts):
            try:
                completed = self.runner(
                    command,
                    cwd=self._work_dir,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                if attempt + 1 == self.max_attempts:
                    raise ReporterError(
                        f"Reporter 下载 {report_date.isoformat()} 日报超时"
                    ) from exc
                self.sleep(float(2**attempt))
                continue

            if expected_path.exists():
                payload = expected_path.read_bytes()
                expected_path.unlink()
                return payload

            output = f"{completed.stdout}\n{completed.stderr}"
            code, message, retry_seconds = _parse_reporter_error(output)
            if code == "213":
                return None
            if code in REPORTER_RETRYABLE_CODES and attempt + 1 < self.max_attempts:
                self.sleep(retry_seconds if retry_seconds is not None else float(2**attempt))
                continue

            safe_message = message.replace(self.config.access_token, "[REDACTED]")
            if code == "123":
                hint = "；Reporter Access Token 已过期，请重新生成（有效期为 180 天）"
            elif code in {"124", "125"}:
                hint = "；请检查 ASC_REPORTER_ACCESS_TOKEN"
            elif code == "214":
                hint = "；该 Apple 账户关联多个账号，请设置 ASC_REPORTER_ACCOUNT"
            elif code in {"215", "216"}:
                hint = "；ASC_REPORTER_ACCOUNT 无效，可用 Sales.getAccounts 查询"
            elif code == "210":
                hint = "；该日报尚未生成，请在太平洋时间 08:00 后重试"
            else:
                hint = ""
            code_text = code or f"process-exit-{completed.returncode}"
            detail = f"：{safe_message}" if safe_message else ""
            raise ReporterError(
                f"Reporter 下载 {report_date.isoformat()} 日报失败（{code_text}）{detail}{hint}"
            )

        raise AssertionError("unreachable")


def download_daily_report(
    session: requests.Session,
    config: AppStoreConfig,
    report_date: date,
    *,
    timeout_seconds: float = 30,
    max_attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes | None:
    params = {
        "filter[frequency]": "DAILY",
        "filter[reportDate]": report_date.isoformat(),
        "filter[reportSubType]": "SUMMARY",
        "filter[reportType]": "SALES",
        "filter[vendorNumber]": config.vendor_number,
        "filter[version]": REPORT_VERSION,
    }

    for attempt in range(max_attempts):
        response: requests.Response | None = None
        try:
            response = session.get(
                API_URL,
                params=params,
                headers={"Authorization": f"Bearer {build_jwt(config)}"},
                timeout=timeout_seconds,
            )
        except requests.RequestException as exc:
            if attempt + 1 == max_attempts:
                raise ReporterError(
                    f"下载 {report_date.isoformat()} 日报时网络请求失败，已重试 {max_attempts} 次"
                ) from exc
            sleep(_retry_delay(None, attempt))
            continue

        if response.status_code == 200:
            return response.content
        if response.status_code == 404:
            return None
        if response.status_code in RETRYABLE_HTTP_STATUSES and attempt + 1 < max_attempts:
            sleep(_retry_delay(response, attempt))
            continue

        detail = _response_error_detail(response)
        suffix = f"：{detail}" if detail else ""
        if response.status_code == 401:
            hint = "；请检查 Issuer ID、Key ID、私钥和系统时间"
        elif response.status_code == 403:
            hint = "；请确认使用 Team Key，且权限至少为 Sales and Reports"
        elif response.status_code == 429:
            hint = "；Apple API 限流，稍后重试"
        else:
            hint = ""
        raise ReporterError(
            f"下载 {report_date.isoformat()} 日报失败（HTTP {response.status_code}）{suffix}{hint}"
        )

    raise AssertionError("unreachable")


def decode_report_payload(payload: bytes) -> str:
    try:
        raw = gzip.decompress(payload) if payload.startswith(b"\x1f\x8b") else payload
        return raw.decode("utf-8-sig")
    except (gzip.BadGzipFile, EOFError, UnicodeDecodeError) as exc:
        raise ReporterError("Apple 返回的日报不是有效的 gzip/UTF-8 TSV 文件") from exc


def _first_value(row: Mapping[str, str], names: Sequence[str]) -> str | None:
    for name in names:
        value = row.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def parse_daily_totals(tsv_text: str) -> DailyTotals:
    """Return paid net units and total proceeds (Units × Developer Proceeds)."""
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
    if not reader.fieldnames:
        return DailyTotals({}, Decimal("0"))
    reader.fieldnames = [field.strip() if field else field for field in reader.fieldnames]

    required = {"Units", "Developer Proceeds"}
    absent = sorted(required.difference(reader.fieldnames))
    currency_fields = ("Currency of Proceeds", "Developer Proceeds Currency")
    if not any(name in reader.fieldnames for name in currency_fields):
        absent.append("Currency of Proceeds")
    if absent:
        raise ReporterError("日报缺少必要字段：" + ", ".join(absent))

    totals: defaultdict[str, Decimal] = defaultdict(Decimal)
    total_units = Decimal("0")
    for row_number, row in enumerate(reader, start=2):
        if not any(value and value.strip() for value in row.values()):
            continue
        currency = _first_value(row, currency_fields)
        if not currency:
            raise ReporterError(f"日报第 {row_number} 行缺少收益币种")
        try:
            units = Decimal((row.get("Units") or "").strip())
            proceeds_per_unit = Decimal((row.get("Developer Proceeds") or "").strip())
        except InvalidOperation as exc:
            raise ReporterError(f"日报第 {row_number} 行包含无效的 Units 或 Developer Proceeds") from exc
        totals[currency.upper()] += units * proceeds_per_unit
        # Summary Sales Reports also contain free downloads, re-downloads and
        # updates. They have zero Developer Proceeds and are not paid sales.
        if proceeds_per_unit != 0:
            total_units += units
    return DailyTotals(dict(totals), total_units)


def parse_report_totals(tsv_text: str) -> dict[str, Decimal]:
    """Return proceeds by currency; retained for callers that only need revenue."""
    return dict(parse_daily_totals(tsv_text).amounts)


def _merge_totals(target: defaultdict[str, Decimal], source: Mapping[str, Decimal]) -> None:
    for currency, amount in source.items():
        target[currency] += amount


def convert_amounts_to_cny(
    report_date: date,
    amounts: Mapping[str, Decimal],
    fx_tables: Mapping[str, MonthlyFxRates],
) -> Decimal:
    total = Decimal("0")
    for currency, amount in amounts.items():
        if amount == 0:
            continue
        if currency == "CNY":
            rate = Decimal("1")
        else:
            table = fx_tables.get(_month_key(report_date))
            rate = table.rates.get(currency) if table is not None else None
            if rate is None:
                raise ReporterError(
                    f"缺少 {report_date.isoformat()} 所属报表月的 {currency}->CNY 汇率"
                )
        total += amount * rate
    return total


def _summarize_range(
    period: Period,
    daily_totals: Mapping[date, DailyTotals | Mapping[str, Decimal] | None],
    fx_tables: Mapping[str, MonthlyFxRates],
) -> dict[str, object]:
    totals: defaultdict[str, Decimal] = defaultdict(Decimal)
    cny_total = Decimal("0")
    unit_total = Decimal("0")
    report_dates = []
    no_report_dates = []
    for current in iter_dates(period.start_date, period.end_date):
        day = daily_totals.get(current)
        if day is None:
            no_report_dates.append(current.isoformat())
            continue
        report_dates.append(current.isoformat())
        if isinstance(day, DailyTotals):
            amounts = day.amounts
            unit_total += day.units
        else:
            # Backward compatibility for callers that supplied revenue-only mappings.
            amounts = day
        _merge_totals(totals, amounts)
        cny_total += convert_amounts_to_cny(current, amounts, fx_tables)
    return {
        "start_date": period.start_date.isoformat(),
        "end_date": period.end_date.isoformat(),
        "requested_days": period.days,
        "report_days": len(report_dates),
        "no_report_dates": no_report_dates,
        "amounts": {
            currency: decimal_to_string(amount)
            for currency, amount in sorted(totals.items())
            if amount != 0
        },
        "cny_amount": decimal_to_string(cny_total),
        "units": quantity_to_string(unit_total),
        "_cny_decimal": cny_total,
        "_units_decimal": unit_total,
    }


def _comparison_values(current: Decimal, previous: Decimal) -> dict[str, str | None]:
    difference = current - previous
    if difference == 0:
        direction = "equal"
        percent = Decimal("0")
    elif previous == 0:
        direction = "new" if current > 0 else "turned_negative"
        percent = None
    else:
        direction = "up" if difference > 0 else "down"
        percent = difference / abs(previous) * Decimal("100")
    return {
        "change_amount_cny": decimal_to_string(difference),
        "change_percent": percent_to_string(percent) if percent is not None else None,
        "direction": direction,
    }


def _unit_comparison_values(current: Decimal, previous: Decimal) -> dict[str, str | None]:
    values = _comparison_values(current, previous)
    return {
        "change_units": quantity_to_string(current - previous),
        "unit_change_percent": values["change_percent"],
        "unit_direction": values["direction"],
    }


def summarize_periods(
    periods: Sequence[Period],
    daily_totals: Mapping[date, DailyTotals | Mapping[str, Decimal] | None],
    fx_tables: Mapping[str, MonthlyFxRates],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for period in periods:
        current = _summarize_range(period, daily_totals, fx_tables)
        previous_period = comparison_period(period)
        previous = _summarize_range(previous_period, daily_totals, fx_tables)
        current_decimal = current.pop("_cny_decimal")
        previous_decimal = previous.pop("_cny_decimal")
        current_units = current.pop("_units_decimal")
        previous_units = previous.pop("_units_decimal")
        assert isinstance(current_decimal, Decimal)
        assert isinstance(previous_decimal, Decimal)
        assert isinstance(current_units, Decimal)
        assert isinstance(previous_units, Decimal)
        result[period.key] = {
            "label": period.label,
            **current,
            "comparison": {
                **previous,
                **_comparison_values(current_decimal, previous_decimal),
                **_unit_comparison_values(current_units, previous_units),
            },
        }
    return result


def decimal_to_string(amount: Decimal) -> str:
    rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = Decimal("0.00")
    return format(rounded, ".2f")


def quantity_to_string(quantity: Decimal) -> str:
    if quantity == 0:
        return "0"
    rendered = format(quantity, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def percent_to_string(percent: Decimal) -> str:
    rounded = percent.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        return "0"
    rendered = format(rounded, ".1f")
    return rendered.removesuffix(".0")


def format_cny(amount: object) -> str:
    try:
        return f"¥{Decimal(str(amount)):,.2f}"
    except InvalidOperation as exc:
        raise ReporterError(f"报告包含无效的 CNY 金额：{amount}") from exc


def format_comparison(comparison: Mapping[str, object]) -> str:
    direction = comparison["direction"]
    percent = comparison["change_percent"]
    if direction == "up":
        return f'<font color="info">+{percent}%</font>'
    if direction == "down":
        return f'<font color="warning">{percent}%</font>'
    if direction == "new":
        return '<font color="info">新增</font>'
    if direction == "turned_negative":
        return '<font color="warning">转负</font>'
    return "="


def format_unit_comparison(comparison: Mapping[str, object]) -> str:
    return format_comparison(
        {
            "direction": comparison["unit_direction"],
            "change_percent": comparison["unit_change_percent"],
        }
    )


def format_units(units: object) -> str:
    try:
        quantity = Decimal(str(units))
    except InvalidOperation as exc:
        raise ReporterError(f"报告包含无效的销售数量：{units}") from exc
    if quantity % 1:
        return f"{quantity:,f}".rstrip("0").rstrip(".")
    return f"{quantity:,.0f}"


def format_amounts(amounts: Mapping[str, str]) -> str:
    if not amounts:
        return "无收入"
    return "；".join(f"{currency} {amount}" for currency, amount in sorted(amounts.items()))


def render_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# App Store 收入日报",
        "",
        f"> Apple 报表统计截止日（太平洋时间）：{report['end_date']}",
        "> 收入口径：Units × Developer Proceeds，统一折算为 CNY",
        "> 销售数量口径：产生收益的净 Units；不含免费下载、重新下载和更新",
        "> 对比口径：与紧邻的上一等长周期相比",
        "",
    ]
    period_data = report["periods"]
    assert isinstance(period_data, dict)
    for key in ("yesterday", "last_7_days", "last_30_days", "last_quarter"):
        period = period_data[key]
        assert isinstance(period, dict)
        comparison = period["comparison"]
        assert isinstance(comparison, dict)
        lines.extend(
            [
                f"**{period['label']}**　{format_cny(period['cny_amount'])}　"
                f"{format_comparison(comparison)}",
                f"> 销售数量 {format_units(period['units'])}　"
                f"{format_unit_comparison(comparison)}；"
                f"上期 {format_units(comparison['units'])}",
                f"> {period['start_date']} 至 {period['end_date']}；"
                f"上期 {format_cny(comparison['cny_amount'])}",
                "",
            ]
        )

    coverage = report["coverage"]
    assert isinstance(coverage, dict)
    lines.append(
        f"> 数据覆盖：取得 {coverage['report_days']}/{coverage['requested_days']} 份日报；"
        f"其余 {coverage['no_report_days']} 天 Apple 未提供报表（通常表示当天无销售单位）"
    )
    lines.append("")
    lines.append("> 汇率：每个报表月固定使用上一个自然月的日均参考汇率；月内不变")
    lines.append("")
    lines.append("> Sales and Trends 为预估收益，正式结算以 Finance Report 为准")
    return "\n".join(lines)


def split_markdown(markdown: str, max_bytes: int = MAX_WECOM_MESSAGE_BYTES) -> list[str]:
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须大于 0")
    chunks: list[str] = []
    current_lines: list[str] = []

    def flush() -> None:
        if current_lines:
            chunks.append("\n".join(current_lines).strip())
            current_lines.clear()

    for line in markdown.splitlines():
        if len(line.encode("utf-8")) > max_bytes:
            flush()
            remaining = line
            while remaining:
                cut = len(remaining)
                while cut > 0 and len(remaining[:cut].encode("utf-8")) > max_bytes:
                    cut -= 1
                if cut == 0:
                    raise ValueError("无法按 UTF-8 安全拆分消息")
                chunks.append(remaining[:cut])
                remaining = remaining[cut:]
            continue

        candidate = "\n".join([*current_lines, line])
        if current_lines and len(candidate.encode("utf-8")) > max_bytes:
            flush()
        current_lines.append(line)
    flush()
    return chunks or [""]


def validate_wecom_webhook_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "qyapi.weixin.qq.com":
        raise ReporterError("WECOM_WEBHOOK_URL 必须是 qyapi.weixin.qq.com 的 HTTPS Webhook 地址")
    if parsed.path != "/cgi-bin/webhook/send" or not parse_qs(parsed.query).get("key"):
        raise ReporterError("WECOM_WEBHOOK_URL 路径或 key 参数无效")


def send_wecom_markdown(
    webhook_url: str,
    markdown: str,
    *,
    session: requests.Session | None = None,
    timeout_seconds: float = 15,
    max_attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    validate_wecom_webhook_url(webhook_url)
    client = session or requests.Session()
    # Reserve room for the continuation marker added to split messages.
    chunks = split_markdown(markdown, max_bytes=MAX_WECOM_MESSAGE_BYTES - 64)
    for index, chunk in enumerate(chunks, start=1):
        if len(chunks) > 1:
            chunk = f"{chunk}\n\n> 第 {index}/{len(chunks)} 段"
        for attempt in range(max_attempts):
            response: requests.Response | None = None
            try:
                response = client.post(
                    webhook_url,
                    json={"msgtype": "markdown", "markdown": {"content": chunk}},
                    timeout=timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt + 1 == max_attempts:
                    raise ReporterError("企业微信 Webhook 网络请求失败，且重试仍未成功") from exc
                sleep(_retry_delay(None, attempt))
                continue

            if response.status_code in RETRYABLE_HTTP_STATUSES and attempt + 1 < max_attempts:
                sleep(_retry_delay(response, attempt))
                continue
            if not 200 <= response.status_code < 300:
                raise ReporterError(f"企业微信 Webhook 返回 HTTP {response.status_code}")
            try:
                result = response.json()
            except ValueError as exc:
                raise ReporterError("企业微信 Webhook 返回了无法解析的响应") from exc
            if result.get("errcode") != 0:
                code = result.get("errcode", "unknown")
                message = result.get("errmsg", "unknown error")
                raise ReporterError(f"企业微信发送失败（errcode={code}）：{message}")
            break
    return len(chunks)


def _read_cached_report(path: Path) -> bytes | None:
    if not path.exists():
        return None
    try:
        return gzip.decompress(path.read_bytes())
    except (OSError, gzip.BadGzipFile, EOFError):
        return None


def _write_cached_report(path: Path, tsv_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(tsv_text.encode("utf-8")))


def collect_daily_totals(
    download_report: Callable[[date], bytes | None],
    start_date: date,
    end_date: date,
    raw_dir: Path,
) -> dict[date, DailyTotals | None]:
    result: dict[date, DailyTotals | None] = {}
    for current in iter_dates(start_date, end_date):
        cache_path = raw_dir / f"{current.isoformat()}.tsv.gz"
        cached = _read_cached_report(cache_path)
        if cached is not None:
            tsv_text = decode_report_payload(cached)
            source = "缓存"
        else:
            payload = download_report(current)
            if payload is None:
                result[current] = None
                print(f"{current.isoformat()}：Apple 未提供日报")
                continue
            tsv_text = decode_report_payload(payload)
            _write_cached_report(cache_path, tsv_text)
            source = "Apple"
        totals = parse_daily_totals(tsv_text)
        result[current] = totals
        print(
            f"{current.isoformat()}：已从{source}读取，"
            f"{len(totals.amounts)} 个收益币种，净销售数量 {quantity_to_string(totals.units)}"
        )
    return result


def build_report(
    end_date: date,
    daily_totals: Mapping[date, DailyTotals | Mapping[str, Decimal] | None],
    fx_tables: Mapping[str, MonthlyFxRates] | None = None,
    auth_method: str | None = None,
) -> dict[str, object]:
    periods = build_periods(end_date)
    all_dates = list(iter_dates(required_start_date(end_date), end_date))
    report_days = sum(1 for current in all_dates if daily_totals.get(current) is not None)
    tables = fx_tables or {}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_timezone": "America/Los_Angeles",
        "end_date": end_date.isoformat(),
        "auth_method": auth_method,
        "definitions": {
            "revenue": "Units multiplied by Developer Proceeds, grouped by Currency of Proceeds",
            "units": (
                "net Units on rows with non-zero Developer Proceeds; excludes free "
                "downloads, re-downloads, and updates"
            ),
            "last_quarter": "rolling 90 Apple reporting days ending on end_date",
            "comparison": "immediately preceding period with the same number of days",
            "cny_conversion": (
                "each report month uses the average of daily source-currency-to-CNY "
                "rates from the previous calendar month"
            ),
        },
        "coverage": {
            "requested_days": len(all_dates),
            "report_days": report_days,
            "no_report_days": len(all_dates) - report_days,
        },
        "exchange_rates": {
            "base_currency": "CNY",
            "source": {"name": FX_SOURCE_NAME, "url": FX_API_URL},
            "months": {
                report_month: {
                    "source_start_date": table.source_start_date.isoformat(),
                    "source_end_date": table.source_end_date.isoformat(),
                    "rates": {
                        currency: format(rate, "f")
                        for currency, rate in sorted(table.rates.items())
                    },
                    "observation_counts": dict(sorted(table.observation_counts.items())),
                }
                for report_month, table in sorted(tables.items())
            },
        },
        "periods": summarize_periods(periods, daily_totals, tables),
    }


def write_outputs(output_dir: Path, report: Mapping[str, object], markdown: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "revenue-summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "revenue-summary.md").write_text(markdown + "\n", encoding="utf-8")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="汇总 App Store 预估收入并可发送到企业微信")
    parser.add_argument(
        "--end-date",
        default=os.environ.get("REPORT_END_DATE") or None,
        help="统计截止日 YYYY-MM-DD；默认取太平洋时间的昨天",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.environ.get("REPORT_OUTPUT_DIR", "output")),
        help="输出目录，默认 output",
    )
    parser.add_argument(
        "--send-wecom",
        action="store_true",
        help="发送 Markdown 摘要到 WECOM_WEBHOOK_URL",
    )
    return parser.parse_args(argv)


def _parse_end_date(value: str | None) -> date:
    if value is None:
        return report_end_date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ReporterError("--end-date/REPORT_END_DATE 必须是 YYYY-MM-DD") from exc


def run(args: argparse.Namespace) -> dict[str, object]:
    end_date = _parse_end_date(args.end_date)
    webhook_url = ""
    if args.send_wecom:
        webhook_url = os.environ.get("WECOM_WEBHOOK_URL", "").strip()
        if not webhook_url:
            raise ReporterError("使用 --send-wecom 时必须设置 WECOM_WEBHOOK_URL")
        validate_wecom_webhook_url(webhook_url)

    auth_method = get_auth_method()
    start_date = required_start_date(end_date)
    if auth_method == "reporter":
        reporter_config = ReporterConfig.from_env()
        reporter_jar = resolve_reporter_jar(reporter_config)
        with ReporterClient(reporter_config, reporter_jar) as reporter_client:
            daily_totals = collect_daily_totals(
                reporter_client.download_daily_report,
                start_date,
                end_date,
                args.output_dir / "raw",
            )
    else:
        api_config = AppStoreConfig.from_env()
        api_session = requests.Session()
        daily_totals = collect_daily_totals(
            lambda current: download_daily_report(api_session, api_config, current),
            start_date,
            end_date,
            args.output_dir / "raw",
        )
    fx_tables = load_fx_tables(daily_totals, args.output_dir / "fx-rates")
    report = build_report(
        end_date, daily_totals, fx_tables=fx_tables, auth_method=auth_method
    )
    markdown = render_markdown(report)
    write_outputs(args.output_dir, report, markdown)
    print("\n" + markdown)

    if args.send_wecom:
        message_count = send_wecom_markdown(webhook_url, markdown)
        print(f"企业微信发送成功（{message_count} 条消息）")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    try:
        run(parse_args(argv))
    except ReporterError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("错误：用户中断", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
