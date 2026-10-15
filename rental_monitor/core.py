from __future__ import annotations

import email
import email.policy
import html
import imaplib
import json
import logging
import math
import os
import re
import smtplib
import sqlite3
import ssl
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LOGGER = logging.getLogger("rental_monitor")
SUPPORTED_HOSTS = {
    "seloger.com": "SeLoger",
    "bienici.com": "Bien’ici",
    "pap.fr": "PAP",
}
TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}
URL_PATTERN = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
MONEY_PATTERN = re.compile(r"(?<!\w)(\d[\d\s\u00a0\u202f.,]*)\s*(?:€|euros?)", re.IGNORECASE)
SURFACE_PATTERN = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s*m(?:²|2)\b", re.IGNORECASE)
ROOM_PATTERN = re.compile(r"(?<!\w)(\d+)\s*(?:pi[eè]ces?|p)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Criteria:
    include_any: tuple[str, ...] = ()
    include_all: tuple[str, ...] = ()
    exclude_any: tuple[str, ...] = ()
    min_rent: float | None = None
    max_rent: float | None = None
    min_surface: float | None = None
    max_surface: float | None = None
    min_rooms: int | None = None

    @classmethod
    def from_dict(cls, values: dict[str, object]) -> Criteria:
        def words(name: str) -> tuple[str, ...]:
            value = values.get(name, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"criteria.{name} must be a list of strings")
            return tuple(item.strip() for item in value if item.strip())

        def number(name: str, integer: bool = False) -> float | int | None:
            value = values.get(name)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"criteria.{name} must be a number or null")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"criteria.{name} must be finite and nonnegative")
            if integer and int(value) != value:
                raise ValueError(f"criteria.{name} must be a whole number")
            return int(value) if integer else float(value)

        return cls(
            include_any=words("include_any"),
            include_all=words("include_all"),
            exclude_any=words("exclude_any"),
            min_rent=number("min_rent"),
            max_rent=number("max_rent"),
            min_surface=number("min_surface"),
            max_surface=number("max_surface"),
            min_rooms=number("min_rooms", integer=True),
        )


@dataclass(frozen=True)
class Listing:
    url: str
    source: str
    title: str
    snippet: str
    discovered_at: str


@dataclass(frozen=True)
class MatchResult:
    matches: bool
    unverified: tuple[str, ...]


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(char for char in decomposed if not unicodedata.combining(char)).split())


def normalize_listing_url(raw_url: str) -> tuple[str, str] | None:
    cleaned = html.unescape(raw_url).rstrip(".,;:!?)\"]}")
    parts = urlsplit(cleaned)
    host = (parts.hostname or "").lower().removeprefix("www.")
    source = next(
        (label for domain, label in SUPPORTED_HOSTS.items() if host == domain or host.endswith(f".{domain}")),
        None,
    )
    if source is None or parts.scheme.lower() not in {"http", "https"}:
        return None
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in TRACKING_PARAMETERS and not key.casefold().startswith("utm_")
    ]
    normalized = urlunsplit(
        (parts.scheme.lower(), host, parts.path.rstrip("/") or "/", urlencode(query), "")
    )
    return normalized, source


def _number(value: str) -> float | None:
    cleaned = value.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _first_number(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    return _number(match.group(1)) if match else None


def check_criteria(criteria: Criteria, title: str, snippet: str) -> MatchResult:
    content = normalize_text(f"{title}\n{snippet}")
    if any(normalize_text(term) in content for term in criteria.exclude_any):
        return MatchResult(False, ())
    if criteria.include_any and not any(normalize_text(term) in content for term in criteria.include_any):
        return MatchResult(False, ())
    if any(normalize_text(term) not in content for term in criteria.include_all):
        return MatchResult(False, ())

    unverified: list[str] = []
    rent = _first_number(MONEY_PATTERN, content)
    surface = _first_number(SURFACE_PATTERN, content)
    rooms = _first_number(ROOM_PATTERN, content)
    bounds = (
        ("rent", rent, criteria.min_rent, criteria.max_rent),
        ("surface", surface, criteria.min_surface, criteria.max_surface),
        ("rooms", rooms, criteria.min_rooms, None),
    )
    for label, actual, minimum, maximum in bounds:
        if minimum is None and maximum is None:
            continue
        if actual is None:
            unverified.append(label)
        elif (minimum is not None and actual < minimum) or (maximum is not None and actual > maximum):
            return MatchResult(False, ())
    return MatchResult(True, tuple(unverified))


class _LinkTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str, int]] = []
        self._active_href: str | None = None
        self._active_start = 0
        self._active_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            self._active_href = href
            self._active_start = len(" ".join(self.parts))
            self._active_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._active_href:
            self.links.append((self._active_href, " ".join(self._active_text), self._active_start))
            self._active_href = None
            self._active_text = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.parts.append(value)
            if self._active_href:
                self._active_text.append(value)

    def get_text(self) -> str:
        return " ".join(self.parts)


