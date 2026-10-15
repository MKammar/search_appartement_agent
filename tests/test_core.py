from __future__ import annotations

import email
import email.policy
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rental_monitor.cli import main
from rental_monitor.core import (
    Criteria,
    check_criteria,
    normalize_listing_url,
    parse_alert,
    poll_once,
    _smtp_send,
)


def plain_alert(body: str, sender: str = "alerts@example.com") -> bytes:
    message = email.message.EmailMessage()
    message["From"] = sender
    message["To"] = "user@example.com"
    message["Subject"] = "New rentals"
    message.set_content(body)
    return message.as_bytes()


class ListingUrlTests(unittest.TestCase):
    def test_supported_domains_and_tracking_cleanup(self) -> None:
        normalized = normalize_listing_url(
            "https://www.pap.fr/annonces/logement-r123?utm_source=mail&foo=bar#photos"
        )
        self.assertEqual(normalized, ("https://pap.fr/annonces/logement-r123?foo=bar", "PAP"))

    def test_rejects_unrelated_hosts_and_lookalikes(self) -> None:
        self.assertIsNone(normalize_listing_url("https://example.com/listing"))
        self.assertIsNone(normalize_listing_url("https://notpap.fr/listing"))


class AlertParsingTests(unittest.TestCase):
    def test_extracts_deduplicated_site_links_and_ignores_other_links(self) -> None:
        raw = plain_alert(
            "Appartement 3 pièces, 42 m², 1 250 € CC à Paris\n"
            "https://www.seloger.com/annonces/locations/abc.htm?utm_source=email\n"
            "https://www.seloger.com/annonces/locations/abc.htm\n"
            "Unsubscribe https://example.com/unsubscribe"
        )
        listings = parse_alert(raw)
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].source, "SeLoger")
        self.assertIn("42 m²", listings[0].snippet)

    def test_sender_allowlist(self) -> None:
        raw = plain_alert("https://www.pap.fr/annonce/test", sender="someone@example.com")
        self.assertEqual(parse_alert(raw, ("alerts@pap.fr",)), [])
        spoofed = plain_alert(
            "https://www.pap.fr/annonce/test",
            sender="alerts@pap.fr.attacker.example",
        )
        self.assertEqual(parse_alert(spoofed, ("pap.fr",)), [])

    def test_parses_html_anchor_text(self) -> None:
        message = email.message.EmailMessage()
        message["From"] = "alerts@example.com"
        message.set_content(
            '<p>Appartement 2 pièces de 38 m² à Lyon, 890 €</p>'
            '<a href="https://www.bienici.com/annonce/xyz">Voir cet appartement</a>',
            subtype="html",
        )
        listings = parse_alert(message.as_bytes())
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].source, "Bien’ici")
        self.assertEqual(listings[0].title, "Voir cet appartement")


class CriteriaTests(unittest.TestCase):
    def test_matches_french_listing_with_grouped_euro_amount(self) -> None:
        criteria = Criteria.from_dict(
            {
                "include_any": ["Paris 11e", "Paris 12e"],
                "max_rent": 1300,
                "min_surface": 40,
                "min_rooms": 2,
            }
        )
        result = check_criteria(criteria, "Appartement 2 pièces Paris 11e", "42 m², 1 250 € CC")
        self.assertTrue(result.matches)
        self.assertEqual(result.unverified, ())

    def test_rejects_known_out_of_range_and_excluded_words(self) -> None:
        criteria = Criteria.from_dict(
            {"max_rent": 1000, "exclude_any": ["colocation"]}
        )
        self.assertFalse(check_criteria(criteria, "Appartement", "1 100 €").matches)
        self.assertFalse(check_criteria(criteria, "Colocation", "850 €").matches)

    def test_marks_missing_numeric_details_unverified(self) -> None:
        criteria = Criteria.from_dict({"max_rent": 1000, "min_surface": 30})
        result = check_criteria(criteria, "Appartement à louer", "Voir l'annonce")
        self.assertTrue(result.matches)
        self.assertEqual(set(result.unverified), {"rent", "surface"})

    def test_invalid_criteria_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Criteria.from_dict({"include_any": "Paris"})
        with self.assertRaises(ValueError):
            Criteria.from_dict({"max_rent": "1000"})
        with self.assertRaises(ValueError):
            Criteria.from_dict({"min_rooms": 2.5})


