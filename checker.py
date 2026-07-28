#!/usr/bin/env python3

import argparse
import hashlib
import json
import math
import os
import re
import threading
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Queue
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pyotp
from dotenv import dotenv_values
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
ENV_VALUES = dotenv_values(ROOT / ".env")
BOT_STATE_PATH = ROOT / ".bot_state.json"

PORTAL_URL = "https://polimi-sol.dirittoallostudio.it/apps/V3.1/sol/public/index.php"
DEFAULT_ACADEMIC_YEAR = "2026/2027"
TEHRAN_TIMEZONE = ZoneInfo("Asia/Tehran")
TEHRAN_CHECK_HOURS = (9, 21)
TELEGRAM_MAX_ATTEMPTS = 3
TELEGRAM_RETRY_SECONDS = 2
TELEGRAM_MESSAGE_LIMIT = 3_500
TELEGRAM_POLL_SECONDS = 25
TELEGRAM_POLL_NETWORK_TIMEOUT = 35
TELEGRAM_POLL_RETRY_SECONDS = 5
TELEGRAM_DUPLICATE_POLL_SECONDS = 1
TELEGRAM_ACTION_QUEUE_LIMIT = 50
TELEGRAM_UI_VERSION = 1
BUTTON_CHECK_NOW = "🔎 Check now"
BUTTON_MANAGE_RESIDENCES = "🏘 Manage residences"
BUTTON_HELP = "ℹ️ Help"
NOTIFICATION_DIVIDER = "━━━━━━━━━━━━━━━━━━━━"
PORTAL_LOAD_ATTEMPTS = 3
PORTAL_RETRY_SECONDS = 2
BOOKING_PAGE_TIMEOUT_SECONDS = 60
TABLE_STABLE_SECONDS = 2
LOGOUT_VERIFY_ATTEMPTS = 3
LOGOUT_STATE_STABLE_SECONDS = 3
ROOM_COUNT_PATTERN = re.compile(
    r"^\s*(\d+)\s*(?:Post[oi]|Rooms?)\s*$", re.IGNORECASE
)
BOT_STATE_LOCK = threading.Lock()
CHECK_ACTION_LOCK = threading.Lock()
PENDING_SOL_ACTIONS = set()
PROCESSED_OFFSET_LOCK = threading.Lock()
PROCESSED_TELEGRAM_OFFSET = 0
TELEGRAM_ACTION_QUEUE = Queue(maxsize=TELEGRAM_ACTION_QUEUE_LIMIT)


def setting(name, default=""):
    value = os.environ.get(name)
    if value is None:
        value = ENV_VALUES.get(name, default)
    return default if value is None else str(value)


POLIMI_USERNAME = setting("POLIMI_USERNAME").strip()
POLIMI_PASSWORD = setting("POLIMI_PASSWORD")
POLIMI_TOTP_URI = setting("POLIMI_TOTP_URI").strip()
ACADEMIC_YEAR = setting(
    "ACADEMIC_YEAR",
    DEFAULT_ACADEMIC_YEAR,
).strip()
TELEGRAM_BOT_TOKEN = setting("TELEGRAM_BOT_TOKEN").strip()
TELEGRAM_CHAT_ID = setting("TELEGRAM_CHAT_ID").strip()
CHECK_INTERVAL_HOURS = setting("CHECK_INTERVAL_HOURS", "12").strip()
CHECK_START_HOUR = setting("CHECK_START_HOUR").strip()
INCLUDE_RESIDENCE_NOTICE_PAGE_VALUE = setting(
    "INCLUDE_RESIDENCE_NOTICE_PAGE",
    "true",
).strip()

# Do not pass secrets or verbose browser-debug settings to Playwright/Chromium.
for secret_name in (
    "POLIMI_USERNAME",
    "POLIMI_PASSWORD",
    "POLIMI_TOTP_URI",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DEBUG",
    "PWDEBUG",
    "SSLKEYLOGFILE",
):
    os.environ.pop(secret_name, None)


class CheckerError(Exception):
    pass


def boolean_setting(name, value):
    normalized = value.casefold()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    raise CheckerError(f"{name} must be true or false.")


def academic_year_start(value):
    match = re.fullmatch(r"([0-9]{4})/([0-9]{4})", value)
    if match is None or int(match.group(2)) != int(match.group(1)) + 1:
        raise CheckerError(
            "ACADEMIC_YEAR must contain consecutive years in YYYY/YYYY "
            "format, for example 2026/2027."
        )
    return match.group(1)


def academic_year_panel(value):
    return f"#aa{academic_year_start(value)}"


@contextmanager
def browser_stage(description):
    try:
        yield
    except PlaywrightError as error:
        raise CheckerError(
            f"Browser automation failed while {description}."
        ) from error


def log(message):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"{timestamp} {message}", flush=True)


def default_bot_state():
    return {
        "schema": 1,
        "telegram_offset": 0,
        "telegram_ui_version": 0,
        "known_residences": [],
        "unwatched_residences": [],
    }


def unique_text_list(values):
    unique = []
    seen = set()
    if not isinstance(values, list):
        return unique
    for value in values:
        if not isinstance(value, str):
            continue
        value = clean_text(value)
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def read_bot_state_unlocked():
    try:
        raw_state = json.loads(BOT_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw_state, dict):
            raise ValueError
    except FileNotFoundError:
        return default_bot_state()
    except ValueError:
        log(
            "Bot state was invalid; using the safe default "
            "(all known residences watched)."
        )
        backup_name = (
            ".bot_state.corrupt-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{os.getpid()}.json"
        )
        try:
            os.replace(BOT_STATE_PATH, BOT_STATE_PATH.with_name(backup_name))
        except OSError:
            log("The invalid bot state could not be preserved as a backup.")
        return default_bot_state()
    except OSError:
        log(
            "Bot state could not be read; using the safe default "
            "(all known residences watched)."
        )
        return default_bot_state()

    state = default_bot_state()
    offset = raw_state.get("telegram_offset", 0)
    if isinstance(offset, int) and offset >= 0:
        state["telegram_offset"] = offset
    ui_version = raw_state.get("telegram_ui_version", 0)
    if isinstance(ui_version, int) and ui_version >= 0:
        state["telegram_ui_version"] = ui_version
    state["known_residences"] = unique_text_list(
        raw_state.get("known_residences", [])
    )
    state["unwatched_residences"] = unique_text_list(
        raw_state.get("unwatched_residences", [])
    )
    return state


def write_bot_state_unlocked(state):
    temporary_path = BOT_STATE_PATH.with_name(f"{BOT_STATE_PATH.name}.tmp")
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    temporary_path.write_text(payload, encoding="utf-8")
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, BOT_STATE_PATH)


def residence_preferences():
    with BOT_STATE_LOCK:
        state = read_bot_state_unlocked()
    catalog = state["known_residences"]
    unwatched = set(state["unwatched_residences"])
    watched = [name for name in catalog if name not in unwatched]
    return catalog, watched


def refresh_residence_catalog(residences):
    catalog = unique_text_list(list(residences))
    with BOT_STATE_LOCK:
        state = read_bot_state_unlocked()
        state["known_residences"] = catalog
        write_bot_state_unlocked(state)
    unwatched = set(state["unwatched_residences"])
    return {name for name in catalog if name not in unwatched}


def update_residence_watch(numbers, should_watch):
    with BOT_STATE_LOCK:
        state = read_bot_state_unlocked()
        catalog = state["known_residences"]
        selected = [catalog[number - 1] for number in numbers]
        unwatched = set(state["unwatched_residences"])
        for residence in selected:
            if should_watch:
                unwatched.discard(residence)
            else:
                unwatched.add(residence)
        state["unwatched_residences"] = sorted(unwatched)
        write_bot_state_unlocked(state)

    watched = [name for name in catalog if name not in unwatched]
    return catalog, watched


def update_all_residence_watches(should_watch):
    with BOT_STATE_LOCK:
        state = read_bot_state_unlocked()
        catalog = state["known_residences"]
        state["unwatched_residences"] = [] if should_watch else list(catalog)
        write_bot_state_unlocked(state)
    watched = list(catalog) if should_watch else []
    return catalog, watched


def telegram_update_offset():
    with BOT_STATE_LOCK:
        return read_bot_state_unlocked()["telegram_offset"]


def save_telegram_update_offset(offset):
    with BOT_STATE_LOCK:
        state = read_bot_state_unlocked()
        state["telegram_offset"] = max(state["telegram_offset"], offset)
        write_bot_state_unlocked(state)


def remember_processed_telegram_offset(offset):
    global PROCESSED_TELEGRAM_OFFSET
    with PROCESSED_OFFSET_LOCK:
        PROCESSED_TELEGRAM_OFFSET = max(
            PROCESSED_TELEGRAM_OFFSET,
            offset,
        )


def processed_telegram_offset():
    with PROCESSED_OFFSET_LOCK:
        return PROCESSED_TELEGRAM_OFFSET


