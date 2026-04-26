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
import difflib
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
DEFAULT_ALARM_SECONDS = 20.0
DEFAULT_PULSE_ON_SECONDS = 0.5
DEFAULT_PULSE_OFF_SECONDS = 0.5
DEFAULT_DEDUPE_MINUTES = 60.0
MAX_ALARM_KEYS = 500
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
    "300 technology square": "NE45",
    "alumni pool": "57",
    "bemis": "64",
    "brain and cognitive sciences": "46",
    "brain cognitive sciences": "46",
    "brain & cognitive sciences": "46",
    "brain and cognitive sciences complex": "46",
    "bcs building": "46",
    "mcgovern institute": "46",
    "mcgovern institute for brain research": "46",
    "brown building": "39",
    "vannevar bush building": "13",
    "building 24": "24",
    "compton laboratories": "26",
    "eg&g education center": "34",
    "eg and g education center": "34",
    "eg g education center": "34",
    "dorrance building": "16",
    "dorrance bldg": "16",
    "dreyfus building": "18",
    "dreyfus bldg": "18",
    "du pont athletic gymnasium": "W31",
    "du pont gym": "W31",
    "eastman laboratories": "8",
    "fairchild building": "36",
    "fairchild bldg": "36",
    "ford building": "E19",
    "ford bldg": "E19",
    "stata": "32",
    "stata center": "32",
    "green building": "54",
    "green bldg": "54",
    "guggenheim laboratory": "33",
    "hayden": "62",
    "hayden memorial library": "62",
    "hayden library": "62",
    "health services": "E23",
    "mit health": "E23",
    "hermann building": "E53",
    "koch institute": "76",
    "koch institute for integrative cancer research": "76",
    "koch biology building": "68",
    "koch biology bldg": "68",
    "kresge auditorium": "W16",
    "landau building": "66",
    "landau bldg": "66",
    "lisa t su building": "12",
    "lisa su building": "12",
    "samuel tak lee building": "9",
    "lincoln laboratory": "LL",
    "arthur d. little building": "E60",
    "arthur d little building": "E60",
    "maclaurin buildings": "10",
    "student center": "W20",
    "stratton student center": "W20",
    "stud": "W20",
    "mit.nano": "12",
    "mit nano": "12",
    "media lab": "E14",
    "media lab complex": "E14",
    "merkin building": "E38",
    "moghadam building": "55",
    "moghadam bldg": "55",
    "muckley building": "E40",
    "nuclear reactor laboratory": "NW12",
    "nuclear science and engineering": "24",
    "pierce laboratory": "1",
    "parsons laboratory": "48",
    "pratt school": "5",
    "rogers building": "7",
    "simons building": "2",
    "sloan laboratories": "31",
    "sloan laboratory": "35",
    "mit sloan": "E62",
    "walker memorial": "50",
    "walter c. wood sailing pavilion": "51",
    "walter c wood sailing pavilion": "51",
    "whitaker building": "56",
    "whitaker bldg": "56",
    "whitaker college": "E25",
    "wiesner building": "E15",
    "wiesner bldg": "E15",
    "whitehead institute": "NE45",
    "mudd building": "E17",
    "mudd bldg": "E17",
    "mit museum": "E28",
    "wright brothers wind tunnel": "17",
    "zesiger sports and fitness center": "W35",
    "z center": "W35",
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
WORD_RE = re.compile(r"[a-z0-9]+")
SUBJECT_PREFIX_RE = re.compile(
    r"^(?:\s*(?:re|fw|fwd)\s*:\s*|\s*\[[^\]]+\]\s*)+",
    re.IGNORECASE,
)
NEGATIVE_UPDATE_RE = re.compile(
    r"\b(?:all\s+gone|no\s+more|none\s+left|food\s+is\s+gone|gone|taken|empty|finished|over)\b",
    re.IGNORECASE,
)
GENERIC_ALIAS_WORDS = {
    "and",
    "at",
    "building",
    "buildings",
    "center",
    "college",
    "gym",
    "institute",
    "laboratories",
    "laboratory",
    "library",
    "memorial",
    "mit",
}


@dataclass(frozen=True)
class ArchiveMessage:
    url: str
    subject: str
    text: str


@dataclass(frozen=True)
class MessageLink:
    url: str
    subject_hint: str


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str


@dataclass
class PollState:
    seen_urls: set[str]
    alarm_keys: dict[str, float]


class AuthenticationExpired(RuntimeError):
    pass


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
    normalized_words = WORD_RE.findall(lowered)
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
        elif building in DEFAULT_WATCH_BUILDINGS and fuzzy_phrase_in_words(alias, normalized_words):
            buildings.add(building)
    return buildings


