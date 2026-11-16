from __future__ import annotations

from datetime import datetime, timezone
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import availability
from domains.availability.checker import (
    AvailabilityResult,
    check_availability,
    checks_due,
    modes_due,
)
from infrastructure.database_targets import DatabaseTarget
from infrastructure.mailer import send_mail


IST = ZoneInfo("Asia/Kolkata")
CFG = {
    "availability": {
        "timezone": "Asia/Kolkata",
        "checks": {
            "charge": {
                "tables": [
                    {
                        "table": "offline_feed.charge_data",
                        "label": "Charge",
                        "window_hours": 1,
                        "due_hours": "all",
                    }
                ],
            },
            "hot_metal": {
                "tables": [
                    {
                        "table": "offline_feed.hot_metal_slag_analysis",
                        "label": "Hot Metal",
                        "window_hours": 4,
                        "due_hours": [3, 7, 11, 15, 19, 23],
                    }
                ],
            },
            "rm": {
                "tables": [
                    {
                        "table": "offline_feed.fuel_chemistry",
                        "label": "RM - Fuel",
                        "window_hours": 8,
                        "due_hours": [0, 8, 16],
                    },
                    {
                        "table": "offline_feed.sinter_chemistry",
                        "label": "RM - Sinter",
                        "window_hours": 4,
                        "due_hours": [0, 4, 8, 12, 16, 20],
                    },
                ],
            },
        },
        "mail": {},
    },
    "write_db": ["pi_db", "neon_db"],
    "pi_db": {"url": "postgres://pi"},
    "neon_developer": {"url": "postgres://neon"},
}


class FakeCursor:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.calls.append((query, params))

    def fetchone(self):
        value = self.rows.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeConnection:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def cursor(self):
        return FakeCursor(self.rows, self.calls)


class FakeClient:
    def __init__(self, rows):
        self.calls = []
        self.conn = FakeConnection(list(rows), self.calls)
        self.closed = False

    def close(self):
        self.closed = True


class AvailabilityCheckerTests(unittest.TestCase):
    def test_due_mode_selection_per_hour(self):
        expected = {
            0: ["charge", "rm"],
            2: ["charge"],
            3: ["charge", "hot_metal"],
            4: ["charge", "rm"],
            7: ["charge", "hot_metal"],
            8: ["charge", "rm"],
            12: ["charge", "rm"],
            15: ["charge", "hot_metal"],
            16: ["charge", "rm"],
            20: ["charge", "rm"],
            23: ["charge", "hot_metal"],
        }
        for hour, modes in expected.items():
            with self.subTest(hour=hour):
                now = datetime(2026, 11, 13, hour, 5, tzinfo=IST)
                self.assertEqual(modes_due(CFG, now), modes)

    def test_rm_tables_have_independent_due_hours(self):
        at_0405 = checks_due(CFG, datetime(2026, 11, 13, 4, 5, tzinfo=IST))
        at_0805 = checks_due(CFG, datetime(2026, 11, 13, 8, 5, tzinfo=IST))

        self.assertEqual(at_0405["rm"], ["offline_feed.sinter_chemistry"])
        self.assertEqual(
            at_0805["rm"],
            [
                "offline_feed.fuel_chemistry",
                "offline_feed.sinter_chemistry",
            ],
        )

    def test_rm_tables_use_their_independent_inclusive_windows(self):
        client = FakeClient([(1, None), (1, None)])
        target = DatabaseTarget("pi_db", "PI_DB", {"url": "postgres://pi"})
        now = datetime(2026, 11, 13, 8, 5, tzinfo=IST)

        check_availability(
            CFG, [target], ["rm"], now, client_factory=lambda _cfg: client
        )

        fuel_start = datetime(2026, 11, 12, 18, 30, tzinfo=timezone.utc)
        sinter_start = datetime(2026, 11, 12, 22, 30, tzinfo=timezone.utc)
        self.assertEqual(client.calls[0][1][0], fuel_start)
        self.assertEqual(client.calls[1][1][0], sinter_start)
        self.assertIn("date_time >= %s", client.calls[0][0])
        self.assertIn("SELECT MAX(date_time)", client.calls[0][0])
        self.assertEqual(client.calls[0][1][1], client.calls[0][1][2])

    def test_due_table_filter_runs_only_sinter_at_0405(self):
        client = FakeClient([(2, None), (2, None)])
        target = DatabaseTarget("pi_db", "PI_DB", {"url": "postgres://pi"})
        now = datetime(2026, 11, 13, 4, 5, tzinfo=IST)

        results = check_availability(
            CFG,
            [target],
            ["charge", "rm"],
            now,
            tables_by_mode=checks_due(CFG, now),
            client_factory=lambda _cfg: client,
        )

        self.assertEqual(
            [result.table for result in results],
            ["offline_feed.charge_data", "offline_feed.sinter_chemistry"],
        )

    def test_rm_reports_only_fuel_as_missing(self):
        client = FakeClient([(0, None), (3, datetime(2026, 11, 13, 1, 30, tzinfo=timezone.utc))])
        target = DatabaseTarget("pi_db", "PI_DB", {"url": "postgres://pi"})

        results = check_availability(
            CFG,
            [target],
            ["rm"],
            datetime(2026, 11, 13, 7, 5, tzinfo=IST),
            client_factory=lambda _cfg: client,
        )

        missing = [result.table for result in results if result.status == "MISSING"]
        self.assertEqual(missing, ["offline_feed.fuel_chemistry"])
        self.assertEqual(results[1].status, "OK")

    def test_one_target_error_does_not_stop_the_other(self):
        good_client = FakeClient([(2, datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc))])

        def factory(target_cfg):
            if target_cfg["url"] == "postgres://pi":
                raise OSError("database unavailable")
            return good_client

        targets = [
            DatabaseTarget("pi_db", "PI_DB", {"url": "postgres://pi"}),
            DatabaseTarget("neon_developer", "NeonDB developer", {"url": "postgres://neon"}),
        ]
        results = check_availability(
            CFG,
            targets,
            ["charge"],
            datetime(2026, 11, 13, 7, 5, tzinfo=IST),
            client_factory=factory,
        )

        self.assertEqual([result.status for result in results], ["ERROR", "OK"])
        self.assertEqual(results[0].target_label, "PI_DB")
        self.assertEqual(results[1].target_label, "NeonDB developer")


class AvailabilityCliTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger(f"test-availability-{id(self)}")
        self.logger.addHandler(logging.NullHandler())

    @staticmethod
    def _result(status="OK"):
        return AvailabilityResult(
            mode="charge",
            target_label="PI_DB",
            table="offline_feed.charge_data",
            window_start=datetime(2026, 11, 13, 0, 30, tzinfo=timezone.utc),
            window_end=datetime(2026, 11, 13, 1, 35, tzinfo=timezone.utc),
            row_count=1 if status == "OK" else 0,
            latest_date_time=None,
            status=status,
        )

    @patch.object(availability, "send_mail")
    @patch.object(availability, "check_availability")
    def test_no_mail_when_all_checks_are_ok(self, check, mail):
        check.return_value = [self._result("OK")]
        code = availability.run(
            CFG,
            modes=["charge"],
            now=datetime(2026, 11, 13, 7, 5, tzinfo=IST),
            dry_run=False,
            logger=self.logger,
        )
        self.assertEqual(code, 0)
        mail.assert_not_called()

    @patch.object(availability, "send_mail")
    @patch.object(availability, "check_availability")
    def test_dry_run_suppresses_alert_mail(self, check, mail):
        check.return_value = [self._result("MISSING")]
        code = availability.run(
            CFG,
            modes=["charge"],
            now=datetime(2026, 11, 13, 7, 5, tzinfo=IST),
            dry_run=True,
            logger=self.logger,
        )
        self.assertEqual(code, 0)
        mail.assert_not_called()

    def test_alert_email_groups_databases_in_a_readable_format(self):
        window_start = datetime(2026, 11, 16, 2, 30, tzinfo=timezone.utc)
        window_end = datetime(2026, 11, 16, 6, 49, tzinfo=timezone.utc)
        latest = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
        results = [
            AvailabilityResult(
                mode="rm",
                target_label=target,
                table="offline_feed.sinter_chemistry",
                window_start=window_start,
                window_end=window_end,
                row_count=0,
                latest_date_time=latest,
                status="MISSING",
                check_label="RM - Sinter",
                window_hours=4,
            )
            for target in ("PI_DB", "NeonDB developer")
        ]

        subject, body = availability._alert_message(results, IST)

        self.assertEqual(
            subject,
            "[BF2 Offline Data] Availability alert: RM - Sinter",
        )
        self.assertIn("Expected interval: Last 4 hours", body)
        self.assertIn("- PI_DB: No data", body)
        self.assertIn("- NeonDB developer: No data", body)
        self.assertIn("16-11-2026 07:30 IST", body)
        self.assertNotIn("status=MISSING", body)


class MailerTests(unittest.TestCase):
    @patch("infrastructure.mailer.smtplib.SMTP")
    def test_mailer_uses_starttls_and_sends_once(self, smtp_cls):
        smtp = smtp_cls.return_value.__enter__.return_value
        send_mail(
            {
                "smtp_host": "smtp.example.com",
                "smtp_port": "587",
                "user": "alerts",
                "password": "secret",
                "from_name": "Data Availability - Evonith",
                "from": "alerts@example.com",
                "to": "one@example.com, two@example.com",
            },
            "subject",
            "body",
        )
        smtp.starttls.assert_called_once_with()
        smtp.login.assert_called_once_with("alerts", "secret")
        smtp.send_message.assert_called_once()
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(
            message["From"],
            "Data Availability - Evonith <alerts@example.com>",
        )


if __name__ == "__main__":
    unittest.main()