def persist_processed_telegram_offset():
    target_offset = processed_telegram_offset()
    if target_offset <= 0:
        return True
    try:
        save_telegram_update_offset(target_offset)
    except OSError:
        return False
    return True


def mark_telegram_update_processed(update_id):
    remember_processed_telegram_offset(update_id + 1)
    if not persist_processed_telegram_offset():
        log(
            "Telegram update offset could not be saved; "
            "it will be retried and may repeat after a restart."
        )


def telegram_ui_version():
    with BOT_STATE_LOCK:
        return read_bot_state_unlocked()["telegram_ui_version"]


def save_telegram_ui_version(version):
    with BOT_STATE_LOCK:
        state = read_bot_state_unlocked()
        state["telegram_ui_version"] = max(
            state["telegram_ui_version"], version
        )
        write_bot_state_unlocked(state)


def sol_check_action_key(action):
    if not isinstance(action, dict):
        return None
    if action.get("kind") == "message":
        if action.get("action") == "check":
            return "check"
        if action.get("action") != "residences":
            return None
        catalog, _ = residence_preferences()
        return "discovery" if not catalog else None
    if action.get("kind") != "callback":
        return None
    callback = parse_residence_callback(action.get("data", ""))
    if callback is not None and callback["target"] == "refresh":
        return "refresh"
    catalog, _ = residence_preferences()
    return "discovery" if not catalog else None


def claim_sol_check_action(action_key):
    with CHECK_ACTION_LOCK:
        if action_key in PENDING_SOL_ACTIONS:
            return False
        PENDING_SOL_ACTIONS.add(action_key)
        return True


def release_sol_check_action(action_key):
    with CHECK_ACTION_LOCK:
        PENDING_SOL_ACTIONS.discard(action_key)


def first_visible(locator, description, timeout_seconds=30):
    deadline = time.monotonic() + timeout_seconds
    last_browser_error = None
    while True:
        try:
            for index in range(locator.count()):
                item = locator.nth(index)
                if item.is_visible():
                    return item
        except PlaywrightError as error:
            last_browser_error = error
        if time.monotonic() >= deadline:
            raise CheckerError(
                f"Could not find the visible {description} control."
            ) from last_browser_error
        time.sleep(0.2)


def only_visible(locator, description, timeout_seconds=30):
    deadline = time.monotonic() + timeout_seconds
    last_browser_error = None
    while True:
        try:
            visible = []
            for index in range(locator.count()):
                item = locator.nth(index)
                if item.is_visible():
                    visible.append(item)

            if len(visible) == 1:
                return visible[0]
            if len(visible) > 1:
                raise CheckerError(f"Found multiple visible {description} controls.")
        except PlaywrightError as error:
            last_browser_error = error

        if time.monotonic() >= deadline:
            raise CheckerError(
                f"Could not find the visible {description} control."
            ) from last_browser_error
        time.sleep(0.2)


def visible_now(locator):
    for index in range(locator.count()):
        item = locator.nth(index)
        if item.is_visible():
            return item
    return None


def declaration_yes_control(page, heading_name):
    """Return the attached YES radio for a visible declaration page."""
    heading = visible_now(page.get_by_role("heading", name=heading_name))
    if heading is None:
        return None

    yes_options = page.locator(
        'input#PRESA_VISIONE_S[name="PRESA_VISIONE"][value="S"]'
    )
    if yes_options.count() != 1:
        return None

    # SOL hides the native radio and makes its label the visible control.
    # Requiring both avoids matching an unrelated hidden input.
    yes_label = page.locator('label[for="PRESA_VISIONE_S"]')
    if visible_now(yes_label) is None:
        return None

    save_buttons = page.get_by_role(
        "button",
        name=re.compile(
            r"(?:Salva e Continua|Save\s*&\s*Continue)",
            re.IGNORECASE,
        ),
    )
    if visible_now(save_buttons) is None:
        return None
    return yes_options.first


def click_language_if_present(page, names):
    locator = page.get_by_role("link", name=re.compile(names, re.IGNORECASE))
    for index in range(locator.count()):
        item = locator.nth(index)
        if item.is_visible():
            item.click()
            return True
    return False


def select_sol_english(page):
    last_error = None
    last_step = "loading the SOL landing page"

    for attempt in range(1, PORTAL_LOAD_ATTEMPTS + 1):
        try:
            if attempt > 1:
                last_step = "reloading the SOL landing page"
                response = page.reload(wait_until="commit", timeout=60_000)
                if response is not None and response.status >= 500:
                    raise CheckerError(
                        f"The SOL portal returned HTTP {response.status}."
                    )

            last_step = "waiting for the SOL landing page"
            if wait_for_sol_page_state(
                page, timeout_seconds=30, accepted_states=("landing",)
            ) != "landing":
                raise CheckerError("The SOL landing page was not ready.")

            # The AJAX content can appear just before its menu handler is ready.
            # A short settle avoids clicking that half-initialized render.
            time.sleep(2)

            # Reacquire every locator after a reload or SOL DOM replacement.
            language_toggles = page.locator('a[aria-haspopup="true"]')
            english_toggle = language_toggles.filter(
                has_text=re.compile(r"^\s*English\s*$", re.IGNORECASE)
            )
            if visible_now(english_toggle) is not None:
                return

            italian_toggle = only_visible(
                language_toggles.filter(
                    has_text=re.compile(r"^\s*Italiano\s*$", re.IGNORECASE)
                ),
                "SOL language menu",
                timeout_seconds=15,
            )
            last_step = "opening the SOL language menu"
            if italian_toggle.get_attribute("aria-expanded") != "true":
                italian_toggle.click(timeout=10_000)

            last_step = "waiting for the expanded SOL language menu"
            open_menu = only_visible(
                page.locator(".dropdown-menu.show").filter(
                    has_text=re.compile(r"\bEnglish\b", re.IGNORECASE)
                ),
                "expanded SOL language menu",
                timeout_seconds=10,
            )
            english_option = only_visible(
                open_menu.locator(
                    "a.dropdown-item, button.dropdown-item, [role=menuitem]"
                ).filter(
                    has_text=re.compile(r"^\s*English\s*$", re.IGNORECASE)
                ),
                "English language",
                timeout_seconds=10,
            )

            last_step = "selecting English from the SOL language menu"
            english_option.click(timeout=10_000)

            last_step = "verifying the English SOL landing page"
            english_toggle = page.locator('a[aria-haspopup="true"]').filter(
                has_text=re.compile(r"^\s*English\s*$", re.IGNORECASE)
            )
            only_visible(
                english_toggle,
                "English SOL language indicator",
                timeout_seconds=15,
            )
            if wait_for_sol_page_state(
                page, timeout_seconds=15, accepted_states=("landing",)
            ) == "landing":
                return
            raise CheckerError("The SOL landing page did not settle in English.")
        except (CheckerError, PlaywrightError) as error:
            last_error = error
            if attempt < PORTAL_LOAD_ATTEMPTS:
                log(
                    f"SOL language attempt {attempt}/{PORTAL_LOAD_ATTEMPTS} "
                    f"failed while {last_step}; reloading in 1 second."
                )
                time.sleep(1)

    raise CheckerError(
        "Could not switch the SOL landing page to English after "
        f"{PORTAL_LOAD_ATTEMPTS} attempts (last step: {last_step})."
    ) from last_error


def sol_login_cards(page):
    return page.locator(".info-box.pointer").filter(
        has_text=re.compile(r"^\s*LOGIN(?:\s|$)", re.IGNORECASE)
    )


def sol_page_state(page):
    if page.locator(academic_year_panel(ACADEMIC_YEAR)).count() > 0:
        return "authenticated"
    if visible_now(page.locator("input#login")) is not None:
        return "sso"
    if visible_now(sol_login_cards(page)) is not None:
        return "landing"
    return None


def wait_for_sol_page_state(page, timeout_seconds=30, accepted_states=None):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            state = sol_page_state(page)
            if state is not None and (
                accepted_states is None or state in accepted_states
            ):
                return state
        except PlaywrightError:
            # Redirects and the SOL AJAX renderer briefly replace the document.
            pass
        time.sleep(0.2)
    return None


def wait_for_signed_out_sol_state(page, timeout_seconds=30):
    """Require a stable signed-out page; authenticated always takes priority."""
    deadline = time.monotonic() + timeout_seconds
    candidate_state = None
    stable_since = None

    while time.monotonic() < deadline:
        try:
            state = sol_page_state(page)
            if state == "authenticated":
                return state
            if state in ("landing", "sso"):
                if state != candidate_state:
                    candidate_state = state
                    stable_since = time.monotonic()
                elif (
                    time.monotonic() - stable_since
                    >= LOGOUT_STATE_STABLE_SECONDS
                ):
                    return state
            else:
                candidate_state = None
                stable_since = None
        except PlaywrightError:
            candidate_state = None
            stable_since = None
        time.sleep(0.2)
    return None