def _message_body(message: email.message.Message) -> tuple[str, list[tuple[str, str, int]]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if message.is_multipart():
        parts: Iterable[email.message.Message] = message.walk()
    else:
        parts = (message,)
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_content()
        except (LookupError, UnicodeDecodeError):
            raw = part.get_payload(decode=True) or b""
            payload = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        if not isinstance(payload, str):
            continue
        if content_type == "text/plain":
            plain_parts.append(payload)
        else:
            html_parts.append(payload)
    if plain_parts:
        return "\n".join(plain_parts), []
    parser = _LinkTextParser()
    for fragment in html_parts:
        parser.feed(fragment)
    return parser.get_text(), parser.links


def parse_alert(raw_message: bytes, allowed_senders: tuple[str, ...] = ()) -> list[Listing]:
    message = email.message_from_bytes(raw_message, policy=email.policy.default)
    sender = parseaddr(str(message.get("From", "")))[1].casefold()
    sender_domain = sender.rpartition("@")[2]
    if allowed_senders and not any(
        (allowed.casefold() == sender if "@" in allowed else
         sender_domain == allowed.casefold().lstrip("@")
         or sender_domain.endswith(f".{allowed.casefold().lstrip('@')}"))
        for allowed in allowed_senders
    ):
        return []
    body, html_links = _message_body(message)
    candidates: dict[str, tuple[str, str]] = {}
    links = [(url, title, position) for url, title, position in html_links]
    links.extend((match.group(0), "", match.start()) for match in URL_PATTERN.finditer(body))
    for raw_url, title, position in links:
        normalized = normalize_listing_url(raw_url)
        if normalized is None:
            continue
        url, source = normalized
        snippet = body[max(0, position - 220) : position + 500].strip()
        clean_title = " ".join(html.unescape(title).split())
        if not clean_title:
            first_line = next((line.strip() for line in snippet.splitlines() if line.strip()), "")
            clean_title = first_line[:160] or source
        candidates.setdefault(url, (source, clean_title, snippet))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return [
        Listing(url, source, title, snippet, now)
        for url, (source, title, snippet) in candidates.items()
    ]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid .env entry at line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            raise ValueError(f"Invalid .env key at line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.startswith("replace-"):
        raise ValueError(f"Set {name} in .env before running this command")
    return value


def smtp_settings() -> tuple[str, int, bool, bool, str, str, str]:
    host = env_value("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    if not 1 <= port <= 65535:
        raise ValueError("SMTP_PORT must be between 1 and 65535")
    use_ssl = os.environ.get("SMTP_USE_SSL", "false").casefold() == "true"
    starttls = os.environ.get("SMTP_STARTTLS", "true").casefold() == "true"
    username = env_value("SMTP_USER")
    password = env_value("SMTP_PASSWORD")
    sender = env_value("SMTP_FROM")
    if use_ssl and starttls:
        raise ValueError("Choose either SMTP_USE_SSL=true or SMTP_STARTTLS=true, not both")
    return host, port, use_ssl, starttls, username, password, sender


def _connect_imap() -> imaplib.IMAP4_SSL:
    host = env_value("IMAP_HOST")
    port = int(os.environ.get("IMAP_PORT", "993"))
    if not 1 <= port <= 65535:
        raise ValueError("IMAP_PORT must be between 1 and 65535")
    username = env_value("IMAP_USER")
    password = env_value("IMAP_PASSWORD")
    client = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)
    try:
        client.login(username, password)
    except (imaplib.IMAP4.error, OSError):
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            LOGGER.debug("IMAP logout failed after login error", exc_info=True)
        raise
    return client


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS mailbox_state (
            folder TEXT PRIMARY KEY,
            uid_validity TEXT NOT NULL,
            last_uid INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS listings (
            url TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            snippet TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            unverified TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'filtered'))
        );
        """
    )


def _mailbox_cursor(client: imaplib.IMAP4_SSL, folder: str) -> tuple[str, int]:
    status, _ = client.select(folder, readonly=True)
    if status != "OK":
        raise RuntimeError(f"Could not select IMAP folder {folder!r}")
    status, values = client.response("UIDVALIDITY")
    if status != "UIDVALIDITY" or not values or not values[0]:
        raise RuntimeError("The IMAP server did not provide UIDVALIDITY")
    return values[0].decode("ascii"), int(values[0])


def _fetch_uid(client: imaplib.IMAP4_SSL, uid: int) -> bytes:
    status, data = client.uid("fetch", str(uid), "(RFC822)")
    if status != "OK":
        raise RuntimeError(f"Could not fetch IMAP message UID {uid}")
    for item in data:
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            return item[1]
    raise RuntimeError(f"IMAP returned no message body for UID {uid}")


def _search_uids(client: imaplib.IMAP4_SSL, criterion: str) -> list[int]:
    status, data = client.uid("search", None, criterion)
    if status != "OK":
        raise RuntimeError(f"IMAP search failed for criterion {criterion!r}")
    raw = data[0] if data and data[0] else b""
    try:
        return [int(value) for value in raw.split()]
    except ValueError as error:
        raise RuntimeError("IMAP returned an invalid UID") from error


def _smtp_send(subject: str, body: str, recipient: str) -> None:
    host, port, use_ssl, starttls, username, password, sender = smtp_settings()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)
    context = ssl.create_default_context()
    if use_ssl:
        client = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
    else:
        client = smtplib.SMTP(host, port, timeout=30)
    with client:
        if starttls:
            client.starttls(context=context)
        client.login(username, password)
        client.send_message(message)


def _database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    _schema(connection)
    return connection


def _format_notification(rows: list[sqlite3.Row]) -> str:
    sections = []
    for row in rows:
        status = "Check details: " + ", ".join(row["unverified"].split(",")) if row["unverified"] else "Criteria verified"
        sections.append(
            f"{row['title']}\nSource: {row['source']} | {status}\n{row['url']}\n\n"
            f"{row['snippet'][:900]}"
        )
    return "New possible rental listing(s) from your official site alerts:\n\n" + "\n\n---\n\n".join(sections)


def poll_once(config_path: Path, database_path: Path, dotenv_path: Path) -> int:
    load_dotenv(dotenv_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("config.json must contain a JSON object")
    mailbox = config.get("mailbox", {})
    if not isinstance(mailbox, dict):
        raise ValueError("mailbox must be an object in config.json")
    folder = str(mailbox.get("folder", "INBOX"))
    if not folder.strip():
        raise ValueError("mailbox.folder cannot be empty")
    raw_senders = mailbox.get("allowed_senders", [])
    if not isinstance(raw_senders, list) or not all(isinstance(item, str) for item in raw_senders):
        raise ValueError("mailbox.allowed_senders must be a list of strings")
    criteria_value = config.get("criteria", {})
    if not isinstance(criteria_value, dict):
        raise ValueError("criteria must be an object in config.json")
    criteria = Criteria.from_dict(criteria_value)
    recipient = config.get("notification_email")
    if not isinstance(recipient, str) or not recipient.strip():
        raise ValueError("Set notification_email in config.json")
    recipient = recipient.strip()
    if "@" not in recipient or any(char.isspace() for char in recipient):
        raise ValueError("config.json notification_email must be an email address")
    if (
        criteria.min_rent is not None
        and criteria.max_rent is not None
        and criteria.min_rent > criteria.max_rent
    ):
        raise ValueError("criteria.min_rent cannot exceed criteria.max_rent")
    if (
        criteria.min_surface is not None
        and criteria.max_surface is not None
        and criteria.min_surface > criteria.max_surface
    ):
        raise ValueError("criteria.min_surface cannot exceed criteria.max_surface")
    interval = config.get("poll_interval_seconds", 300)
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 30:
        raise ValueError("poll_interval_seconds must be an integer of at least 30")

    connection = _database(database_path)
    client: imaplib.IMAP4_SSL | None = None
    try:
        client = _connect_imap()
        uid_validity, _ = _mailbox_cursor(client, folder)
        state = connection.execute(
            "SELECT uid_validity, last_uid FROM mailbox_state WHERE folder = ?", (folder,)
        ).fetchone()
        if state is None:
            latest = max(_search_uids(client, "ALL"), default=0)
            connection.execute(
                "INSERT INTO mailbox_state(folder, uid_validity, last_uid) VALUES (?, ?, ?)",
                (folder, uid_validity, latest),
            )
            connection.commit()
            LOGGER.info("Initialized mailbox cursor at UID %s; existing mail will not trigger alerts", latest)
            return 0
        if state["uid_validity"] != uid_validity:
            latest = max(_search_uids(client, "ALL"), default=0)
            connection.execute(
                "UPDATE mailbox_state SET uid_validity = ?, last_uid = ? WHERE folder = ?",
                (uid_validity, latest, folder),
            )
            connection.commit()
            LOGGER.warning("IMAP UIDVALIDITY changed; reset cursor at UID %s without replaying old mail", latest)
            return 0

        last_uid = int(state["last_uid"])
        new_uids = [uid for uid in _search_uids(client, f"UID {last_uid + 1}:*") if uid > last_uid]
        for uid in new_uids:
            raw_message = _fetch_uid(client, uid)
            listings = parse_alert(raw_message, tuple(raw_senders))
            with connection:
                for listing in listings:
                    result = check_criteria(criteria, listing.title, listing.snippet)
                    status = "pending" if result.matches else "filtered"
                    connection.execute(
                        """INSERT OR IGNORE INTO listings
                        (url, source, title, snippet, discovered_at, unverified, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            listing.url,
                            listing.source,
                            listing.title,
                            listing.snippet,
                            listing.discovered_at,
                            ",".join(result.unverified),
                            status,
                        ),
                    )
                connection.execute(
                    "UPDATE mailbox_state SET last_uid = ? WHERE folder = ?",
                    (uid, folder),
                )

        pending = connection.execute(
            """SELECT url, source, title, snippet, discovered_at, unverified
            FROM listings WHERE status = 'pending' ORDER BY discovered_at, url"""
        ).fetchall()
        if pending:
            _smtp_send(
                f"{len(pending)} new rental listing(s)",
                _format_notification(pending),
                recipient,
            )
            with connection:
                connection.executemany(
                    "UPDATE listings SET status = 'sent' WHERE url = ?",
                    [(row["url"],) for row in pending],
                )
        LOGGER.info("Processed %s new email(s); sent %s listing(s)", len(new_uids), len(pending))
        return len(pending)
    finally:
        connection.close()
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                LOGGER.debug("IMAP logout failed", exc_info=True)


def run_forever(config_path: Path, database_path: Path, dotenv_path: Path) -> None:
    while True:
        try:
            poll_once(config_path, database_path, dotenv_path)
        except (OSError, imaplib.IMAP4.error, smtplib.SMTPException, ValueError, RuntimeError) as error:
            LOGGER.error("Monitor check failed: %s", error)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("config.json must contain a JSON object")
        interval = config.get("poll_interval_seconds", 300)
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 30:
            raise ValueError("poll_interval_seconds must be an integer of at least 30")
        time.sleep(interval)


def test_email(config_path: Path, dotenv_path: Path) -> None:
    load_dotenv(dotenv_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("config.json must contain a JSON object")
    recipient = config.get("notification_email")
    if not isinstance(recipient, str) or not recipient.strip():
        raise ValueError("Set notification_email in config.json")
    _smtp_send("Rental monitor test", "SMTP email delivery is configured.", recipient.strip())
