import base64
import gzip
import hashlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat


SCRIPT = Path(__file__).parents[1] / "scripts" / "fetch_appstore_revenue.py"
SPEC = importlib.util.spec_from_file_location("fetch_appstore_revenue", SCRIPT)
reporter = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = reporter
SPEC.loader.exec_module(reporter)


def make_config():
    key = ec.generate_private_key(ec.SECP256R1())
    return reporter.AppStoreConfig("issuer", "key-id", "vendor", key)


def make_fx_tables(start_date, end_date, **rates):
    tables = {}
    current = start_date
    while current <= end_date:
        month = current.strftime("%Y-%m")
        source_start, source_end = reporter._previous_month_range(month)
        tables[month] = reporter.MonthlyFxRates(
            month,
            source_start,
            source_end,
            {"CNY": Decimal("1"), **rates},
            {currency: 30 for currency in rates},
        )
        current = (current.replace(day=28) + reporter.timedelta(days=4)).replace(day=1)
    return tables


class FakeResponse:
    def __init__(self, status_code=200, content=b"", payload=None, headers=None):
        self.status_code = status_code
        self.content = content
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.get_calls = []
        self.post_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        return self.responses.pop(0)

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self.responses.pop(0)


class ConfigTests(unittest.TestCase):
    def test_reports_all_missing_environment_variables(self):
        with self.assertRaisesRegex(reporter.ReporterError, "ASC_ISSUER_ID.*ASC_KEY_ID"):
            reporter.AppStoreConfig.from_env({})

    def test_loads_base64_private_key(self):
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        env = {
            "ASC_ISSUER_ID": " issuer ",
            "ASC_KEY_ID": " key ",
            "ASC_VENDOR_NUMBER": " vendor ",
            "ASC_PRIVATE_KEY_BASE64": base64.b64encode(pem).decode(),
        }
        config = reporter.AppStoreConfig.from_env(env)
        self.assertEqual(config.issuer_id, "issuer")
        self.assertIsInstance(config.private_key, ec.EllipticCurvePrivateKey)

    def test_reporter_config_only_requires_token_and_vendor(self):
        config = reporter.ReporterConfig.from_env(
            {
                "ASC_REPORTER_ACCESS_TOKEN": "reporter-token",
                "ASC_VENDOR_NUMBER": "80012345",
                "ASC_REPORTER_ACCOUNT": "12345",
            }
        )
        self.assertEqual(config.access_token, "reporter-token")
        self.assertEqual(config.vendor_number, "80012345")
        self.assertEqual(config.account_number, "12345")

    def test_reporter_is_default_auth_method(self):
        self.assertEqual(reporter.get_auth_method({}), "reporter")
        self.assertEqual(reporter.get_auth_method({"ASC_AUTH_METHOD": "api-key"}), "api_key")

    def test_reporter_rejects_non_numeric_vendor(self):
        with self.assertRaisesRegex(reporter.ReporterError, "Vendor Number"):
            reporter.ReporterConfig.from_env(
                {
                    "ASC_REPORTER_ACCESS_TOKEN": "reporter-token",
                    "ASC_VENDOR_NUMBER": "not-a-number",
                }
            )


class JwtTests(unittest.TestCase):
    def test_jwt_claims_and_expiry(self):
        config = make_config()
        token = reporter.build_jwt(config, now=1_700_000_000)
        header = jwt.get_unverified_header(token)
        payload = jwt.decode(token, options={"verify_signature": False})
        self.assertEqual(header["alg"], "ES256")
        self.assertEqual(header["kid"], "key-id")
        self.assertEqual(payload["iss"], "issuer")
        self.assertEqual(payload["aud"], "appstoreconnect-v1")
        self.assertEqual(payload["exp"] - payload["iat"], 300)