def open_sol_entry(page):
    last_error = None

    for attempt in range(1, PORTAL_LOAD_ATTEMPTS + 1):
        try:
            state = sol_page_state(page)
            if state is not None:
                return state

            response = page.goto(PORTAL_URL, wait_until="commit", timeout=60_000)
            if response is not None and response.status >= 500:
                raise CheckerError(f"The SOL portal returned HTTP {response.status}.")

            state = wait_for_sol_page_state(page)
            if state is not None:
                return state
            last_error = CheckerError("The SOL portal did not show a usable page.")
        except (CheckerError, PlaywrightError) as error:
            last_error = error

        if attempt < PORTAL_LOAD_ATTEMPTS:
            log(
                f"SOL portal attempt {attempt}/{PORTAL_LOAD_ATTEMPTS} failed; "
                f"retrying in {PORTAL_RETRY_SECONDS} seconds."
            )
            time.sleep(PORTAL_RETRY_SECONDS)

    raise CheckerError(
        "Could not reach the SOL landing page or Polimi SSO after "
        f"{PORTAL_LOAD_ATTEMPTS} attempts."
    ) from last_error


def open_polimi_sso(page):
    last_error = None

    for attempt in range(1, PORTAL_LOAD_ATTEMPTS + 1):
        try:
            state = sol_page_state(page)
            if state in ("sso", "authenticated"):
                return state
            if state is None:
                state = wait_for_sol_page_state(page, timeout_seconds=10)
                if state in ("sso", "authenticated"):
                    return state
            if state != "landing":
                state = open_sol_entry(page)
                if state in ("sso", "authenticated"):
                    return state
                if state != "landing":
                    raise CheckerError("The SOL landing page was not ready.")

            # Reacquire the card on every attempt because SOL replaces its DOM.
            login_card = first_visible(
                sol_login_cards(page), "LOGIN card", timeout_seconds=10
            )
            login_card.click()
        except (CheckerError, PlaywrightError) as error:
            last_error = error

        state = wait_for_sol_page_state(
            page, accepted_states=("sso", "authenticated")
        )
        if state in ("sso", "authenticated"):
            return state

        if attempt < PORTAL_LOAD_ATTEMPTS:
            log(
                f"SOL LOGIN attempt {attempt}/{PORTAL_LOAD_ATTEMPTS} failed; "
                f"retrying in {PORTAL_RETRY_SECONDS} seconds."
            )
            time.sleep(PORTAL_RETRY_SECONDS)

    raise CheckerError(
        "The LOGIN card could not open Polimi SSO after "
        f"{PORTAL_LOAD_ATTEMPTS} attempts."
    ) from last_error


def fresh_totp(totp):
    seconds_left = totp.interval - (int(time.time()) % totp.interval)
    if seconds_left <= 10:
        time.sleep(seconds_left + 1)
    return totp.now()


def login(page, totp):
    state = open_sol_entry(page)
    if state == "authenticated":
        return

    if state == "landing":
        with browser_stage("selecting English on the SOL landing page"):
            select_sol_english(page)

        state = open_polimi_sso(page)
        if state == "authenticated":
            return
        if state != "sso":
            raise CheckerError("The SOL LOGIN card did not reach Polimi SSO.")

    with browser_stage("selecting English on Polimi SSO"):
        # The SSO form may initially appear in Italian even when SOL is English.
        click_language_if_present(page, r"^EN$")

    with browser_stage("submitting Polimi credentials"):
        # These SSO inputs use placeholders but have no associated HTML labels.
        first_visible(page.locator("input#login"), "person code").fill(
            POLIMI_USERNAME
        )
        first_visible(page.locator("input#password"), "password").fill(
            POLIMI_PASSWORD
        )
        first_visible(
            page.get_by_role(
                "button", name=re.compile(r"^(Sign in|Accedi)$", re.IGNORECASE)
            ),
            "sign-in",
        ).click()

    with browser_stage("completing Polimi OTP"):
        otp_input = first_visible(
            page.locator(
                'input[placeholder="OTP" i], input[name="OTP" i], input[id="OTP" i]'
            ),
            "OTP",
        )
        otp_input.fill(fresh_totp(totp))
        first_visible(
            page.get_by_role(
                "button", name=re.compile(r"^(Continue|Continua)$", re.IGNORECASE)
            ),
            "OTP continue",
        ).click()

        page.wait_for_url(
            re.compile(r"dirittoallostudio\.it/apps/V3\.1/sol/public/index\.php"),
            wait_until="domcontentloaded",
            timeout=60_000,
        )


def find_availability_table(page):
    """Return the visible room-count table, ignoring unrelated layout tables."""
    tables = page.locator("table")
    for index in range(tables.count()):
        table = tables.nth(index)
        if not table.is_visible():
            continue

        headers = table.locator("thead th")
        count_texts = table.locator("tbody td button").all_inner_texts()
        if (
            headers.count() >= 2
            and count_texts
            and all(ROOM_COUNT_PATTERN.fullmatch(text) for text in count_texts)
        ):
            return table
    return None


def availability_table_signature(table):
    headers = tuple(table.locator("thead th").all_inner_texts())
    rows = table.locator("tbody tr").count()
    count_texts = tuple(table.locator("tbody td button").all_inner_texts())
    return headers, rows, count_texts


def residence_notice_yes_control(page):
    return declaration_yes_control(
        page,
        re.compile(
            r"^University residences\s*-\s*"
            r"Details of the university residences$",
            re.IGNORECASE,
        ),
    )


def wait_for_booking_state(page, timeout_seconds=BOOKING_PAGE_TIMEOUT_SECONDS):
    """Wait for a declaration step or an already-open availability table."""
    deadline = time.monotonic() + timeout_seconds
    last_browser_error = None

    while time.monotonic() < deadline:
        try:
            table = find_availability_table(page)
            if table is not None:
                seconds_left = max(1, deadline - time.monotonic())
                table = wait_for_availability_table(page, seconds_left)
                return "table", table

            residence_notice_yes = residence_notice_yes_control(page)
            if residence_notice_yes is not None:
                return "residence_notice", residence_notice_yes

            privacy_yes = declaration_yes_control(
                page,
                re.compile(r"Privacy", re.IGNORECASE),
            )
            if privacy_yes is not None:
                return "privacy", privacy_yes
        except PlaywrightError as error:
            # The portal replaces sections while its AJAX requests finish.
            last_browser_error = error
        time.sleep(0.25)

    raise CheckerError(
        "The booking page did not show a declaration step or availability table."
    ) from last_browser_error


def wait_after_privacy_notice(
    page,
    timeout_seconds=BOOKING_PAGE_TIMEOUT_SECONDS,
):
    """Wait until the first declaration transitions to the added notice or table."""
    deadline = time.monotonic() + timeout_seconds
    last_browser_error = None

    while time.monotonic() < deadline:
        try:
            table = find_availability_table(page)
            if table is not None:
                seconds_left = max(1, deadline - time.monotonic())
                table = wait_for_availability_table(page, seconds_left)
                return "table", table

            residence_notice_yes = residence_notice_yes_control(page)
            if residence_notice_yes is not None:
                return "residence_notice", residence_notice_yes
        except PlaywrightError as error:
            last_browser_error = error
        time.sleep(0.25)

    raise CheckerError(
        "The privacy declaration did not advance to the residence notice "
        "or availability table."
    ) from last_browser_error


def wait_for_availability_table(
    page, timeout_seconds=BOOKING_PAGE_TIMEOUT_SECONDS
):
    deadline = time.monotonic() + timeout_seconds
    last_browser_error = None
    last_signature = None
    stable_since = None

    while time.monotonic() < deadline:
        try:
            table = find_availability_table(page)
            if table is not None:
                signature = availability_table_signature(table)
                if signature != last_signature:
                    last_signature = signature
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= TABLE_STABLE_SECONDS:
                    return table
            else:
                last_signature = None
                stable_since = None
        except PlaywrightError as error:
            last_browser_error = error
            last_signature = None
            stable_since = None
        time.sleep(0.25)

    raise CheckerError(
        "The availability table did not finish loading."
    ) from last_browser_error


