#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

NUMBER_PATTERN = r"[-+]?\d+(?:\.\d+)?"
DEFAULT_LINKS_FILE = "mettl-links.json"
DEFAULT_CREDENTIALS_FILE = "mettl-credentials.json"
DEFAULT_STATE_FILE = "mettl-state.json"

DEFAULT_USERNAME_SELECTORS = [
    "input[placeholder*='Email' i]",
    "input[aria-label*='Email' i]",
    "input[name*='email' i]",
    "input[id*='email' i]",
    "input[name='username']",
    "input[name='email']",
    "input[id*='user']",
    "input[id*='email']",
    "input[type='email']",
    "input[type='text']",
]
DEFAULT_PASSWORD_SELECTORS = [
    "input[placeholder*='Password' i]",
    "input[aria-label*='Password' i]",
    "input[name*='password' i]",
    "input[type='password']",
    "input[name='password']",
]
DEFAULT_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Login Now')",
    "button:has-text('Login')",
    "button:has-text('Log in')",
    "button:has-text('Log In')",
    "button:has-text('Sign in')",
    "button:has-text('Continue')",
]


@dataclass
class Metrics:
    marks_scored: Optional[float] = None
    marks_out_of: Optional[float] = None
    percentage: Optional[float] = None
    percentile: Optional[float] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_json_file(path: Path, default: Any = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(f"Missing JSON file: {path}")

    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_file(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def load_links(path: Path) -> dict[str, str]:
    raw = load_json_file(path)
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{path} must contain a JSON object of subject -> URL mappings.")

    links: dict[str, str] = {}
    for subject, url in raw.items():
        if not isinstance(subject, str) or not isinstance(url, str):
            raise ValueError(f"Invalid subject/url in {path}: {subject!r} -> {url!r}")
        subject = subject.strip()
        url = url.strip()
        if not subject or not url:
            raise ValueError(f"Subject and URL cannot be empty in {path}.")
        links[subject] = url

    return links


def resolve_selectors(credentials: dict[str, Any], key: str, defaults: list[str]) -> list[str]:
    login_cfg = credentials.get("login")
    if isinstance(login_cfg, dict):
        raw = login_cfg.get(key)
        if isinstance(raw, list) and all(isinstance(item, str) for item in raw) and raw:
            return raw
    return defaults


async def first_visible_locator(page: Page, selectors: list[str], timeout_ms: int = 1500) -> Optional[Locator]:
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count() == 0:
            continue
        candidate = locator.first
        try:
            await candidate.wait_for(state="visible", timeout=timeout_ms)
            return candidate
        except PlaywrightTimeoutError:
            continue
    return None


async def maybe_login(page: Page, credentials: dict[str, Any]) -> bool:
    username = credentials.get("email") or credentials.get("username")
    password = credentials.get("password")
    if not username or not password:
        raise ValueError("Credentials JSON must contain 'username' (or 'email') and 'password'.")

    username_selectors = resolve_selectors(credentials, "username_selectors", DEFAULT_USERNAME_SELECTORS)
    password_selectors = resolve_selectors(credentials, "password_selectors", DEFAULT_PASSWORD_SELECTORS)
    submit_selectors = resolve_selectors(credentials, "submit_selectors", DEFAULT_SUBMIT_SELECTORS)

    password_input = await first_visible_locator(page, password_selectors)
    if password_input is None:
        return False

    username_input = await first_visible_locator(page, username_selectors)
    if username_input is None:
        raise RuntimeError("Login page detected, but a username/email input was not found.")

    await username_input.fill(str(username))
    await password_input.fill(str(password))

    submit_button = await first_visible_locator(page, submit_selectors, timeout_ms=1200)
    if submit_button is not None:
        await submit_button.click()
    else:
        await password_input.press("Enter")

    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        # Some sites keep background requests open; continue and try scraping.
        pass
    await page.wait_for_timeout(1500)
    return True


def extract_metrics_from_text(page_text: str) -> Metrics:
    text = normalize_space(page_text)

    marks_match = re.search(
        rf"({NUMBER_PATTERN})\s*Marks\s*Scored\s*out\s*of\s*({NUMBER_PATTERN})",
        text,
        flags=re.IGNORECASE,
    )
    pair_match = re.search(
        rf"({NUMBER_PATTERN})\s*%\s*({NUMBER_PATTERN})\s*percentile",
        text,
        flags=re.IGNORECASE,
    )

    marks_scored = parse_optional_float(marks_match.group(1)) if marks_match else None
    marks_out_of = parse_optional_float(marks_match.group(2)) if marks_match else None

    if pair_match:
        percentage = parse_optional_float(pair_match.group(1))
        percentile = parse_optional_float(pair_match.group(2))
    else:
        percentage_match = re.search(rf"({NUMBER_PATTERN})\s*%", text)
        percentile_match = re.search(rf"({NUMBER_PATTERN})\s*percentile", text, flags=re.IGNORECASE)
        percentage = parse_optional_float(percentage_match.group(1)) if percentage_match else None
        percentile = parse_optional_float(percentile_match.group(1)) if percentile_match else None

    return Metrics(
        marks_scored=marks_scored,
        marks_out_of=marks_out_of,
        percentage=percentage,
        percentile=percentile,
    )


async def scrape_metrics(page: Page) -> Metrics:
    try:
        await page.get_by_text("Overall Summary", exact=False).first.wait_for(timeout=15000)
    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(1000)
    body_text = await page.inner_text("body")
    metrics = extract_metrics_from_text(body_text)

    missing = []
    if metrics.marks_scored is None:
        missing.append("marks_scored")
    if metrics.percentage is None:
        missing.append("percentage")
    if metrics.percentile is None:
        missing.append("percentile")
    if missing:
        raise RuntimeError(f"Could not scrape required metrics: {', '.join(missing)}")

    return metrics


def almost_equal(a: Optional[float], b: Optional[float], tolerance: float = 1e-6) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= tolerance


def metrics_changed(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    fields = ("marks_scored", "marks_out_of", "percentage", "percentile")
    for field in fields:
        prev_val = parse_optional_float(previous.get(field))
        curr_val = parse_optional_float(current.get(field))
        if not almost_equal(prev_val, curr_val):
            return True
    return False


def format_value(value: Optional[float], suffix: str = "") -> str:
    if value is None:
        return "N/A"
    if abs(value - round(value)) <= 1e-9:
        rendered = str(int(round(value)))
    else:
        rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{rendered}{suffix}"


def format_diff(current: Optional[float], previous: Optional[float], suffix: str = "") -> str:
    if current is None or previous is None:
        return "N/A"
    delta = current - previous
    prefix = "+" if delta >= 0 else ""
    if abs(delta - round(delta)) <= 1e-9:
        rendered = str(int(round(delta)))
    else:
        rendered = f"{delta:.2f}".rstrip("0").rstrip(".")
    return f"{prefix}{rendered}{suffix}"


def build_message(
    subject: str,
    link: str,
    previous: dict[str, Any],
    current: dict[str, Any],
    scraped_at_utc: str,
) -> str:
    prev_marks = parse_optional_float(previous.get("marks_scored"))
    prev_marks_out_of = parse_optional_float(previous.get("marks_out_of"))
    prev_percentage = parse_optional_float(previous.get("percentage"))
    prev_percentile = parse_optional_float(previous.get("percentile"))

    curr_marks = parse_optional_float(current.get("marks_scored"))
    curr_marks_out_of = parse_optional_float(current.get("marks_out_of"))
    curr_percentage = parse_optional_float(current.get("percentage"))
    curr_percentile = parse_optional_float(current.get("percentile"))

    lines = [
        "Mettl report update detected",
        f"Subject: {subject}",
        f"Link: {link}",
        "",
        "Current",
        f"Marks: {format_value(curr_marks)}/{format_value(curr_marks_out_of)}",
        f"Percentage: {format_value(curr_percentage, '%')}",
        f"Percentile: {format_value(curr_percentile)}",
        "",
        "Previous",
        f"Marks: {format_value(prev_marks)}/{format_value(prev_marks_out_of)}",
        f"Percentage: {format_value(prev_percentage, '%')}",
        f"Percentile: {format_value(prev_percentile)}",
        "",
        "Difference",
        f"Marks: {format_diff(curr_marks, prev_marks)}",
        f"Percentage: {format_diff(curr_percentage, prev_percentage, '%')}",
        f"Percentile: {format_diff(curr_percentile, prev_percentile)}",
        "",
        f"Checked at (UTC): {scraped_at_utc}",
    ]
    return "\n".join(lines)


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API returned error: {body}")


async def scrape_subject(page: Page, subject: str, url: str, credentials: dict[str, Any]) -> Metrics:
    logging.info("Checking %s", subject)
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(1200)

    logged_in = await maybe_login(page, credentials)
    if logged_in:
        logging.info("Login performed for %s", subject)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1200)

    return await scrape_metrics(page)


async def run_cycle(links_file: Path, credentials_file: Path, state_file: Path, headless: bool) -> int:
    links = load_links(links_file)
    credentials = load_json_file(credentials_file)
    if not isinstance(credentials, dict):
        raise ValueError(f"{credentials_file} must contain a JSON object.")

    bot_token = credentials.get("telegram_bot_token")
    chat_id = credentials.get("telegram_chat_id")
    if not bot_token or not chat_id:
        raise ValueError("Credentials JSON must include 'telegram_bot_token' and 'telegram_chat_id'.")

    state = load_json_file(state_file, default={})
    if not isinstance(state, dict):
        state = {}

    updates: list[str] = []
    attempt_time = utc_now_iso()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        for subject, url in links.items():
            previous_entry = state.get(subject)
            if not isinstance(previous_entry, dict):
                previous_entry = {}
            previous_metrics = previous_entry.get("metrics")
            if not isinstance(previous_metrics, dict):
                previous_metrics = None

            try:
                current_metrics = await scrape_subject(page, subject, url, credentials)
                current_metrics_dict = asdict(current_metrics)

                state[subject] = {
                    "link": url,
                    "metrics": current_metrics_dict,
                    "last_success_at": attempt_time,
                    "last_error": None,
                    "last_error_at": None,
                }

                if previous_metrics and metrics_changed(previous_metrics, current_metrics_dict):
                    updates.append(build_message(subject, url, previous_metrics, current_metrics_dict, attempt_time))
                    logging.info("Change detected for %s", subject)
                elif previous_metrics is None:
                    logging.info("Initial baseline saved for %s", subject)
                else:
                    logging.info("No metric changes for %s", subject)
            except Exception as exc:
                logging.exception("Failed to scrape %s", subject)
                state[subject] = {
                    "link": url,
                    "metrics": previous_metrics,
                    "last_success_at": previous_entry.get("last_success_at"),
                    "last_error": str(exc),
                    "last_error_at": attempt_time,
                }

        await context.close()
        await browser.close()

    write_json_file(state_file, state)

    for message in updates:
        send_telegram_message(str(bot_token), str(chat_id), message)
        logging.info("Telegram message sent")

    return len(updates)


async def monitor(args: argparse.Namespace) -> None:
    links_file = Path(args.links_file)
    credentials_file = Path(args.credentials_file)
    state_file = Path(args.state_file)

    updates = await run_cycle(
        links_file=links_file,
        credentials_file=credentials_file,
        state_file=state_file,
        headless=not args.headed,
    )
    logging.info("Cycle complete. Telegram updates sent: %d", updates)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Monitor Mettl report links, scrape marks/percentage/percentile, "
            "and send Telegram alerts on score changes."
        )
    )
    parser.add_argument("--links-file", default=DEFAULT_LINKS_FILE, help="Path to subject->URL JSON file.")
    parser.add_argument(
        "--credentials-file",
        default=DEFAULT_CREDENTIALS_FILE,
        help="Path to login + Telegram credentials JSON file.",
    )
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_FILE,
        help="Path to local state JSON file used for previous scrape comparison.",
    )
    parser.add_argument("--headed", action="store_true", help="Run browser with visible UI for debugging.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(monitor(args))


if __name__ == "__main__":
    main()