def fuzzy_phrase_in_words(phrase: str, words: list[str]) -> bool:
    phrase_words = WORD_RE.findall(phrase.lower())
    if not phrase_words:
        return False

    phrase_text = " ".join(phrase_words)
    if len(phrase_text) < 5:
        return False

    threshold = 0.84 if len(phrase_text) <= 14 else 0.88
    phrase_len = len(phrase_words)
    for size in {phrase_len - 1, phrase_len, phrase_len + 1}:
        if size <= 0:
            continue
        for start in range(0, len(words) - size + 1):
            candidate_words = words[start : start + size]
            if not has_distinctive_word_match(phrase_words, candidate_words):
                continue
            candidate = " ".join(candidate_words)
            if difflib.SequenceMatcher(None, phrase_text, candidate).ratio() >= threshold:
                return True
    return False


def has_distinctive_word_match(phrase_words: list[str], candidate_words: list[str]) -> bool:
    distinctive_words = [
        word for word in phrase_words if len(word) >= 4 and word not in GENERIC_ALIAS_WORDS
    ]
    if not distinctive_words:
        return False

    for phrase_word in distinctive_words:
        for candidate_word in candidate_words:
            if difflib.SequenceMatcher(None, phrase_word, candidate_word).ratio() >= 0.8:
                return True
    return False


def load_state(path: Path) -> PollState:
    if not path.exists():
        return PollState(seen_urls=set(), alarm_keys={})
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return PollState(seen_urls={str(item) for item in data}, alarm_keys={})
    if not isinstance(data, dict):
        raise ValueError(f"State file must contain a JSON list or object: {path}")

    seen_urls = data.get("seen_urls", [])
    alarm_keys = data.get("alarm_keys", {})
    if not isinstance(seen_urls, list) or not isinstance(alarm_keys, dict):
        raise ValueError(f"State file has invalid shape: {path}")
    return PollState(
        seen_urls={str(item) for item in seen_urls},
        alarm_keys={str(key): float(value) for key, value in alarm_keys.items()},
    )


def load_seen(path: Path) -> set[str]:
    return load_state(path).seen_urls


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


def save_state(path: Path, state: PollState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    data = {
        "seen_urls": sorted(state.seen_urls),
        "alarm_keys": dict(sorted(state.alarm_keys.items())),
    }
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    tmp_path.replace(path)


def save_seen(path: Path, seen: Iterable[str]) -> None:
    save_state(path, PollState(seen_urls=set(seen), alarm_keys={}))


def login(session: requests.Session, email: str, password: str) -> None:
    session.cookies.clear()
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


def ensure_authenticated(response: requests.Response) -> None:
    text = response.text.lower()
    if response.status_code in {401, 403}:
        raise AuthenticationExpired(f"Mailman authentication expired with HTTP {response.status_code}")
    if "private archive authentication" in text or "authentication failed" in text:
        raise AuthenticationExpired("Mailman returned the private archive login page")


def fetch_message_links(session: requests.Session, url: str) -> list[MessageLink]:
    response = session.get(url, timeout=20)
    response.raise_for_status()
    ensure_authenticated(response)
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
    ensure_authenticated(response)
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


def canonical_subject(subject: str) -> str:
    value = clean_subject(subject)
    while True:
        cleaned = SUBJECT_PREFIX_RE.sub("", value).strip()
        if cleaned == value:
            break
        value = cleaned
    return value


def is_negative_update(message: ArchiveMessage) -> bool:
    return bool(NEGATIVE_UPDATE_RE.search(f"{message.subject}\n{message.text}"))


def alarm_key(message: ArchiveMessage, matched_buildings: set[str]) -> str:
    words = WORD_RE.findall(canonical_subject(message.subject).lower())
    useful_words = [
        word
        for word in words
        if word
        not in {
            "free",
            "food",
            "foods",
            "found",
            "leftover",
            "leftovers",
            "outside",
            "room",
            "building",
            "bldg",
            "lobby",
            "floor",
            "the",
            "and",
            "with",
            "in",
            "at",
            "of",
        }
    ]
    subject_part = " ".join(useful_words[:8]) or canonical_subject(message.subject).lower()
    buildings_part = ",".join(sorted(matched_buildings))
    return f"{buildings_part}|{subject_part}"


def recently_alarmed(state: PollState, key: str, now: float, dedupe_seconds: float) -> bool:
    last_alarm = state.alarm_keys.get(key)
    return last_alarm is not None and now - last_alarm < dedupe_seconds


def record_alarm_key(state: PollState, key: str, now: float, dedupe_seconds: float) -> None:
    cutoff = now - max(dedupe_seconds, 60.0)
    state.alarm_keys = {
        stored_key: ts
        for stored_key, ts in state.alarm_keys.items()
        if ts >= cutoff
    }
    state.alarm_keys[key] = now
    if len(state.alarm_keys) > MAX_ALARM_KEYS:
        newest = sorted(state.alarm_keys.items(), key=lambda item: item[1], reverse=True)
        state.alarm_keys = dict(newest[:MAX_ALARM_KEYS])


def strip_mail_headers(message_text: str) -> str:
    if "\n\n" not in message_text:
        return strip_quoted_text(message_text)
    headers, body = message_text.split("\n\n", 1)
    if re.search(r"(?im)^(from|date|subject):\s+", headers):
        return strip_quoted_text(body)
    return strip_quoted_text(message_text)


def strip_quoted_text(message_text: str) -> str:
    kept_lines: list[str] = []
    for line in message_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"(?i)^on .+ wrote:$", stripped):
            break
        if re.match(r"(?i)^from:\s", stripped):
            break
        if "free-foods mailing list" in stripped.lower():
            break
        kept_lines.append(line)
    return "\n".join(kept_lines)


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
        print(
            f"GPIO dry-run pulse pin={pin} seconds={seconds:g} "
            f"on={DEFAULT_PULSE_ON_SECONDS:g} off={DEFAULT_PULSE_OFF_SECONDS:g}",
            flush=True,
        )
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
        pulse_until = time.monotonic() + seconds
        while time.monotonic() < pulse_until:
            device.on()
            time.sleep(min(DEFAULT_PULSE_ON_SECONDS, max(0.0, pulse_until - time.monotonic())))
            device.off()
            remaining = pulse_until - time.monotonic()
            if remaining > 0:
                time.sleep(min(DEFAULT_PULSE_OFF_SECONDS, remaining))
    finally:
        device.off()
        device.close()