def open_availability_table(page):
    include_residence_notice_page = boolean_setting(
        "INCLUDE_RESIDENCE_NOTICE_PAGE",
        INCLUDE_RESIDENCE_NOTICE_PAGE_VALUE,
    )

    with browser_stage(f"opening the {ACADEMIC_YEAR} section"):
        panel = page.locator(academic_year_panel(ACADEMIC_YEAR)).first
        panel.wait_for(state="attached", timeout=30_000)

        # English was selected on the landing page before LOGIN. Verify that the
        # authenticated portal preserved it before reading table headers.
        only_visible(
            page.get_by_role(
                "link", name=re.compile(r"^English$", re.IGNORECASE)
            ),
            "English portal language indicator",
        )

        if not panel.is_visible():
            year_controls = page.get_by_text(ACADEMIC_YEAR, exact=True)
            first_visible(year_controls, ACADEMIC_YEAR).click()
        panel.wait_for(state="visible", timeout=30_000)

    with browser_stage("selecting full-rate Accommodation Booking"):
        full_rate_name = re.compile(
            r"^(?:Alloggio\s+Studenti\s+a\s+TARIFFA\s+INTERA|"
            r"Student\s+accommodation\s+at\s+FULL\s+RATE)$",
            re.IGNORECASE,
        )
        full_rate_heading = only_visible(
            panel.get_by_role("heading", name=full_rate_name),
            f"{ACADEMIC_YEAR} full-rate accommodation section",
        )
        full_rate_section = full_rate_heading.locator(
            'xpath=ancestor::div['
            'contains(concat(" ", normalize-space(@class), " "), " row ")'
            '][1]'
        )
        full_rate_section.wait_for(state="visible", timeout=30_000)

        booking_name = re.compile(
            r"^(Prenotazione Alloggio|Accommodation Booking)$", re.IGNORECASE
        )
        booking_heading = only_visible(
            full_rate_section.get_by_role("heading", name=booking_name),
            "full-rate Accommodation Booking heading",
        )
        booking_card = booking_heading.locator(
            'xpath=ancestor::div['
            'contains(concat(" ", normalize-space(@class), " "), " small-box ")'
            '][1]'
        )
        booking_card.wait_for(state="visible", timeout=30_000)
        booking_link = only_visible(
            booking_card.locator("a.small-box-footer"),
            "full-rate Accommodation Booking link",
        )
        booking_link.click()

    with browser_stage("waiting for the accommodation booking page"):
        state, control = wait_for_booking_state(page)

    if state == "privacy":
        with browser_stage("confirming the accommodation privacy notice"):
            yes = control
            if not yes.is_checked():
                yes.check(force=True)
            if not yes.is_checked():
                raise CheckerError("The privacy Yes option was not selected.")

        with browser_stage("submitting the accommodation privacy notice"):
            save_buttons = page.get_by_role(
                "button",
                name=re.compile(
                    r"(Salva e Continua|Save\s*&\s*Continue)", re.IGNORECASE
                ),
            )
            first_visible(save_buttons, "Save & Continue").click()

        with browser_stage(
            "waiting after the accommodation privacy notice"
        ):
            state, control = wait_after_privacy_notice(page)

    if state == "residence_notice":
        if not include_residence_notice_page:
            raise CheckerError(
                "SOL showed the University residences notice page while "
                "INCLUDE_RESIDENCE_NOTICE_PAGE=false. Set it to true."
            )

        with browser_stage("confirming the university residences notice"):
            yes = control
            if not yes.is_checked():
                yes.check(force=True)
            if not yes.is_checked():
                raise CheckerError(
                    "The university residences notice Yes option was not selected."
                )

        with browser_stage("submitting the university residences notice"):
            save_buttons = page.get_by_role(
                "button",
                name=re.compile(
                    r"Save\s*&\s*Continue",
                    re.IGNORECASE,
                ),
            )
            first_visible(
                save_buttons,
                "university residences Save & Continue",
            ).click()

        with browser_stage(
            "waiting for the availability table after the residence notice"
        ):
            return wait_for_availability_table(page)

    if state == "table" and include_residence_notice_page:
        # The portal can remember completed declarations and reopen the table
        # directly. This is already the requested destination, so no notice
        # interaction is needed.
        return control

    return control


def logout(page):
    account_menus = page.locator("li.nav-item.dropdown").filter(
        has_text=re.compile(r"\bLogout\b", re.IGNORECASE)
    )
    account_menu = only_visible(account_menus, "account menu")
    account_toggle = only_visible(
        account_menu.locator("a.nav-link.dropdown-toggle"),
        "account-menu toggle",
    )
    if account_toggle.get_attribute("aria-expanded") != "true":
        account_toggle.click()

    logout_links = account_menu.get_by_role(
        "link", name=re.compile(r"^Logout$", re.IGNORECASE)
    )
    first_visible(logout_links, "Logout").click()

    logout_dialogs = page.get_by_role(
        "dialog", name=re.compile(r"^Logout$", re.IGNORECASE)
    )
    logout_dialog = only_visible(logout_dialogs, "Logout dialog")
    confirm_buttons = logout_dialog.get_by_role(
        "button", name=re.compile(r"^(Conferma|Confirm)$", re.IGNORECASE)
    )
    context = page.context
    click_error = None
    try:
        first_visible(confirm_buttons, "logout confirmation").click(
            timeout=10_000,
        )
    except PlaywrightError as error:
        # The confirmed cross-site navigation can close or replace this tab.
        # Verify the session below before deciding whether the click failed.
        click_error = error

    # Prove the session ended instead of relying on a transient logout URL or
    # on the source tab remaining available after the cross-site redirect.
    verify_page = None
    last_error = click_error
    saw_authenticated = False
    try:
        verify_page = context.new_page()
        verify_page.set_default_timeout(30_000)

        for attempt in range(1, LOGOUT_VERIFY_ATTEMPTS + 1):
            try:
                response = verify_page.goto(
                    PORTAL_URL,
                    wait_until="commit",
                    timeout=45_000,
                )
                if response is not None and response.status >= 500:
                    raise CheckerError(
                        f"The SOL portal returned HTTP {response.status}."
                    )

                state = wait_for_signed_out_sol_state(
                    verify_page,
                    timeout_seconds=30,
                )
                if state in ("landing", "sso"):
                    return
                if state == "authenticated":
                    saw_authenticated = True
                    last_error = CheckerError(
                        "SOL still appeared authenticated after logout."
                    )
                else:
                    last_error = CheckerError(
                        "The SOL logout state could not be determined."
                    )
            except (CheckerError, PlaywrightError) as error:
                last_error = error

            if attempt < LOGOUT_VERIFY_ATTEMPTS:
                time.sleep(1)
    except PlaywrightError as error:
        last_error = error
    finally:
        if verify_page is not None:
            with suppress(PlaywrightError):
                verify_page.close()

    if saw_authenticated:
        description = "SOL still appeared authenticated after logout."
    else:
        description = "Could not verify that SOL signed out."
    raise CheckerError(description) from last_error


def clean_text(value):
    return " ".join(value.split())


def room_count(value):
    match = ROOM_COUNT_PATTERN.fullmatch(clean_text(value))
    if not match:
        raise CheckerError("An availability cell had an unexpected format.")
    return int(match.group(1))


def read_availability(table):
    headers = [clean_text(text) for text in table.locator("thead th").all_inner_texts()]
    if len(headers) < 2:
        raise CheckerError("The availability table has no room-type columns.")

    room_types = headers[1:]
    rows = table.locator("tbody tr")
    if rows.count() == 0:
        raise CheckerError("The availability table has no residences.")

    available = []
    checked_cells = 0
    residence_totals = {}

    for row_index in range(rows.count()):
        row = rows.nth(row_index)
        residence = clean_text(row.locator("th").first.inner_text())
        count_texts = row.locator("td button").all_inner_texts()

        if len(count_texts) != len(room_types):
            raise CheckerError("The availability table shape was not recognized.")

        residence_total = 0
        for room_type, count_text in zip(room_types, count_texts):
            count = room_count(count_text)
            checked_cells += 1
            residence_total += count
            if count > 0:
                available.append(
                    {
                        "residence": residence,
                        "room_type": room_type,
                        "count": count,
                    }
                )
        residence_totals[residence] = residence_total

    return available, checked_cells, residence_totals


def notification_timestamp():
    timestamp = datetime.now(TEHRAN_TIMEZONE).strftime("%d %b %Y, %H:%M")
    return f"{timestamp} (Tehran)"


def count_label(count, singular, plural=None):
    return singular if count == 1 else (plural or f"{singular}s")