class PollTests(unittest.TestCase):
    class FakeImap:
        def __init__(self) -> None:
            self.messages = {
                1: plain_alert("Old listing https://www.pap.fr/annonce/old")
            }

        def select(self, _folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
            self.assert_readonly = readonly
            return "OK", [b"1"]

        def response(self, _name: str) -> tuple[str, list[bytes]]:
            return "UIDVALIDITY", [b"42"]

        def uid(self, command: str, *args: str) -> tuple[str, list[object]]:
            if command == "search":
                criterion = args[-1]
                if criterion == "ALL":
                    return "OK", [b"1"]
                start = int(criterion.removeprefix("UID ").split(":", 1)[0])
                return "OK", [" ".join(str(uid) for uid in self.messages if uid >= start).encode()]
            if command == "fetch":
                uid = int(args[0])
                return "OK", [(b"message", self.messages[uid])]
            raise AssertionError(f"Unexpected IMAP command: {command}")

        def logout(self) -> tuple[str, list[bytes]]:
            return "OK", [b"logout"]

    def test_baselines_old_email_then_notifies_new_listing_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "poll_interval_seconds": 300,
                        "notification_email": "recipient@example.test",
                        "mailbox": {"folder": "INBOX", "allowed_senders": []},
                        "criteria": {"include_any": ["Paris"]},
                    }
                ),
                encoding="utf-8",
            )
            database = root / "monitor.sqlite3"
            env_file = root / ".env"
            client = self.FakeImap()
            with (
                patch("rental_monitor.core._connect_imap", return_value=client),
                patch("rental_monitor.core._smtp_send") as send,
            ):
                self.assertEqual(poll_once(config, database, env_file), 0)
                client.messages[2] = plain_alert(
                    "Appartement à Paris https://www.pap.fr/annonce/new"
                )
                self.assertEqual(poll_once(config, database, env_file), 1)
                self.assertEqual(poll_once(config, database, env_file), 0)
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[2], "recipient@example.test")
                connection = sqlite3.connect(database)
                try:
                    self.assertEqual(
                        connection.execute("SELECT status FROM listings ORDER BY url").fetchall(),
                        [("sent",)],
                    )
                finally:
                    connection.close()


class SmtpTests(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {
            "SMTP_HOST": "smtp.test",
            "SMTP_PORT": "587",
            "SMTP_USE_SSL": "false",
            "SMTP_STARTTLS": "true",
            "SMTP_USER": "sender@test",
            "SMTP_PASSWORD": "secret",
            "SMTP_FROM": "sender@test",
            "SMTP_TO": "recipient@test",
        },
    )
    @patch("rental_monitor.core.smtplib.SMTP")
    def test_starttls_connection_sends_plain_text_email(self, smtp_class: MagicMock) -> None:
        client = smtp_class.return_value
        _smtp_send("New listings", "A new listing is available.", "recipient@test")
        smtp_class.assert_called_once_with("smtp.test", 587, timeout=30)
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("sender@test", "secret")
        sent = client.send_message.call_args.args[0]
        self.assertEqual(sent["To"], "recipient@test")
        self.assertEqual(sent.get_content().strip(), "A new listing is available.")


class CliTests(unittest.TestCase):
    def test_init_creates_config_and_secret_template_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            env_file = root / ".env"
            with contextlib.redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "--config",
                        str(config),
                        "--env-file",
                        str(env_file),
                        "init",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["mailbox"]["folder"], "INBOX")
            self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["notification_email"], "")
            self.assertIn("IMAP_PASSWORD=", env_file.read_text(encoding="utf-8"))
            with contextlib.redirect_stdout(io.StringIO()):
                second_result = main(
                    [
                        "--config",
                        str(config),
                        "--env-file",
                        str(env_file),
                        "init",
                    ]
                )
            self.assertEqual(second_result, 1)


if __name__ == "__main__":
    unittest.main()