def poll_once(
    session: requests.Session,
    archive_url: str,
    watch_buildings: set[str],
    state: PollState,
    process_existing: bool,
    args: argparse.Namespace,
) -> tuple[PollState, int]:
    links = fetch_message_links(session, archive_url)
    new_links = [link for link in links if link.url not in state.seen_urls]

    if not state.seen_urls and not process_existing:
        state.seen_urls.update(link.url for link in links)
        return state, 0

    alarms = 0
    dedupe_seconds = args.dedupe_minutes * 60.0
    for link in new_links:
        message = fetch_message(session, link)
        found = extract_buildings(f"{message.subject}\n{message.text}")
        matched = found & watch_buildings
        if matched:
            if args.suppress_gone_updates and is_negative_update(message):
                print(f"suppressed gone-update subject={message.subject} url={message.url}", flush=True)
            else:
                key = alarm_key(message, matched)
                now = time.time()
                if dedupe_seconds > 0 and recently_alarmed(state, key, now, dedupe_seconds):
                    print(f"suppressed duplicate key={key} subject={message.subject} url={message.url}", flush=True)
                else:
                    alarm(message, matched, args)
                    record_alarm_key(state, key, now, dedupe_seconds)
                    alarms += 1
        else:
            print(f"seen no-match subject={message.subject} url={message.url}", flush=True)
        state.seen_urls.add(link.url)

    return state, alarms


def poll_once_with_relogin(
    session: requests.Session,
    credentials: Credentials,
    archive_url: str,
    watch_buildings: set[str],
    state: PollState,
    process_existing: bool,
    args: argparse.Namespace,
) -> tuple[PollState, int]:
    try:
        return poll_once(
            session=session,
            archive_url=archive_url,
            watch_buildings=watch_buildings,
            state=state,
            process_existing=process_existing,
            args=args,
        )
    except AuthenticationExpired as exc:
        print(f"authentication expired, re-logging in: {exc}", file=sys.stderr, flush=True)
        login(session, credentials.email, credentials.password)
        return poll_once(
            session=session,
            archive_url=archive_url,
            watch_buildings=watch_buildings,
            state=state,
            process_existing=process_existing,
            args=args,
        )


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
        help="Seconds to pulse the GPIO pin for each alarm",
    )
    parser.add_argument(
        "--dry-run-alarm",
        action="store_true",
        help="Print GPIO action instead of touching hardware",
    )
    parser.add_argument(
        "--dedupe-minutes",
        type=float,
        default=DEFAULT_DEDUPE_MINUTES,
        help="Suppress duplicate alarms for the same building/subject event within this many minutes",
    )
    parser.add_argument(
        "--suppress-gone-updates",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Suppress posts that say the food is gone/no more/taken",
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
    credentials = Credentials(email=email, password=password)

    state = load_state(args.state)
    session = requests.Session()
    session.headers.update({"User-Agent": "free-food-alarm/0.1"})

    login(session, credentials.email, credentials.password)
    if args.archive_url:
        print(f"polling {args.archive_url} for buildings={','.join(sorted(args.buildings))}", flush=True)
    else:
        print(f"polling current monthly archive for buildings={','.join(sorted(args.buildings))}", flush=True)

    while True:
        try:
            archive_url = args.archive_url or month_date_url()
            state, alarms = poll_once_with_relogin(
                session=session,
                credentials=credentials,
                archive_url=archive_url,
                watch_buildings=args.buildings,
                state=state,
                process_existing=args.process_existing,
                args=args,
            )
            save_state(args.state, state)
            print(
                f"poll complete alarms={alarms} seen={len(state.seen_urls)} alarm_keys={len(state.alarm_keys)}",
                flush=True,
            )
        except Exception as exc:
            print(f"poll error: {exc}", file=sys.stderr, flush=True)

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