def alert_text(available, residence_totals, watched_residences):
    watched_residences = set(watched_residences)
    detailed_by_residence = {}
    for item in available:
        if item["residence"] in watched_residences:
            detailed_by_residence.setdefault(item["residence"], []).append(item)

    watched_totals = {
        residence: total
        for residence, total in residence_totals.items()
        if residence in detailed_by_residence and total > 0
    }
    unmonitored_totals = {
        residence: total
        for residence, total in residence_totals.items()
        if residence not in watched_residences and total > 0
    }
    has_watched_availability = bool(watched_totals)

    if has_watched_availability:
        relevant_totals = watched_totals
        lines = [
            "🎉 MONITORED RESIDENCE AVAILABLE",
            "🏠 Polimi Residence Checker",
            "",
            "A monitored residence has an available room.",
            "Open SOL now—availability may disappear quickly.",
            f"🎓 Academic year: {ACADEMIC_YEAR}",
            f"🕒 Checked: {notification_timestamp()}",
            "",
            NOTIFICATION_DIVIDER,
            "",
            "⭐ MONITORED AVAILABILITY",
            "",
        ]
    else:
        relevant_totals = unmonitored_totals
        lines = [
            "📊 ROOMS IN UNMONITORED RESIDENCES",
            "🏠 Polimi Residence Checker",
            "",
            "No monitored residence currently has availability.",
            "The rooms below are in unmonitored residences only.",
            f"🎓 Academic year: {ACADEMIC_YEAR}",
            f"🕒 Checked: {notification_timestamp()}",
            "",
            NOTIFICATION_DIVIDER,
            "",
            "📊 UNMONITORED AVAILABILITY",
            "",
        ]

    blocks = []
    if has_watched_availability:
        for residence, residence_total in watched_totals.items():
            block = [
                (
                    f"🏢 {residence} — {residence_total} "
                    f"{count_label(residence_total, 'room')}"
                )
            ]
            for item in detailed_by_residence[residence]:
                block.append(f"  🛏️ {item['room_type']} — {item['count']}")
            block.append("")
            blocks.append(block)
    else:
        for residence, total in relevant_totals.items():
            blocks.append(
                [
                    (
                        f"🏢 {residence} — {total} "
                        f"{count_label(total, 'room')}"
                    )
                ]
            )

    for block in blocks:
        lines.extend(block)

    if lines[-1] == "":
        lines.pop()

    if has_watched_availability and unmonitored_totals:
        lines.extend(
            [
                "",
                NOTIFICATION_DIVIDER,
                "",
                "📊 UNMONITORED AVAILABILITY",
                "",
            ]
        )
        for residence, total in unmonitored_totals.items():
            lines.append(
                f"🏢 {residence} — {total} "
                f"{count_label(total, 'room')}"
            )

    total_rooms = sum(relevant_totals.values())
    available_residences = len(relevant_totals)
    total_label = "Monitored availability" if has_watched_availability else (
        "Unmonitored availability"
    )
    lines.extend(
        [
            "",
            NOTIFICATION_DIVIDER,
            (
                f"🔑 {total_label}: {total_rooms} "
                f"{count_label(total_rooms, 'room')} across "
                f"{available_residences} "
                f"{count_label(available_residences, 'residence', 'residences')}"
            ),
        ]
    )
    if has_watched_availability:
        if unmonitored_totals:
            unmonitored_rooms = sum(unmonitored_totals.values())
            unmonitored_residences = len(unmonitored_totals)
            lines.append(
                f"📊 Unmonitored availability: {unmonitored_rooms} "
                f"{count_label(unmonitored_rooms, 'room')} across "
                f"{unmonitored_residences} "
                f"{count_label(unmonitored_residences, 'residence', 'residences')}"
            )
        lines.extend(
            [
                "⚡ Action recommended: check SOL now.",
                f"🔗 Open SOL: {PORTAL_URL}",
            ]
        )
    else:
        lines.append(
            "ℹ️ No action is needed unless these residences interest you."
        )
    return "\n".join(lines)


def no_availability_text(checked_cells, residence_totals):
    residence_count = len(residence_totals)
    return "\n".join(
        [
            "😴 NO ROOMS AVAILABLE",
            "🏠 Polimi Residence Checker",
            "",
            "The check completed successfully, but every room count was zero.",
            "No action is needed.",
            f"🎓 Academic year: {ACADEMIC_YEAR}",
            f"🕒 Checked: {notification_timestamp()}",
            "",
            NOTIFICATION_DIVIDER,
            "",
            (
                f"🔎 Checked {residence_count} "
                f"{count_label(residence_count, 'residence', 'residences')} "
                f"and {checked_cells} room combinations."
            ),
        ]
    )


def residence_menu_revision(catalog):
    payload = json.dumps(
        list(catalog),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]


