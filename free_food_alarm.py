#!/usr/bin/env python3
"""Poll the MIT free-foods Mailman archive and alarm on nearby posts.

Credentials are read from environment variables:
  FREE_FOODS_EMAIL
  FREE_FOODS_PASSWORD

The first run marks existing archive messages as seen unless --process-existing
is passed. That prevents an immediate alarm on old posts.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

try:
    import requests
except ImportError as exc:  # pragma: no cover - startup dependency guard
    raise SystemExit(
        "Missing dependency: requests. Install with: python3 -m pip install requests"
    ) from exc


ARCHIVE_ROOT = "https://mailman.mit.edu/mailman/private/free-foods/"
LISTINFO_URL = "https://mailman.mit.edu/mailman/listinfo/free-foods"
DEFAULT_STATE_PATH = Path.home() / ".free_food_alarm_seen.json"
DEFAULT_ENV_PATH = Path(".env")
DEFAULT_GPIO_PIN = 16
DEFAULT_ALARM_SECONDS = 10.0
DEFAULT_WATCH_BUILDINGS = {
    "8",
    "12",
    "16",
    "18",
    "24",
    "26",
    "32",
    "34",
    "36",
    "46",
    "48",
    "54",
    "55",
    "56",
    "57",
    "62",
    "64",
    "66",
    "68",
    "76",
    "E14",
    "E15",
    "E17",
    "E18",
    "E19",
    "E23",
    "E25",
    "E28",
    "NE45",
}

BUILDING_ALIASES = {
    "stata": "32",
    "green building": "54",
    "student center": "W20",
    "stud": "W20",
    "media lab": "E14",
    "lobby 7": "7",
    "infinite corridor": "7",
    "banana lounge": "26",
    "barker": "10",
    "maseeh": "W1",
}

EXPLICIT_BUILDING_RE = re.compile(
    r"\b(?:building|bldg\.?)\s*((?:NE|NW|N|E|W)?\d{1,2})\b",
    re.IGNORECASE,
)
ROOM_CODE_RE = re.compile(
    r"\b((?:NE|NW|N|E|W)?\d{1,2})-[A-Z]?\d{1,4}\b",
    re.IGNORECASE,
)
REVERSED_EW_ROOM_RE = re.compile(r"\b(\d{1,2})([EW])-\d{1,4}\b", re.IGNORECASE)
LETTER_BUILDING_RE = re.compile(r"\b(?:NE|NW|N|E|W)\d{1,2}\b", re.IGNORECASE)
MESSAGE_PATH_RE = re.compile(r"/\d{6}\.html$")


@dataclass(frozen=True)
class ArchiveMessage:
    url: str
    subject: str
    text: str


@dataclass(frozen=True)
class MessageLink:
    url: str
    subject_hint: str


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = dict(attrs)
        self._href = attrs_dict.get("href")
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text_parts).strip()))
            self._href = None
            self._text_parts = []


class TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.pre_parts: list[str] = []
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self._in_pre = False
        self._in_title = False
        self._in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        if tag_name == "pre":
            self._in_pre = True
        elif tag_name == "title":
            self._in_title = True
        elif tag_name == "h1":
            self._in_h1 = True

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
        if self._in_pre:
            self.pre_parts.append(data)
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        if tag_name == "pre":
            self._in_pre = False
        elif tag_name == "title":
            self._in_title = False
        elif tag_name == "h1":
            self._in_h1 = False

    def text(self) -> str:
        return " ".join(part.strip() for part in self.parts if part.strip())

    def title(self) -> str:
        return clean_subject(" ".join(part.strip() for part in self.title_parts if part.strip()))

    def h1(self) -> str:
        return clean_subject(" ".join(part.strip() for part in self.h1_parts if part.strip()))

    def message_body(self) -> str:
        return strip_mail_headers("\n".join(self.pre_parts))


def month_slug(now: dt.datetime | None = None) -> str:
    current = now or dt.datetime.now()
    return f"{current.year}-{calendar.month_name[current.month]}"


def month_date_url(now: dt.datetime | None = None) -> str:
    return urljoin(ARCHIVE_ROOT, f"{month_slug(now)}/date.html")


def normalize_building(raw: str) -> str:
    return raw.upper()


def extract_buildings(text: str) -> set[str]:
    lowered = text.lower()
    buildings = set()
    buildings.update(normalize_building(match.group(1)) for match in EXPLICIT_BUILDING_RE.finditer(text))
    buildings.update(normalize_building(match.group(1)) for match in ROOM_CODE_RE.finditer(text))
    buildings.update(
        normalize_building(f"{match.group(2)}{match.group(1)}")
        for match in REVERSED_EW_ROOM_RE.finditer(text)
    )
    buildings.update(normalize_building(match.group(0)) for match in LETTER_BUILDING_RE.finditer(text))
    for alias, building in BUILDING_ALIASES.items():
        if alias in lowered:
            buildings.add(building)
    return buildings


def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"State file must contain a JSON list: {path}")
    return {str(item) for item in data}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value


def save_seen(path: Path, seen: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(sorted(set(seen)), fh, indent=2)
        fh.write("\n")
    tmp_path.replace(path)


def login(session: requests.Session, email: str, password: str) -> None:
    response = session.get(ARCHIVE_ROOT, timeout=20)
    response.raise_for_status()

    data_variants = [
        {"username": email, "password": password, "submit": "Let me in..."},
        {"email": email, "password": password, "submit": "Let me in..."},
    ]

    last_text = ""
    for data in data_variants:
        login_response = session.post(ARCHIVE_ROOT, data=data, timeout=20)
        login_response.raise_for_status()
        last_text = login_response.text
        if login_succeeded(login_response):
            return

    snippet = " ".join(last_text.split())[:240]
    raise RuntimeError(f"Mailman login did not appear to succeed. Response: {snippet}")


def login_succeeded(response: requests.Response) -> bool:
    final_url = response.url.rstrip("/") + "/"
    text = response.text.lower()
    return (
        final_url.startswith(ARCHIVE_ROOT)
        and "authentication failed" not in text
        and "private archive authentication" not in text
        and "the free-foods archives" in text
    )


def fetch_message_links(session: requests.Session, url: str) -> list[MessageLink]:
    response = session.get(url, timeout=20)
    response.raise_for_status()
    parser = LinkParser()
    parser.feed(response.text)

    links: list[MessageLink] = []
    for href, label in parser.links:
        absolute = urljoin(url, href)
        parsed = urlparse(absolute)
        if MESSAGE_PATH_RE.search(parsed.path):
            links.append(MessageLink(url=absolute, subject_hint=clean_subject(label)))
    return dedupe_links(links)


def fetch_message(session: requests.Session, link: MessageLink) -> ArchiveMessage:
    url = link.url
    response = session.get(url, timeout=20)
    response.raise_for_status()
    parser = TextParser()
    parser.feed(response.text)
    text = parser.message_body()
    subject = parser.h1() or parser.title() or link.subject_hint or extract_subject(text)
    return ArchiveMessage(url=url, subject=subject, text=text)


def extract_subject(text: str) -> str:
    match = re.search(r"\bSubject:\s*(.+?)(?:\s+From:|\s+Date:|$)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return "(no subject found)"


def clean_subject(subject: str) -> str:
    subject = re.sub(r"\s+", " ", subject).strip()
    subject = re.sub(r"^\[?Free-foods?\]?\s*", "", subject, flags=re.IGNORECASE)
    return subject


def strip_mail_headers(message_text: str) -> str:
    if "\n\n" not in message_text:
        return message_text
    headers, body = message_text.split("\n\n", 1)
    if re.search(r"(?im)^(from|date|subject):\s+", headers):
        return body
    return message_text


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            output.append(item)
    return output


def dedupe_links(items: Iterable[MessageLink]) -> list[MessageLink]:
    seen: set[str] = set()
    output: list[MessageLink] = []
    for item in items:
        if item.url not in seen:
            seen.add(item.url)
            output.append(item)
    return output


def alarm(message: ArchiveMessage, matched_buildings: set[str], args: argparse.Namespace) -> None:
    buildings = ",".join(sorted(matched_buildings))
    print(f"ALARM buildings={buildings} subject={message.subject} url={message.url}", flush=True)
    trigger_gpio(args.gpio_pin, args.alarm_seconds, args.dry_run_alarm)


def trigger_gpio(pin: int, seconds: float, dry_run: bool) -> None:
    if dry_run:
        print(f"GPIO dry-run pin={pin} seconds={seconds:g}", flush=True)
        return

    try:
        from gpiozero import DigitalOutputDevice
    except ImportError:
        print(
            f"GPIO unavailable: install gpiozero on the Pi. Would turn on GPIO {pin} for {seconds:g}s.",
            file=sys.stderr,
            flush=True,
        )
        return

    device = DigitalOutputDevice(pin, active_high=True, initial_value=False)
    try:
        device.on()
        time.sleep(seconds)
    finally:
        device.off()
        device.close()


def poll_once(
    session: requests.Session,
    archive_url: str,
    watch_buildings: set[str],
    seen: set[str],
    process_existing: bool,
    args: argparse.Namespace,
) -> tuple[set[str], int]:
    links = fetch_message_links(session, archive_url)
    new_links = [link for link in links if link.url not in seen]

    if not seen and not process_existing:
        seen.update(link.url for link in links)
        return seen, 0

    alarms = 0
    for link in new_links:
        message = fetch_message(session, link)
        found = extract_buildings(f"{message.subject}\n{message.text}")
        matched = found & watch_buildings
        if matched:
            alarm(message, matched, args)
            alarms += 1
        else:
            print(f"seen no-match subject={message.subject} url={message.url}", flush=True)
        seen.add(link.url)

    return seen, alarms


def parse_buildings(raw: str) -> set[str]:
    buildings = {item.strip().upper() for item in raw.split(",") if item.strip()}
    if not buildings:
        raise argparse.ArgumentTypeError("provide at least one building, e.g. 32,36,W20")
    return buildings


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--buildings",
        type=parse_buildings,
        default=DEFAULT_WATCH_BUILDINGS,
        help=(
            "Comma-separated MIT buildings to alarm on. "
            f"Default: {','.join(sorted(DEFAULT_WATCH_BUILDINGS))}"
        ),
    )
    parser.add_argument("--interval", type=int, default=30, help="Polling interval in seconds")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH, help="Path to .env credentials file")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="Seen-state JSON path")
    parser.add_argument("--archive-url", default=None, help="Override archive date.html URL")
    parser.add_argument("--gpio-pin", type=int, default=DEFAULT_GPIO_PIN, help="BCM GPIO pin for MOSFET gate")
    parser.add_argument(
        "--alarm-seconds",
        type=float,
        default=DEFAULT_ALARM_SECONDS,
        help="Seconds to keep the GPIO pin on for each alarm",
    )
    parser.add_argument(
        "--dry-run-alarm",
        action="store_true",
        help="Print GPIO action instead of touching hardware",
    )
    parser.add_argument(
        "--process-existing",
        action="store_true",
        help="Process already-present archive messages on first run",
    )
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    load_dotenv(args.env_file)
    email = os.environ.get("FREE_FOODS_EMAIL")
    password = os.environ.get("FREE_FOODS_PASSWORD")
    if not email or not password:
        print("Set FREE_FOODS_EMAIL and FREE_FOODS_PASSWORD in the environment.", file=sys.stderr)
        return 2

    seen = load_seen(args.state)
    session = requests.Session()
    session.headers.update({"User-Agent": "free-food-alarm/0.1"})

    login(session, email, password)
    if args.archive_url:
        print(f"polling {args.archive_url} for buildings={','.join(sorted(args.buildings))}", flush=True)
    else:
        print(f"polling current monthly archive for buildings={','.join(sorted(args.buildings))}", flush=True)

    while True:
        try:
            archive_url = args.archive_url or month_date_url()
            seen, alarms = poll_once(
                session=session,
                archive_url=archive_url,
                watch_buildings=args.buildings,
                seen=seen,
                process_existing=args.process_existing,
                args=args,
            )
            save_seen(args.state, seen)
            print(f"poll complete alarms={alarms} seen={len(seen)}", flush=True)
        except Exception as exc:
            print(f"poll error: {exc}", file=sys.stderr, flush=True)

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