class DateTests(unittest.TestCase):
    def test_end_date_uses_pacific_time(self):
        now = datetime(2026, 7, 14, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(reporter.report_end_date(now), date(2026, 7, 12))

    def test_end_date_uses_yesterday_after_pacific_availability_time(self):
        now = datetime(2026, 7, 14, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(reporter.report_end_date(now), date(2026, 7, 13))

    def test_periods_are_inclusive(self):
        periods = reporter.build_periods(date(2026, 7, 13))
        self.assertEqual(periods[1].start_date, date(2026, 7, 7))
        self.assertEqual(periods[2].days, 30)
        self.assertEqual(periods[3].days, 90)

    def test_comparison_requires_180_days(self):
        end = date(2026, 7, 13)
        self.assertEqual(reporter.required_start_date(end), date(2026, 1, 15))
        previous = reporter.comparison_period(reporter.build_periods(end)[1])
        self.assertEqual(previous.start_date, date(2026, 6, 30))
        self.assertEqual(previous.end_date, date(2026, 7, 6))


class ParsingTests(unittest.TestCase):
    TSV = (
        "Title\tUnits\tDeveloper Proceeds\tCurrency of Proceeds\n"
        "App A\t2\t0.70\tUSD\n"
        "App B\t3\t1.25\tUSD\n"
        "App A refund\t-1\t0.70\tUSD\n"
        "App C\t2\t4.50\tCNY\n"
    )

    def test_decompresses_and_multiplies_units(self):
        text = reporter.decode_report_payload(gzip.compress(self.TSV.encode()))
        totals = reporter.parse_report_totals(text)
        self.assertEqual(totals, {"USD": Decimal("4.45"), "CNY": Decimal("9.00")})

        daily = reporter.parse_daily_totals(text)
        self.assertEqual(daily.units, Decimal("6"))
        self.assertEqual(daily.amounts, totals)

    def test_supports_legacy_currency_header(self):
        tsv = "Units\tDeveloper Proceeds\tDeveloper Proceeds Currency\n2\t1.50\teur\n"
        self.assertEqual(reporter.parse_report_totals(tsv), {"EUR": Decimal("3.00")})

    def test_sales_units_exclude_free_updates_and_downloads(self):
        tsv = (
            "Units\tDeveloper Proceeds\tCurrency of Proceeds\tProduct Type Identifier\n"
            "152\t0.00\tCNY\tF7\n"
            "3\t1.00\tCNY\tIA1\n"
            "-1\t1.00\tCNY\tIA1\n"
        )
        totals = reporter.parse_daily_totals(tsv)
        self.assertEqual(totals.units, Decimal("2"))
        self.assertEqual(totals.amounts, {"CNY": Decimal("2.00")})

    def test_rejects_missing_required_columns(self):
        with self.assertRaisesRegex(reporter.ReporterError, "Developer Proceeds"):
            reporter.parse_report_totals("Units\tCurrency of Proceeds\n1\tUSD\n")


class ApiTests(unittest.TestCase):
    @patch.object(reporter, "build_jwt", return_value="token")
    def test_download_includes_report_date(self, _):
        body = gzip.compress(b"a\tb\n")
        session = FakeSession([FakeResponse(content=body)])
        result = reporter.download_daily_report(session, make_config(), date(2026, 7, 13))
        self.assertEqual(result, body)
        params = session.get_calls[0][1]["params"]
        self.assertEqual(params["filter[reportDate]"], "2026-07-13")
        self.assertEqual(params["filter[frequency]"], "DAILY")

    @patch.object(reporter, "build_jwt", return_value="token")
    def test_404_means_no_report(self, _):
        session = FakeSession([FakeResponse(status_code=404)])
        self.assertIsNone(
            reporter.download_daily_report(session, make_config(), date(2026, 7, 13))
        )


class AppleReporterTests(unittest.TestCase):
    def test_parses_robot_mode_error_and_retry(self):
        output = (
            "<?xml version='1.0'?><Error><Code>117</Code>"
            "<Message>Daily reports are delayed.</Message><Retry>2500</Retry></Error>"
        )
        self.assertEqual(
            reporter._parse_reporter_error(output),
            ("117", "Daily reports are delayed.", 2.5),
        )

    def test_verifies_reporter_jar_manifest(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "META-INF/MANIFEST.MF",
                "Main-Class: com.apple.gbi.autoingest.client.Reporter\n",
            )
        data = buffer.getvalue()
        reporter._verify_reporter_jar(data, hashlib.sha256(data).hexdigest())

    @patch.object(reporter.shutil, "which", return_value="/usr/bin/java")
    def test_reporter_client_uses_token_properties_without_password(self, _):
        seen = {}

        def runner(command, **kwargs):
            work_dir = Path(kwargs["cwd"])
            properties_path = work_dir / command[3].removeprefix("p=")
            seen["command"] = command
            seen["properties"] = properties_path.read_text()
            (work_dir / "S_D_80012345_20260713.txt.gz").write_bytes(b"report")
            return subprocess.CompletedProcess(command, 0, "<Output />", "")

        config = reporter.ReporterConfig(
            "secret-reporter-token", "80012345", account_number="12345"
        )
        with reporter.ReporterClient(
            config,
            Path("/tmp/Reporter.jar"),
            runner=runner,
            sleep=lambda _: None,
        ) as client:
            self.assertEqual(client.download_daily_report(date(2026, 7, 13)), b"report")

        self.assertNotIn("secret-reporter-token", " ".join(seen["command"]))
        self.assertEqual(
            seen["command"][-5:],
            ["80012345,", "Sales,", "Summary,", "Daily,", "20260713"],
        )
        self.assertIn("AccessToken=secret-reporter-token", seen["properties"])
        self.assertIn("Account=12345", seen["properties"])
        self.assertNotIn("Password", seen["properties"])
        self.assertNotIn("Username", seen["properties"])

    @patch.object(reporter.shutil, "which", return_value="/usr/bin/java")
    def test_reporter_error_213_means_no_sales(self, _):
        def runner(command, **_kwargs):
            output = (
                "<Error><Code>213</Code>"
                "<Message>There were no sales for the date specified.</Message></Error>"
            )
            return subprocess.CompletedProcess(command, 0, output, "")

        config = reporter.ReporterConfig("token", "80012345")
        with reporter.ReporterClient(
            config,
            Path("/tmp/Reporter.jar"),
            runner=runner,
            sleep=lambda _: None,
        ) as client:
            self.assertIsNone(client.download_daily_report(date(2026, 7, 13)))


class FxRateTests(unittest.TestCase):
    def test_averages_inverted_daily_rates(self):
        session = FakeSession(
            [
                FakeResponse(
                    payload=[
                        {"date": "2026-06-01", "base": "CNY", "quote": "USD", "rate": 0.2},
                        {"date": "2026-06-02", "base": "CNY", "quote": "USD", "rate": 0.25},
                    ]
                )
            ]
        )
        rates, counts = reporter._fetch_monthly_fx_rates(
            "2026-07", {"USD"}, session=session
        )
        self.assertEqual(rates["USD"], Decimal("4.500000000000"))
        self.assertEqual(counts, {"USD": 2})
        params = session.get_calls[0][1]["params"]
        self.assertEqual(params["from"], "2026-06-01")
        self.assertEqual(params["to"], "2026-06-30")

    def test_monthly_rate_cache_is_reused_without_request(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            first_session = FakeSession(
                [
                    FakeResponse(
                        payload=[
                            {
                                "date": "2026-06-01",
                                "base": "CNY",
                                "quote": "USD",
                                "rate": 0.2,
                            }
                        ]
                    )
                ]
            )
            first = reporter.load_monthly_fx_rates(
                "2026-07", {"USD"}, cache_dir, session=first_session
            )
            second_session = FakeSession([])
            second = reporter.load_monthly_fx_rates(
                "2026-07", {"USD"}, cache_dir, session=second_session
            )
            self.assertEqual(first.rates, second.rates)
            self.assertEqual(second_session.get_calls, [])

    def test_missing_currency_fails_instead_of_being_omitted(self):
        session = FakeSession([FakeResponse(payload=[])])
        with self.assertRaisesRegex(reporter.ReporterError, "缺少.*USD"):
            reporter._fetch_monthly_fx_rates("2026-07", {"USD"}, session=session)


class SummaryTests(unittest.TestCase):
    def test_period_summary_keeps_original_currencies_and_adds_cny(self):
        end = date(2026, 7, 13)
        start = reporter.required_start_date(end)
        daily = {
            current: {"USD": Decimal("1.00"), "CNY": Decimal("2.00")}
            for current in reporter.iter_dates(start, end)
        }
        daily[end - reporter.timedelta(days=1)] = None
        fx_tables = make_fx_tables(start, end, USD=Decimal("7"))
        report = reporter.build_report(end, daily, fx_tables)
        period = report["periods"]["last_7_days"]
        amounts = period["amounts"]
        self.assertEqual(amounts, {"CNY": "12.00", "USD": "6.00"})
        self.assertEqual(period["cny_amount"], "54.00")
        self.assertEqual(report["coverage"]["no_report_days"], 1)

    def test_markdown_contains_all_periods(self):
        end = date(2026, 7, 13)
        daily = {
            current: {"CNY": Decimal("1")}
            for current in reporter.iter_dates(reporter.required_start_date(end), end)
        }
        markdown = reporter.render_markdown(reporter.build_report(end, daily))
        self.assertIn("最新可用日报日", markdown)
        self.assertIn("最近 7 天", markdown)
        self.assertIn("最近 30 天", markdown)
        self.assertIn("滚动 90 天", markdown)
        self.assertIn("统一折算为 CNY", markdown)
        self.assertIn("上期 ¥", markdown)

    def test_zero_amount_currencies_are_hidden(self):
        end = date(2026, 7, 13)
        daily = {
            current: {"USD": Decimal("0.00"), "CNY": Decimal("1.00")}
            for current in reporter.iter_dates(reporter.required_start_date(end), end)
        }
        report = reporter.build_report(end, daily)
        amounts = report["periods"]["last_7_days"]["amounts"]
        self.assertEqual(amounts, {"CNY": "7.00"})

    def test_period_over_period_change_is_calculated(self):
        end = date(2026, 7, 13)
        daily = {
            current: {"CNY": Decimal("10")}
            for current in reporter.iter_dates(reporter.required_start_date(end), end)
        }
        for current in reporter.iter_dates(date(2026, 7, 7), end):
            daily[current] = {"CNY": Decimal("12")}
        report = reporter.build_report(end, daily)
        comparison = report["periods"]["last_7_days"]["comparison"]
        self.assertEqual(comparison["cny_amount"], "70.00")
        self.assertEqual(comparison["change_amount_cny"], "14.00")
        self.assertEqual(comparison["change_percent"], "20")
        self.assertEqual(comparison["direction"], "up")
        self.assertIn(
            '<font color="info">+20%</font>', reporter.render_markdown(report)
        )

    def test_sales_units_are_compared_with_previous_period(self):
        end = date(2026, 7, 13)
        daily = {
            current: reporter.DailyTotals({"CNY": Decimal("10")}, Decimal("10"))
            for current in reporter.iter_dates(reporter.required_start_date(end), end)
        }
        for current in reporter.iter_dates(date(2026, 7, 7), end):
            daily[current] = reporter.DailyTotals(
                {"CNY": Decimal("12")}, Decimal("12")
            )

        report = reporter.build_report(end, daily)
        period = report["periods"]["last_7_days"]
        comparison = period["comparison"]
        self.assertEqual(period["units"], "84")
        self.assertEqual(comparison["units"], "70")
        self.assertEqual(comparison["change_units"], "14")
        self.assertEqual(comparison["unit_change_percent"], "20")
        self.assertEqual(comparison["unit_direction"], "up")
        markdown = reporter.render_markdown(report)
        self.assertIn("销售数量 84", markdown)
        self.assertIn("上期 70", markdown)

    def test_refund_units_reduce_sales_quantity(self):
        end = date(2026, 7, 13)
        daily = {
            current: reporter.DailyTotals({}, Decimal("0"))
            for current in reporter.iter_dates(reporter.required_start_date(end), end)
        }
        daily[end] = reporter.DailyTotals({"CNY": Decimal("7")}, Decimal("9"))
        daily[end - reporter.timedelta(days=1)] = reporter.DailyTotals(
            {"CNY": Decimal("-1")}, Decimal("-1")
        )
        period = reporter.build_report(end, daily)["periods"]["last_7_days"]
        self.assertEqual(period["units"], "8")

    def test_zero_previous_period_is_shown_as_new(self):
        end = date(2026, 7, 13)
        daily = {
            current: {}
            for current in reporter.iter_dates(reporter.required_start_date(end), end)
        }
        daily[end] = {"CNY": Decimal("10")}
        report = reporter.build_report(end, daily)
        comparison = report["periods"]["yesterday"]["comparison"]
        self.assertIsNone(comparison["change_percent"])
        self.assertEqual(comparison["direction"], "new")

    def test_decrease_is_rendered_in_wecom_warning_color(self):
        end = date(2026, 7, 13)
        daily = {
            current: {"CNY": Decimal("10")}
            for current in reporter.iter_dates(reporter.required_start_date(end), end)
        }
        for current in reporter.iter_dates(date(2026, 7, 7), end):
            daily[current] = {"CNY": Decimal("8")}
        report = reporter.build_report(end, daily)
        comparison = report["periods"]["last_7_days"]["comparison"]
        self.assertEqual(comparison["change_percent"], "-20")
        self.assertIn(
            '<font color="warning">-20%</font>', reporter.render_markdown(report)
        )


class WeComTests(unittest.TestCase):
    URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"

    def test_sends_markdown_payload(self):
        session = FakeSession([FakeResponse(payload={"errcode": 0, "errmsg": "ok"})])
        count = reporter.send_wecom_markdown(self.URL, "# 收入\nUSD 1.00", session=session)
        self.assertEqual(count, 1)
        payload = session.post_calls[0][1]["json"]
        self.assertEqual(payload["msgtype"], "markdown")

    def test_rejects_non_wecom_url(self):
        with self.assertRaises(reporter.ReporterError):
            reporter.validate_wecom_webhook_url("https://example.com/hook?key=x")

    def test_run_checks_webhook_before_downloading_reports(self):
        args = reporter.parse_args(["--send-wecom"])
        with patch.dict(reporter.os.environ, {}, clear=True):
            with self.assertRaisesRegex(reporter.ReporterError, "WECOM_WEBHOOK_URL"):
                reporter.run(args)

    def test_split_markdown_respects_utf8_byte_limit(self):
        chunks = reporter.split_markdown("第一行\n" + "收" * 30, max_bytes=20)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.encode()) <= 20 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