def main_menu_keyboard():
    return {
        "keyboard": [
            [
                {"text": BUTTON_CHECK_NOW},
                {"text": BUTTON_MANAGE_RESIDENCES},
            ],
            [{"text": BUTTON_HELP}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def residence_menu_keyboard(catalog, watched):
    watched = set(watched)
    revision = residence_menu_revision(catalog)
    buttons = []
    for index, residence in enumerate(catalog):
        is_watched = residence in watched
        marker = "✅ Monitored" if is_watched else "▫️ Total only"
        desired_state = "0" if is_watched else "1"
        buttons.append(
            [
                {
                    "text": f"{marker} · {residence}",
                    "callback_data": (
                        f"rh|{revision}|{index}|{desired_state}"
                    ),
                }
            ]
        )
    buttons.extend(
        [
            [
                {
                    "text": "✅ Monitor all",
                    "callback_data": f"rh|{revision}|all|1",
                },
                {
                    "text": "📊 Totals only",
                    "callback_data": f"rh|{revision}|all|0",
                },
            ],
            [
                {
                    "text": "🔄 Refresh list from SOL",
                    "callback_data": f"rh|{revision}|refresh",
                }
            ],
        ]
    )
    return {"inline_keyboard": buttons}


def residence_list_text(catalog, watched, notice=""):
    watched = set(watched)
    lines = [
        "🏘️ MANAGE RESIDENCES",
        "🏠 Polimi Residence Checker",
        "",
    ]
    if notice:
        lines.extend([notice, ""])
    lines.extend(
        [
            "Tap a residence to change how its availability is reported.",
            "✅ Monitored: show room-type details and send the urgent alert",
            "▫️ Total only: show one combined total in the optional alert",
            (
                f"⭐ Monitored: {len(watched)} of {len(catalog)} residences"
            ),
            "",
            NOTIFICATION_DIVIDER,
            "",
        ]
    )
    for residence in catalog:
        marker = "✅" if residence in watched else "▫️"
        lines.append(f"{marker} {residence}")
    lines.extend(
        [
            "",
            NOTIFICATION_DIVIDER,
            "The buttons below contain the complete saved residence list.",
        ]
    )
    return "\n".join(lines)


def telegram_help_text():
    return "\n".join(
        [
            "🤖 POLIMI RESIDENCE CHECKER",
            "",
            "Here is what each button does:",
            "",
            f"{BUTTON_CHECK_NOW}",
            "Run a fresh SOL check and receive the result.",
            "",
            f"{BUTTON_MANAGE_RESIDENCES}",
            "See every residence and choose which ones are monitored.",
            "",
            f"{BUTTON_HELP}",
            "Show this explanation again.",
            "",
            "🔒 Only the configured private Telegram chat can use these controls.",
        ]
    )


def telegram_api_call(method, parameters, timeout_seconds=20):
    encoded_parameters = {}
    for name, value in parameters.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        encoded_parameters[name] = value

    request = Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        data=urlencode(encoded_parameters).encode(),
        method="POST",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
        status = response.status
    if status != 200 or not isinstance(payload, dict) or not payload.get("ok"):
        raise ValueError(f"Telegram rejected {method}.")
    return payload.get("result")


def send_telegram(message, reply_markup=None):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False

    parameters = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    if reply_markup is not None:
        parameters["reply_markup"] = reply_markup

    for attempt in range(1, TELEGRAM_MAX_ATTEMPTS + 1):
        try:
            telegram_api_call("sendMessage", parameters)
            return True
        except (
            HTTPError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ):
            pass

        if attempt < TELEGRAM_MAX_ATTEMPTS:
            log(
                f"Telegram attempt {attempt}/{TELEGRAM_MAX_ATTEMPTS} failed; "
                f"retrying in {TELEGRAM_RETRY_SECONDS} seconds."
            )
            time.sleep(TELEGRAM_RETRY_SECONDS)

    return False


def edit_telegram_message(chat_id, message_id, message, reply_markup):
    parameters = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": message,
        "reply_markup": reply_markup,
    }
    for attempt in range(1, TELEGRAM_MAX_ATTEMPTS + 1):
        try:
            telegram_api_call("editMessageText", parameters)
            return True
        except HTTPError as error:
            description = ""
            with suppress(Exception):
                payload = json.loads(error.read().decode("utf-8"))
                description = str(payload.get("description", "")).lower()
            if error.code == 400 and "message is not modified" in description:
                return True
            if 400 <= error.code < 500 and error.code != 429:
                return False
        except (
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ):
            pass

        if attempt < TELEGRAM_MAX_ATTEMPTS:
            time.sleep(TELEGRAM_RETRY_SECONDS)
    return False


def answer_callback_query(callback_id, text):
    parameters = {"callback_query_id": callback_id, "text": text}
    for attempt in range(1, TELEGRAM_MAX_ATTEMPTS + 1):
        try:
            telegram_api_call("answerCallbackQuery", parameters)
            return True
        except HTTPError as error:
            if 400 <= error.code < 500 and error.code != 429:
                return False
        except (
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ):
            pass
        if attempt < TELEGRAM_MAX_ATTEMPTS:
            time.sleep(TELEGRAM_RETRY_SECONDS)
    return False


def get_telegram_updates(offset):
    result = telegram_api_call(
        "getUpdates",
        {
            "offset": offset,
            "timeout": TELEGRAM_POLL_SECONDS,
            "allowed_updates": ["message", "callback_query"],
        },
        timeout_seconds=TELEGRAM_POLL_NETWORK_TIMEOUT,
    )
    if not isinstance(result, list):
        raise ValueError("Telegram returned an invalid getUpdates result.")
    return result


def authorized_telegram_action(update):
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        callback_id = callback.get("id")
        source = callback.get("from")
        message = callback.get("message")
        data = callback.get("data")
        if (
            not isinstance(callback_id, str)
            or not callback_id
            or not isinstance(source, dict)
            or str(source.get("id")) != TELEGRAM_CHAT_ID
            or not isinstance(message, dict)
            or "inline_message_id" in callback
            or not isinstance(data, str)
            or not 1 <= len(data.encode("utf-8")) <= 64
            or parse_residence_callback(data) is None
        ):
            return None
        chat = message.get("chat")
        message_id = message.get("message_id")
        if (
            not isinstance(chat, dict)
            or str(chat.get("id")) != TELEGRAM_CHAT_ID
            or chat.get("type") != "private"
            or not isinstance(message_id, int)
        ):
            return None
        return {
            "kind": "callback",
            "callback_id": callback_id,
            "chat_id": str(chat["id"]),
            "message_id": message_id,
            "data": data,
        }

    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    source = message.get("from")
    if (
        not isinstance(chat, dict)
        or str(chat.get("id")) != TELEGRAM_CHAT_ID
        or chat.get("type") != "private"
        or not isinstance(source, dict)
        or str(source.get("id")) != TELEGRAM_CHAT_ID
    ):
        return None

    text = message.get("text")
    if text == BUTTON_CHECK_NOW:
        action = "check"
    elif text == BUTTON_MANAGE_RESIDENCES:
        action = "residences"
    else:
        # This also handles Telegram's built-in Start button without requiring
        # the user to type or remember special text.
        action = "help"
    return {"kind": "message", "action": action}


def callback_id_from_update(update):
    callback = update.get("callback_query")
    if not isinstance(callback, dict):
        return None
    callback_id = callback.get("id")
    return callback_id if isinstance(callback_id, str) and callback_id else None


def telegram_action_listener():
    offset = telegram_update_offset()
    queued_update_ids = set()
    log("Telegram button listener started.")

    while True:
        try:
            durable_offset = telegram_update_offset()
            in_memory_offset = processed_telegram_offset()
            if in_memory_offset > durable_offset:
                if persist_processed_telegram_offset():
                    durable_offset = in_memory_offset
            if durable_offset > offset:
                offset = durable_offset
                queued_update_ids = {
                    update_id
                    for update_id in queued_update_ids
                    if update_id >= offset
                }

            updates = get_telegram_updates(offset)
            queued_new_update = False
            for update in updates:
                update_id = update.get("update_id")
                if (
                    not isinstance(update_id, int)
                    or update_id < offset
                    or update_id in queued_update_ids
                ):
                    continue

                action = authorized_telegram_action(update)
                callback_id = callback_id_from_update(update)
                action_key = sol_check_action_key(action)
                if action_key is not None:
                    if claim_sol_check_action(action_key):
                        action["sol_check_key"] = action_key
                        if callback_id:
                            answer_callback_query(
                                callback_id,
                                "SOL check queued…",
                            )
                    else:
                        if callback_id:
                            answer_callback_query(
                                callback_id,
                                "A SOL check is already in progress.",
                            )
                        else:
                            send_notification(
                                "\n".join(
                                    [
                                        "⏳ REQUEST ALREADY IN PROGRESS",
                                        "",
                                        "Please wait for the current SOL request to finish.",
                                    ]
                                ),
                                "duplicate request response",
                            )
                        action = None
                elif callback_id:
                    callback_text = (
                        "Updating…"
                        if action is not None
                        else "Not authorized."
                    )
                    answer_callback_query(callback_id, callback_text)

                TELEGRAM_ACTION_QUEUE.put((update_id, action))
                queued_update_ids.add(update_id)
                queued_new_update = True

            # With an unconfirmed queued update, getUpdates returns immediately
            # instead of long-polling. A short pause avoids a busy loop while
            # still discovering and acknowledging new callbacks promptly.
            if updates and not queued_new_update:
                time.sleep(TELEGRAM_DUPLICATE_POLL_SECONDS)
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
            log(
                "Telegram button polling failed; retrying in "
                f"{TELEGRAM_POLL_RETRY_SECONDS} seconds."
            )
            time.sleep(TELEGRAM_POLL_RETRY_SECONDS)
        except Exception as error:
            log(
                "Telegram button listener recovered from "
                f"{type(error).__name__}; retrying."
            )
            time.sleep(TELEGRAM_POLL_RETRY_SECONDS)


def start_telegram_action_listener():
    listener = threading.Thread(
        target=telegram_action_listener,
        name="telegram-button-listener",
        daemon=True,
    )
    listener.start()
    return listener


def telegram_configured():
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def telegram_message_chunks(message):
    chunks = []
    remaining = message
    while len(remaining) > TELEGRAM_MESSAGE_LIMIT:
        split_at = remaining.rfind("\n", 0, TELEGRAM_MESSAGE_LIMIT + 1)
        if split_at <= 0:
            split_at = TELEGRAM_MESSAGE_LIMIT
        chunks.append(remaining[:split_at].rstrip("\n"))
        remaining = remaining[split_at:].lstrip("\n")
    chunks.append(remaining)
    return chunks


def send_notification(message, description, reply_markup=None):
    if not telegram_configured():
        log(f"Telegram is not configured; {description} was not sent.")
        return False
    if reply_markup is None:
        reply_markup = main_menu_keyboard()
    chunks = telegram_message_chunks(message)
    for index, chunk in enumerate(chunks, start=1):
        chunk_markup = reply_markup if index == len(chunks) else None
        if not send_telegram(chunk, chunk_markup):
            log(
                f"Telegram {description} failed on part {index}/{len(chunks)}; "
                "check its settings and the network."
            )
            return False
    suffix = f" in {len(chunks)} parts" if len(chunks) > 1 else ""
    log(f"Telegram {description} sent{suffix}.")
    return True


def send_error_notification(description):
    message = "\n".join(
        [
            "⚠️ CHECK FAILED",
            "🏠 Polimi Residence Checker",
            "",
            "The accommodation check could not be completed.",
            f"🎓 Academic year: {ACADEMIC_YEAR}",
            f"🕒 Failed: {notification_timestamp()}",
            "",
            NOTIFICATION_DIVIDER,
            "",
            f"Reason: {description}",
            "",
            "🔧 Review the PM2 logs for technical details, then try again.",
            "🛡️ No room was booked.",
        ]
    )
    send_notification(message, "error alert")


def check_once(totp):
    log("Starting accommodation check.")
    logout_error = None
    result = None

    with sync_playwright() as playwright:
        with browser_stage("starting Chromium"):
            browser = playwright.chromium.launch(headless=True)
        try:
            with browser_stage("creating the browser session"):
                context = browser.new_context(locale="en-GB")
                page = context.new_page()
                page.set_default_timeout(30_000)

            login(page, totp)
            with browser_stage("opening the accommodation availability table"):
                table = open_availability_table(page)
            with browser_stage("reading the accommodation availability table"):
                available, checked_cells, residence_totals = read_availability(table)

            try:
                watched_residences = refresh_residence_catalog(
                    residence_totals.keys()
                )
            except OSError:
                log(
                    "Residence preferences could not be saved; "
                    "treating all residences as watched for this check."
                )
                watched_residences = set(residence_totals)

            result = {
                "available": available,
                "checked_cells": checked_cells,
                "residence_totals": residence_totals,
                "watched_residences": watched_residences,
            }

            try:
                with browser_stage("logging out of SOL"):
                    logout(page)
                log("Logged out of SOL.")
            except CheckerError as error:
                # Preserve and report any room result before reporting cleanup failure.
                logout_error = error
        finally:
            with suppress(PlaywrightError):
                browser.close()

    return result, logout_error


def configuration(use_start_hour=True):
    missing = []
    if not POLIMI_USERNAME:
        missing.append("POLIMI_USERNAME")
    if not POLIMI_PASSWORD:
        missing.append("POLIMI_PASSWORD")
    if not POLIMI_TOTP_URI:
        missing.append("POLIMI_TOTP_URI")
    if missing:
        raise CheckerError(f"Missing .env values: {', '.join(missing)}")

    if bool(TELEGRAM_BOT_TOKEN) != bool(TELEGRAM_CHAT_ID):
        raise CheckerError(
            "Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or leave both empty."
        )

    academic_year_start(ACADEMIC_YEAR)

    boolean_setting(
        "INCLUDE_RESIDENCE_NOTICE_PAGE",
        INCLUDE_RESIDENCE_NOTICE_PAGE_VALUE,
    )

    try:
        interval_hours = float(CHECK_INTERVAL_HOURS)
    except ValueError as error:
        raise CheckerError("CHECK_INTERVAL_HOURS must be a number.") from error
    if not math.isfinite(interval_hours) or interval_hours <= 0:
        raise CheckerError("CHECK_INTERVAL_HOURS must be a finite number above zero.")

    start_hour = None
    if use_start_hour and CHECK_START_HOUR:
        if not re.fullmatch(r"[0-9]{1,2}", CHECK_START_HOUR):
            raise CheckerError(
                "CHECK_START_HOUR must be a whole hour from 0 to 23, or blank."
            )
        start_hour = int(CHECK_START_HOUR)
        if not 0 <= start_hour <= 23:
            raise CheckerError(
                "CHECK_START_HOUR must be a whole hour from 0 to 23, or blank."
            )
        intervals_per_day = 24 / interval_hours
        if not math.isclose(
            intervals_per_day,
            round(intervals_per_day),
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise CheckerError(
                "When CHECK_START_HOUR is set, CHECK_INTERVAL_HOURS "
                "must divide 24 hours evenly."
            )

    try:
        totp = pyotp.parse_uri(POLIMI_TOTP_URI)
    except Exception as error:
        raise CheckerError("POLIMI_TOTP_URI is not a valid TOTP URI.") from error

    if not isinstance(totp, pyotp.TOTP):
        raise CheckerError("POLIMI_TOTP_URI must describe a TOTP credential.")
    return totp, interval_hours, start_hour


def parse_arguments():
    parser = argparse.ArgumentParser(description="Check Polimi room availability.")
    parser.add_argument(
        "--mode",
        choices=("interval", "tehran"),
        default="interval",
        help=(
            "interval starts at CHECK_START_HOUR when set, otherwise "
            "immediately, then checks every CHECK_INTERVAL_HOURS; "
            "tehran checks at 09:00 and 21:00 Tehran time"
        ),
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--once",
        action="store_true",
        help="perform one immediate website check and exit",
    )
    action.add_argument(
        "--test-telegram",
        action="store_true",
        help="send one Telegram test message without opening the website",
    )
    return parser.parse_args()


def run_check_safely(totp, always_report=False):
    try:
        result, logout_error = check_once(totp)
    except PlaywrightTimeoutError:
        description = "The portal timed out."
    except CheckerError as error:
        description = str(error)
    except PlaywrightError:
        description = "Browser automation failed while starting or stopping Playwright."
    except Exception as error:
        description = f"Unexpected failure ({type(error).__name__})."
    else:
        available = result["available"]
        checked_cells = result["checked_cells"]
        residence_totals = result["residence_totals"]

        # Report the table result before a possible logout warning so cleanup
        # failure can never hide real availability.
        if available:
            message = alert_text(
                available,
                residence_totals,
                result["watched_residences"],
            )
            log(message)
            send_notification(message, "availability alert")
        elif always_report:
            message = no_availability_text(checked_cells, residence_totals)
            log(message)
            send_notification(message, "on-demand check result")
        else:
            log(f"No availability found ({checked_cells} cells checked).")

        if logout_error is None:
            return True
        description = str(logout_error)

    log(f"Check failed: {description}")
    send_error_notification(description)
    return False


def send_residence_manager(catalog, watched, notice=""):
    return send_notification(
        residence_list_text(catalog, watched, notice),
        "residence controls",
        reply_markup=residence_menu_keyboard(catalog, watched),
    )


def edit_or_send_residence_manager(
    chat_id,
    message_id,
    catalog,
    watched,
    notice="",
):
    message = residence_list_text(catalog, watched, notice)
    keyboard = residence_menu_keyboard(catalog, watched)
    if edit_telegram_message(
        chat_id,
        message_id,
        message,
        keyboard,
    ):
        return True
    return send_residence_manager(
        catalog,
        watched,
        "♻️ The previous panel could not be updated. Use this new panel.",
    )


def parse_residence_callback(data):
    refresh_match = re.fullmatch(r"rh\|([0-9a-f]{10})\|refresh", data)
    if refresh_match:
        return {
            "revision": refresh_match.group(1),
            "target": "refresh",
            "should_watch": None,
        }

    selection_match = re.fullmatch(
        r"rh\|([0-9a-f]{10})\|(all|[0-9]+)\|([01])",
        data,
    )
    if not selection_match:
        return None
    target = selection_match.group(2)
    if target != "all":
        target = int(target)
    return {
        "revision": selection_match.group(1),
        "target": target,
        "should_watch": selection_match.group(3) == "1",
    }


def load_residence_catalog_for_action(totp):
    catalog, watched = residence_preferences()
    if catalog:
        return catalog, watched, False

    send_notification(
        "\n".join(
            [
                "🔄 LOADING RESIDENCE LIST",
                "🏠 Polimi Residence Checker",
                "",
                "The residence list has not been saved yet.",
                "I am running one read-only SOL check to load it.",
            ]
        ),
        "residence discovery status",
    )
    run_check_safely(totp)
    catalog, watched = residence_preferences()
    if not catalog:
        send_notification(
            "\n".join(
                [
                    "⚠️ RESIDENCE LIST UNAVAILABLE",
                    "🏠 Polimi Residence Checker",
                    "",
                    "The current list could not be loaded from SOL.",
                    "Review the failure alert or PM2 logs, then try again.",
                ]
            ),
            "residence discovery failure",
        )
    return catalog, watched, True


def save_current_telegram_ui_version():
    try:
        save_telegram_ui_version(TELEGRAM_UI_VERSION)
    except OSError:
        log("Telegram control version could not be saved.")


def ensure_telegram_controls():
    if telegram_ui_version() >= TELEGRAM_UI_VERSION:
        return
    if send_notification(
        "\n".join(
            [
                "🤖 POLIMI RESIDENCE CHECKER",
                "",
                "✅ CONTROLS READY",
                "Use the buttons at the bottom of this chat.",
            ]
        ),
        "button setup",
    ):
        save_current_telegram_ui_version()


def handle_telegram_action(action, totp):
    if action["kind"] == "message":
        message_action = action["action"]
        if message_action == "help":
            if send_notification(telegram_help_text(), "help response"):
                save_current_telegram_ui_version()
            return False

        if message_action == "residences":
            catalog, watched, ran_check = load_residence_catalog_for_action(totp)
            if catalog:
                send_residence_manager(catalog, watched)
            return ran_check

        send_notification(
            "\n".join(
                [
                    "🔎 CHECK STARTED",
                    "🏠 Polimi Residence Checker",
                    "",
                    "I am checking SOL now.",
                    "I will send the result after reading the availability table.",
                ]
            ),
            "on-demand check status",
        )
        run_check_safely(totp, always_report=True)
        return True

    catalog, watched, ran_check = load_residence_catalog_for_action(totp)
    if not catalog:
        return ran_check

    callback = parse_residence_callback(action["data"])
    current_revision = residence_menu_revision(catalog)
    if callback is None or callback["revision"] != current_revision:
        edit_or_send_residence_manager(
            action["chat_id"],
            action["message_id"],
            catalog,
            watched,
            "♻️ This panel was outdated and has been refreshed. No changes were made.",
        )
        return ran_check

    target = callback["target"]
    if target == "refresh":
        refresh_succeeded = run_check_safely(totp)
        catalog, watched = residence_preferences()
        if catalog:
            notice = (
                "✅ Residence list refreshed from SOL."
                if refresh_succeeded
                else "⚠️ Refresh failed; showing the last saved list."
            )
            edit_or_send_residence_manager(
                action["chat_id"],
                action["message_id"],
                catalog,
                watched,
                notice,
            )
        return True

    if target != "all" and target >= len(catalog):
        edit_or_send_residence_manager(
            action["chat_id"],
            action["message_id"],
            catalog,
            watched,
            "♻️ The residence list changed and was refreshed. No changes were made.",
        )
        return ran_check

    try:
        if target == "all":
            catalog, watched = update_all_residence_watches(
                should_watch=callback["should_watch"]
            )
        else:
            catalog, watched = update_residence_watch(
                [target + 1],
                should_watch=callback["should_watch"],
            )
    except OSError:
        send_notification(
            "\n".join(
                [
                    "⚠️ PREFERENCES NOT SAVED",
                    "🏠 Polimi Residence Checker",
                    "",
                    "Monitoring preferences could not be saved.",
                    "Review the PM2 logs and try again.",
                ]
            ),
            "watch-list save failure",
        )
        return ran_check

    edit_or_send_residence_manager(
        action["chat_id"],
        action["message_id"],
        catalog,
        watched,
        "✅ Monitoring preferences saved.",
    )
    return ran_check


def next_interval_start(
    start_hour,
    interval_hours,
    include_current_minute=False,
    now=None,
):
    now = now or datetime.now(TEHRAN_TIMEZONE)
    anchor = now.replace(
        hour=start_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    interval = timedelta(hours=interval_hours)
    elapsed_intervals = math.floor(
        (now - anchor).total_seconds() / interval.total_seconds()
    )
    previous_target = anchor + elapsed_intervals * interval

    if (
        include_current_minute
        and previous_target.replace(second=0, microsecond=0)
        == now.replace(second=0, microsecond=0)
    ):
        return now

    return previous_target + interval


def log_first_interval_check(target, interval_hours):
    log(
        "First interval check at "
        f"{target.isoformat(timespec='minutes')} (Asia/Tehran); "
        f"then every {interval_hours:g} hours."
    )


def next_interval_deadline(scheduled_deadline, interval_seconds, now=None):
    now = time.monotonic() if now is None else now
    next_deadline = scheduled_deadline + interval_seconds
    if next_deadline <= now:
        missed_intervals = math.floor(
            (now - next_deadline) / interval_seconds
        ) + 1
        next_deadline += missed_intervals * interval_seconds
    return next_deadline


def monotonic_deadline_for_wall_target(target, wall_now=None, monotonic_now=None):
    wall_now = wall_now or datetime.now(TEHRAN_TIMEZONE)
    monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
    return monotonic_now + (target - wall_now).total_seconds()


def log_next_interval_check(deadline):
    wait_seconds = max(0, deadline - time.monotonic())
    target = datetime.now(TEHRAN_TIMEZONE) + timedelta(seconds=wait_seconds)
    log(
        "Next interval check at "
        f"{target.isoformat(timespec='minutes')} (Asia/Tehran)."
    )


def next_tehran_slot(include_current_minute=False, now=None):
    now = now or datetime.now(TEHRAN_TIMEZONE)
    if (
        include_current_minute
        and now.hour in TEHRAN_CHECK_HOURS
        and now.minute == 0
    ):
        return now

    for hour in TEHRAN_CHECK_HOURS:
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate

    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(
        hour=TEHRAN_CHECK_HOURS[0], minute=0, second=0, microsecond=0
    )


def log_next_tehran_check(target):
    log(
        "Next check at "
        f"{target.isoformat(timespec='minutes')} (Asia/Tehran)."
    )


def run_service(arguments, totp, interval_hours, start_hour):
    if telegram_configured():
        ensure_telegram_controls()
        start_telegram_action_listener()

    interval_seconds = interval_hours * 60 * 60
    interval_start_target = None
    tehran_target = None
    if arguments.mode == "interval":
        if start_hour is None:
            interval_deadline = time.monotonic()
        else:
            interval_deadline = None
            interval_start_target = next_interval_start(
                start_hour,
                interval_hours,
                include_current_minute=True,
            )
            log_first_interval_check(
                interval_start_target,
                interval_hours,
            )
    else:
        interval_deadline = None
        tehran_target = next_tehran_slot(include_current_minute=True)
        log_next_tehran_check(tehran_target)

    try:
        while True:
            if arguments.mode == "interval":
                if interval_deadline is None:
                    wait_seconds = max(
                        0,
                        (
                            interval_start_target
                            - datetime.now(TEHRAN_TIMEZONE)
                        ).total_seconds(),
                    )
                else:
                    wait_seconds = max(
                        0,
                        interval_deadline - time.monotonic(),
                    )
            else:
                wait_seconds = max(
                    0,
                    (
                        tehran_target - datetime.now(TEHRAN_TIMEZONE)
                    ).total_seconds(),
                )

            if wait_seconds <= 0:
                if arguments.mode == "interval":
                    scheduled_deadline = (
                        interval_deadline
                        if interval_deadline is not None
                        else monotonic_deadline_for_wall_target(
                            interval_start_target
                        )
                    )
                run_check_safely(totp)
                if arguments.mode == "interval":
                    interval_deadline = next_interval_deadline(
                        scheduled_deadline,
                        interval_seconds,
                    )
                    interval_start_target = None
                    log_next_interval_check(interval_deadline)
                else:
                    tehran_target = next_tehran_slot()
                    log_next_tehran_check(tehran_target)
                continue

            waiting_on_wall_clock = (
                arguments.mode == "tehran"
                or (
                    arguments.mode == "interval"
                    and interval_deadline is None
                )
            )
            queue_timeout = (
                min(wait_seconds, 60)
                if waiting_on_wall_clock
                else wait_seconds
            )
            try:
                update_id, action = TELEGRAM_ACTION_QUEUE.get(
                    timeout=queue_timeout
                )
            except Empty:
                if (
                    arguments.mode == "interval"
                    and interval_deadline is None
                    and datetime.now(TEHRAN_TIMEZONE)
                    < interval_start_target
                ):
                    continue
                if (
                    arguments.mode == "tehran"
                    and datetime.now(TEHRAN_TIMEZONE) < tehran_target
                ):
                    continue
                if arguments.mode == "interval":
                    scheduled_deadline = (
                        interval_deadline
                        if interval_deadline is not None
                        else monotonic_deadline_for_wall_target(
                            interval_start_target
                        )
                    )
                run_check_safely(totp)
                if arguments.mode == "interval":
                    interval_deadline = next_interval_deadline(
                        scheduled_deadline,
                        interval_seconds,
                    )
                    interval_start_target = None
                    log_next_interval_check(interval_deadline)
                else:
                    tehran_target = next_tehran_slot()
                    log_next_tehran_check(tehran_target)
                continue

            if action is None:
                mark_telegram_update_processed(update_id)
                continue

            sol_check_key = action.get("sol_check_key")
            action_started_wall = datetime.now(TEHRAN_TIMEZONE)
            action_started_monotonic = time.monotonic()
            try:
                try:
                    completed_check = handle_telegram_action(action, totp)
                except Exception as error:
                    log(
                        "Telegram button action failed safely "
                        f"({type(error).__name__})."
                    )
                    send_error_notification(
                        "A Telegram button action could not be completed."
                    )
                    completed_check = False
            finally:
                if sol_check_key:
                    release_sol_check_action(sol_check_key)

            mark_telegram_update_processed(update_id)

            if not completed_check:
                continue

            # A manual/discovery check attempt that crosses a scheduled
            # deadline satisfies that slot, matching the normal behavior where
            # even a failed scheduled attempt waits until the next slot.
            if arguments.mode == "interval":
                crossed_start_target = (
                    interval_deadline is None
                    and datetime.now(TEHRAN_TIMEZONE)
                    >= interval_start_target
                )
                crossed_interval_deadline = (
                    interval_deadline is not None
                    and time.monotonic() >= interval_deadline
                )
                if crossed_start_target or crossed_interval_deadline:
                    scheduled_deadline = (
                        interval_deadline
                        if interval_deadline is not None
                        else monotonic_deadline_for_wall_target(
                            interval_start_target,
                            wall_now=action_started_wall,
                            monotonic_now=action_started_monotonic,
                        )
                    )
                    interval_deadline = next_interval_deadline(
                        scheduled_deadline,
                        interval_seconds,
                    )
                    interval_start_target = None
                    log(
                        "The on-demand check satisfied the scheduled run."
                    )
                    log_next_interval_check(interval_deadline)
            elif (
                arguments.mode == "tehran"
                and datetime.now(TEHRAN_TIMEZONE) >= tehran_target
            ):
                tehran_target = next_tehran_slot()
                log("The on-demand check satisfied the scheduled Tehran slot.")
                log_next_tehran_check(tehran_target)
    except KeyboardInterrupt:
        log("Checker stopped.")
        return 0


def test_telegram():
    if not telegram_configured():
        log("Telegram test failed: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return 1

    message = "\n".join(
        [
            "✅ TELEGRAM TEST PASSED",
            "🏠 Polimi Residence Checker",
            "",
            "Telegram delivery is working correctly.",
            f"🎓 Academic year: {ACADEMIC_YEAR}",
            f"🕒 Sent: {notification_timestamp()}",
            "",
            NOTIFICATION_DIVIDER,
            "",
            "🌐 SOL was not opened for this test.",
        ]
    )
    if send_telegram(message, main_menu_keyboard()):
        save_current_telegram_ui_version()
        log("Telegram test message sent.")
        return 0
    log("Telegram test failed; check its settings and the network.")
    return 1


def main():
    arguments = parse_arguments()

    if arguments.test_telegram:
        return test_telegram()

    try:
        totp, interval_hours, start_hour = configuration(
            use_start_hour=(
                arguments.mode == "interval"
                and not arguments.once
            )
        )
    except CheckerError as error:
        description = f"Configuration error: {error}"
        log(description)
        send_error_notification(description)
        return 2

    if arguments.once:
        return 0 if run_check_safely(totp) else 1

    notification = "enabled" if telegram_configured() else "not configured"
    log(f"Checker started; mode={arguments.mode}; Telegram={notification}.")
    return run_service(
        arguments,
        totp,
        interval_hours,
        start_hour,
    )


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as error:
        description = f"Fatal failure ({type(error).__name__})."
        log(description)
        send_error_notification(description)
        exit_code = 1
    raise SystemExit(exit_code)
