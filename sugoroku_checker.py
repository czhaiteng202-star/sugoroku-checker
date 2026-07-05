# -*- coding: utf-8 -*-
"""
Sugoroku-goya availability checker v6

Default target:
  2026-08-19 / Sugoroku-goya / General room

Important change from v2:
  v3 first checks the month that is already displayed.
  If 2026-08 is already visible, it reads that calendar immediately.
  If another month is visible, it tries harder to click the website's next-month controls
  before falling back to manual navigation.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ENTRY_URL = "https://www.sugorokugoya.com/reservation/reservehut"
CALENDAR_URL = "https://www.sugorokugoya.com/reservation/selectdate?type=1"
TARGET_HUT = "双六小屋"
TARGET_ROOM = "一般室"
DEFAULT_YEAR = 2026
DEFAULT_MONTH = 8
DEFAULT_DAY = 19
DEFAULT_NIGHTS = 1

RESULT_CSV = "result.csv"
DEBUG_PREFIX = "debug_sugoroku"
LINE_CONFIG_JSON = "line_config.json"
LINE_STATE_JSON = "line_notify_state.json"

ROOM_STOP_WORDS = [
    "わさび平小屋", "鏡平山荘", "双六小屋", "黒部五郎小舎",
    "個室(2名)", "個室(2～3名)", "個室(2〜3名)",
    "個室(3～4名)", "個室(3〜4名)", "個室(4～5名)", "個室(4〜5名)",
    "泊数", "男性", "女性", "高校生", "中学生", "小学生", "合計", "保存",
]

@dataclass
class CheckResult:
    checked_at: str
    year: int
    month: int
    day: int
    nights: int
    hut: str
    room: str
    month_found: bool
    status_mark: str
    status_text: str
    reservable: Optional[bool]
    row_text: str
    url: str


@dataclass
class MonthDayStatus:
    day: int
    status_mark: str
    status_text: str
    reservable: Optional[bool]


@dataclass
class MonthSummary:
    checked_at: str
    year: int
    month: int
    hut: str
    room: str
    month_found: bool
    days: list[MonthDayStatus]
    row_text: str
    url: str


def compact_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_error_page(text: str) -> bool:
    t = compact_spaces(text)
    error_words = [
        "エラーが発生", "エラー", "Internal Error", "An error occurred",
        "ただいまアクセス", "ページが見つかりません",
    ]
    return any(w in t for w in error_words) and TARGET_HUT not in t


def expected_month_text(year: int, month: int) -> str:
    return f"{year}年 {month}月"


def month_is_visible(text: str, year: int, month: int) -> bool:
    """Return True when the target year/month appears in the visible page text.

    The site usually displays `2026年 8月`, but spacing can differ.
    This function intentionally accepts several common formats.
    """
    t = compact_spaces(text)
    patterns = [
        rf"{year}\s*年\s*{month}\s*月",
        rf"{year}[-/.]0?{month}(?!\d)",
    ]
    return any(re.search(pat, t) for pat in patterns)


def visible_months(text: str) -> list[tuple[int, int]]:
    """Extract visible year/month headings from the page text."""
    t = compact_spaces(text)
    months: list[tuple[int, int]] = []
    for y, m in re.findall(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", t):
        try:
            item = (int(y), int(m))
            if 1 <= item[1] <= 12 and item not in months:
                months.append(item)
        except Exception:
            pass
    return months


def month_distance(current: tuple[int, int], target: tuple[int, int]) -> int:
    return (target[0] - current[0]) * 12 + (target[1] - current[1])


def write_debug(page, body_text: str, error_text: str = "") -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=f"{DEBUG_PREFIX}_{stamp}.png", full_page=True)
    except Exception:
        pass
    try:
        with open(f"{DEBUG_PREFIX}_{stamp}.txt", "w", encoding="utf-8") as f:
            if error_text:
                f.write("ERROR:\n")
                f.write(error_text)
                f.write("\n\n")
            f.write("BODY TEXT:\n")
            f.write(body_text or "")
            f.write("\n\nCLICKABLES:\n")
            try:
                clickables = page.locator("a, button, input[type=button], input[type=submit]").evaluate_all(
                    "els => els.map(e => ({text:(e.innerText||e.value||e.getAttribute('aria-label')||e.title||'').trim(), href:e.href||'', cls:e.className||'', id:e.id||''}))"
                )
                for c in clickables:
                    f.write(str(c) + "\n")
            except Exception as e:
                f.write(f"Could not read clickables: {e}\n")
    except Exception:
        pass


def open_calendar_safely(page) -> str:
    """Open the calendar through the official reservation flow to avoid unsupported URL errors."""
    page.goto(ENTRY_URL, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(1000)
    text = page.locator("body").inner_text(timeout=10000)

    # Prefer clicking the official link if available.
    try:
        link = page.get_by_text(re.compile("WEBで予約|空き状況確認"))
        if link.count() > 0:
            link.first.click(timeout=10000)
            page.wait_for_load_state("networkidle", timeout=60000)
            page.wait_for_timeout(1000)
            return page.locator("body").inner_text(timeout=10000)
    except Exception:
        pass

    # Fallback: official calendar URL without unsupported year/month params.
    page.goto(CALENDAR_URL, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(1000)
    return page.locator("body").inner_text(timeout=10000)


def _safe_page_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=10000)
    except Exception:
        return ""


def _read_click_candidates(page) -> list[dict]:
    """Return visible clickable elements with their attributes and screen positions."""
    script = r"""
    () => {
      const nodes = Array.from(document.querySelectorAll(
        'a, button, input[type=button], input[type=submit], [role=button], [onclick], .next, .prev'
      ));
      return nodes.map((el, i) => {
        const r = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        const text = ((el.innerText || el.value || el.textContent || '') + '').trim();
        return {
          index: i,
          tag: el.tagName,
          text,
          aria: el.getAttribute('aria-label') || '',
          title: el.getAttribute('title') || '',
          cls: el.className ? String(el.className) : '',
          id: el.id || '',
          href: el.href || el.getAttribute('href') || '',
          onclick: el.getAttribute('onclick') || '',
          x: r.x, y: r.y, width: r.width, height: r.height,
          visible: !!(r.width && r.height && style.visibility !== 'hidden' && style.display !== 'none')
        };
      });
    }
    """
    try:
        return page.evaluate(script)
    except Exception:
        return []


def _score_nav_candidate(c: dict, direction: int) -> int:
    if not c.get("visible"):
        return -9999
    text = " ".join(str(c.get(k, "")) for k in ["text", "aria", "title", "cls", "id", "href", "onclick"]).lower()
    if not text.strip():
        text = ""

    banned = ["home", "ログイン", "会員登録", "予約確認", "キャンセル", "privacy", "terms", "ご利用規約", "公式サイト"]
    if any(b.lower() in text for b in banned):
        return -9999

    next_words = ["次", "次月", "翌月", "来月", "next", "forward", "right", "chevron-right", "arrow-right", ">", "›", "»", "＞", "→", "▶"]
    prev_words = ["前", "前月", "先月", "previous", "prev", "back", "left", "chevron-left", "arrow-left", "<", "‹", "«", "＜", "←", "◀"]
    positive = next_words if direction > 0 else prev_words
    negative = prev_words if direction > 0 else next_words

    score = 0
    for w in positive:
        if w.lower() in text:
            score += 60
    for w in negative:
        if w.lower() in text:
            score -= 80

    # Month navigation buttons are usually near the top of the calendar.
    y = float(c.get("y") or 0)
    x = float(c.get("x") or 0)
    width = float(c.get("width") or 0)
    if 150 <= y <= 550:
        score += 10
    if direction > 0:
        score += int((x + width) / 300)
    else:
        score += int((1400 - x) / 300)

    # Very large elements are often wrappers, not useful buttons.
    if float(c.get("width") or 0) > 500 or float(c.get("height") or 0) > 200:
        score -= 40
    return score


def _month_heading_rects(page) -> list[dict]:
    """Find rectangles of elements/text nodes that look like a month heading."""
    script = r"""
    () => {
      const out = [];
      const re = /20\d{2}\s*年\s*\d{1,2}\s*月/;
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
      while (walker.nextNode()) {
        const el = walker.currentNode;
        const text = (el.innerText || el.textContent || '').trim();
        if (!text || !re.test(text)) continue;
        const r = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        if (!r.width || !r.height || style.display === 'none' || style.visibility === 'hidden') continue;
        // Prefer smaller elements; ignore the whole body if possible.
        out.push({text, x:r.x, y:r.y, width:r.width, height:r.height, area:r.width*r.height});
      }
      return out.sort((a,b) => a.area - b.area).slice(0, 8);
    }
    """
    try:
        return page.evaluate(script)
    except Exception:
        return []


def _wait_after_nav(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    page.wait_for_timeout(1000)


def try_click_month_navigation(page, direction: int, target_year: int, target_month: int) -> bool:
    """Try to click next/previous month. Returns True if page text changed or target appeared."""
    before = compact_spaces(_safe_page_text(page))

    # 1) Try best-scored clickable elements first.
    candidates = _read_click_candidates(page)
    scored = sorted(
        [( _score_nav_candidate(c, direction), c) for c in candidates],
        key=lambda item: item[0],
        reverse=True,
    )
    tried = 0
    for score, c in scored[:10]:
        if score < 20:
            continue
        x = float(c.get("x") or 0) + float(c.get("width") or 0) / 2
        y = float(c.get("y") or 0) + float(c.get("height") or 0) / 2
        try:
            page.mouse.click(x, y)
            _wait_after_nav(page)
            after = compact_spaces(_safe_page_text(page))
            if month_is_visible(after, target_year, target_month) or after != before:
                return True
        except Exception:
            pass
        tried += 1
        if tried >= 5:
            break

    # 2) Try text/CSS selectors as a fallback.
    labels = ["次月", "翌月", "来月", "次", "Next", "NEXT", ">", "›", "»", "＞", "→"] if direction > 0 else ["前月", "先月", "前", "Prev", "PREV", "Previous", "<", "‹", "«", "＜", "←"]
    for label in labels:
        try:
            loc = page.get_by_text(label, exact=True)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=5000)
                _wait_after_nav(page)
                after = compact_spaces(_safe_page_text(page))
                if month_is_visible(after, target_year, target_month) or after != before:
                    return True
        except Exception:
            pass

    # 3) Last resort: click around the visible month heading.
    # If the first page already shows July, the right-side arrow is often near this area.
    rects = _month_heading_rects(page)
    for r in rects:
        y = float(r.get("y") or 0) + float(r.get("height") or 0) / 2
        if direction > 0:
            xs = [float(r.get("x") or 0) + float(r.get("width") or 0) + 40, 1230, 1320]
        else:
            xs = [max(10, float(r.get("x") or 0) - 40), 80, 160]
        for x in xs:
            try:
                page.mouse.click(x, y)
                _wait_after_nav(page)
                after = compact_spaces(_safe_page_text(page))
                if month_is_visible(after, target_year, target_month) or after != before:
                    return True
            except Exception:
                pass

    return False


def navigate_to_month(page, year: int, month: int, headless: bool) -> tuple[str, bool]:
    body_text = open_calendar_safely(page)

    if is_error_page(body_text):
        # Retry once through the safe entry URL.
        body_text = open_calendar_safely(page)

    # Important: if the browser already shows the target month, do not click anything.
    if month_is_visible(body_text, year, month):
        return body_text, True

    target = (year, month)

    # Try automatic month navigation.
    # The first page is often 2026年7月; in that case this should click next once.
    for _ in range(12):
        months = visible_months(body_text)
        if months:
            # Pick the first visible calendar heading.
            distance = month_distance(months[0], target)
            if distance == 0:
                return body_text, True
            direction = 1 if distance > 0 else -1
        else:
            # If the heading cannot be parsed, try next month because the target is usually August.
            direction = 1

        if not try_click_month_navigation(page, direction, year, month):
            break

        body_text = _safe_page_text(page)
        if is_error_page(body_text):
            break
        if month_is_visible(body_text, year, month):
            return body_text, True

    # In visible mode, allow manual navigation only after automatic navigation fails.
    body_text = _safe_page_text(page)
    if not headless:
        print()
        print("Could not automatically move to the target month.")
        print(f"Please use the opened browser to show: {year}-{month:02d}")
        print("If the opened browser is already showing the target month, just press Enter here.")
        print("After the calendar for the target month is visible, return to this black window and press Enter.")
        print("If the website shows an error, go back to the reservation page and open the calendar again.")
        try:
            input("Press Enter after the target month is visible... ")
            page.wait_for_timeout(500)
            body_text = _safe_page_text(page)
            return body_text, month_is_visible(body_text, year, month)
        except EOFError:
            pass

    return body_text, False


def extract_row(body_text: str, hut: str = TARGET_HUT, room: str = TARGET_ROOM) -> str:
    text = compact_spaces(body_text)
    heading = f"{hut} {room}"
    start = text.find(heading)
    if start == -1:
        return ""
    start_values = start + len(heading)

    # Stop at the nearest subsequent stop word.
    end_positions = []
    for word in ROOM_STOP_WORDS:
        if word == hut:
            continue
        pos = text.find(word, start_values)
        if pos != -1:
            end_positions.append(pos)
    end = min(end_positions) if end_positions else len(text)
    values = text[start_values:end].strip()
    return f"{heading} {values}".strip()


def tokenize_statuses(row_text: str) -> list[str]:
    if not row_text:
        return []
    heading = f"{TARGET_HUT} {TARGET_ROOM}"
    after_heading = row_text.replace(heading, "", 1)
    # Calendar status marks. Do not include digits from the heading, because it was removed.
    return re.findall(r"後日開始|予約不要|℡|[○満/]|[0-9]+", after_heading)


def judge_status(mark: str) -> tuple[str, Optional[bool]]:
    mark = mark.strip()
    if mark == "○":
        return "Available (vacancy)", True
    if re.fullmatch(r"[0-9]+", mark):
        return f"Available ({mark} rooms left)", True
    if mark == "満":
        return "Full", False
    if mark == "/":
        return "Out of season / unavailable", False
    if mark == "℡":
        return "Call hut directly", None
    if mark == "後日開始":
        return "Reservation starts later", False
    if mark == "予約不要":
        return "No reservation required", True
    return f"Unknown status ({mark})", None


def parse_calendar_status(body_text: str, day: int) -> tuple[str, str, Optional[bool], str, list[str]]:
    row_text = extract_row(body_text)
    if not row_text:
        return "", "Could not find Sugoroku-goya general room row", None, "", []

    tokens = tokenize_statuses(row_text)
    if len(tokens) < day:
        return "", f"Could not read day {day}. Found only {len(tokens)} status tokens.", None, row_text, tokens

    mark = tokens[day - 1]
    status_text, reservable = judge_status(mark)
    return mark, status_text, reservable, row_text, tokens



def load_line_config(config_path: str = LINE_CONFIG_JSON) -> tuple[str, str]:
    """Load LINE Messaging API token and user ID from environment variables or JSON file."""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("LINE_USER_ID", "").strip()

    path = Path(config_path)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            token = token or str(data.get("LINE_CHANNEL_ACCESS_TOKEN", "")).strip()
            user_id = user_id or str(data.get("LINE_USER_ID", "")).strip()
        except Exception as e:
            raise RuntimeError(f"Could not read {config_path}: {e}")

    if not token or "PUT_" in token:
        raise RuntimeError(
            "LINE_CHANNEL_ACCESS_TOKEN is not set. Edit line_config.json or set environment variable."
        )
    if not user_id or "PUT_" in user_id:
        raise RuntimeError(
            "LINE_USER_ID is not set. Edit line_config.json or set environment variable."
        )
    return token, user_id


def send_line_message(message: str, config_path: str = LINE_CONFIG_JSON) -> None:
    """Send a text message via LINE Messaging API push message endpoint."""
    token, user_id = load_line_config(config_path)
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": user_id,
        "messages": [
            {"type": "text", "text": message[:4900]}
        ],
    }
    res = requests.post(url, headers=headers, json=payload, timeout=20)
    if res.status_code >= 300:
        body = res.text[:1000]
        raise RuntimeError(f"LINE push failed: HTTP {res.status_code} {body}")


def build_line_message(result: CheckResult) -> str:
    reservable_label = "不明"
    if result.reservable is True:
        reservable_label = "予約できる可能性あり"
    elif result.reservable is False:
        reservable_label = "予約不可"

    return (
        "【双六小屋 空き状況】\n"
        f"日程：{result.year}-{result.month:02d}-{result.day:02d} / {result.nights}泊\n"
        f"小屋：{result.hut}\n"
        f"部屋：{result.room}\n"
        f"表示：{result.status_mark}\n"
        f"判定：{reservable_label}\n"
        f"詳細：{result.status_text}\n"
        f"確認時刻：{result.checked_at}\n"
        f"予約ページ：{CALENDAR_URL}"
    )


def load_last_reservable(state_path: str = LINE_STATE_JSON) -> Optional[bool]:
    path = Path(state_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        value = data.get("reservable")
        if value is True:
            return True
        if value is False:
            return False
    except Exception:
        pass
    return None


def save_last_state(result: CheckResult, state_path: str = LINE_STATE_JSON) -> None:
    data = {
        "checked_at": result.checked_at,
        "year": result.year,
        "month": result.month,
        "day": result.day,
        "hut": result.hut,
        "room": result.room,
        "status_mark": result.status_mark,
        "status_text": result.status_text,
        "reservable": result.reservable,
    }
    Path(state_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def maybe_notify_line(result: CheckResult, config_path: str, always_notify: bool = False) -> bool:
    """Notify LINE when reservable. By default, notify only when it becomes reservable."""
    should_send = False
    if always_notify:
        should_send = True
    elif result.reservable is True:
        last = load_last_reservable()
        # Send only when previous state was not reservable, to avoid hourly spam.
        should_send = last is not True

    if should_send:
        send_line_message(build_line_message(result), config_path=config_path)

    save_last_state(result)
    return should_send


def save_csv(result: CheckResult, path: str = RESULT_CSV) -> None:
    file_exists = Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "checked_at", "year", "month", "day", "nights", "hut", "room",
                "month_found", "status_mark", "status_text", "reservable", "url",
            ],
        )
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "checked_at": result.checked_at,
            "year": result.year,
            "month": result.month,
            "day": result.day,
            "nights": result.nights,
            "hut": result.hut,
            "room": result.room,
            "month_found": result.month_found,
            "status_mark": result.status_mark,
            "status_text": result.status_text,
            "reservable": result.reservable,
            "url": result.url,
        })


def check_month_summary(year: int, month: int, headless: bool) -> MonthSummary:
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(
            locale="ja-JP",
            viewport={"width": 1400, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )

        body_text = ""
        try:
            body_text, month_found = navigate_to_month(page, year, month, headless)
            row_text = extract_row(body_text)
            tokens = tokenize_statuses(row_text)
            num_days = calendar.monthrange(year, month)[1]
            days: list[MonthDayStatus] = []

            for day in range(1, num_days + 1):
                if day <= len(tokens):
                    mark = tokens[day - 1]
                    status_text, reservable = judge_status(mark)
                else:
                    mark = "N/A"
                    status_text = f"Could not read day {day}"
                    reservable = None
                days.append(MonthDayStatus(day, mark, status_text, reservable))

            if not row_text or not month_found or len(tokens) < num_days:
                write_debug(page, body_text, f"Monthly summary read problem. Found {len(tokens)} tokens for {num_days} days.")

            return MonthSummary(
                checked_at=checked_at,
                year=year,
                month=month,
                hut=TARGET_HUT,
                room=TARGET_ROOM,
                month_found=month_found,
                days=days,
                row_text=row_text,
                url=page.url,
            )
        except Exception:
            err = traceback.format_exc()
            try:
                body_text = page.locator("body").inner_text(timeout=5000)
            except Exception:
                pass
            write_debug(page, body_text, err)
            raise
        finally:
            browser.close()


def save_month_csv(summary: MonthSummary, path: str = "month_result.csv") -> None:
    file_exists = Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "checked_at", "year", "month", "day", "hut", "room",
                "month_found", "status_mark", "status_text", "reservable", "url",
            ],
        )
        if not file_exists:
            writer.writeheader()
        for item in summary.days:
            writer.writerow({
                "checked_at": summary.checked_at,
                "year": summary.year,
                "month": summary.month,
                "day": item.day,
                "hut": summary.hut,
                "room": summary.room,
                "month_found": summary.month_found,
                "status_mark": item.status_mark,
                "status_text": item.status_text,
                "reservable": item.reservable,
                "url": summary.url,
            })


def _month_day_label(summary: MonthSummary, item: MonthDayStatus) -> str:
    return f"{summary.month}/{item.day}:{item.status_mark}"


def build_month_summary_message(summary: MonthSummary) -> str:
    available = [item for item in summary.days if item.reservable is True]
    unknown = [item for item in summary.days if item.reservable is None]

    lines = [
        "【双六小屋 8月予約状況】",
        f"対象：{summary.year}年{summary.month}月 / {summary.hut} {summary.room}",
        f"確認時刻：{summary.checked_at}",
        "",
    ]

    if available:
        lines.append("予約できる可能性あり：")
        lines.append(" ".join(_month_day_label(summary, item) for item in available))
    else:
        lines.append("予約できる日：なし")

    if unknown:
        lines.append("")
        lines.append("要確認：")
        lines.append(" ".join(_month_day_label(summary, item) for item in unknown))

    lines.append("")
    lines.append("全日程：")
    chunk: list[str] = []
    for item in summary.days:
        chunk.append(_month_day_label(summary, item))
        if len(chunk) == 7:
            lines.append(" ".join(chunk))
            chunk = []
    if chunk:
        lines.append(" ".join(chunk))

    lines.append("")
    lines.append("記号：○/数字=予約可能、満=満室、/=不可、℡=電話確認")
    lines.append(f"予約ページ：{CALENDAR_URL}")
    return "\n".join(lines)[:4900]


def print_month_summary(summary: MonthSummary) -> None:
    print("========================================")
    print("Sugoroku-goya monthly availability")
    print("========================================")
    print(f"Month       : {summary.year}-{summary.month:02d}")
    print(f"Hut         : {summary.hut}")
    print(f"Room        : {summary.room}")
    print(f"Month found : {summary.month_found}")
    print(f"Checked at  : {summary.checked_at}")
    print("----------------------------------------")
    for item in summary.days:
        reservable_label = "UNKNOWN"
        if item.reservable is True:
            reservable_label = "YES"
        elif item.reservable is False:
            reservable_label = "NO"
        print(f"{summary.month:02d}/{item.day:02d}  {item.status_mark:<8}  {reservable_label}  {item.status_text}")
    print("========================================")


def check_sugoroku(year: int, month: int, day: int, nights: int, headless: bool) -> CheckResult:
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(
            locale="ja-JP",
            viewport={"width": 1400, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )

        body_text = ""
        try:
            body_text, month_found = navigate_to_month(page, year, month, headless)

            mark, status_text, reservable, row_text, _tokens = parse_calendar_status(body_text, day)
            if not row_text or not mark or not month_found:
                write_debug(page, body_text, status_text)

            return CheckResult(
                checked_at=checked_at,
                year=year,
                month=month,
                day=day,
                nights=nights,
                hut=TARGET_HUT,
                room=TARGET_ROOM,
                month_found=month_found,
                status_mark=mark or "N/A",
                status_text=status_text,
                reservable=reservable,
                row_text=row_text,
                url=page.url,
            )
        except Exception:
            err = traceback.format_exc()
            try:
                body_text = page.locator("body").inner_text(timeout=5000)
            except Exception:
                pass
            write_debug(page, body_text, err)
            raise
        finally:
            browser.close()


def print_result(result: CheckResult) -> None:
    reservable_label = "UNKNOWN"
    if result.reservable is True:
        reservable_label = "YES"
    elif result.reservable is False:
        reservable_label = "NO"

    print("========================================")
    print("Sugoroku-goya availability check")
    print("========================================")
    print(f"Date        : {result.year}-{result.month:02d}-{result.day:02d}")
    print(f"Nights      : {result.nights}")
    print(f"Hut         : {result.hut}")
    print(f"Room        : {result.room}")
    print(f"Month found : {result.month_found}")
    print(f"Mark        : {result.status_mark}")
    print(f"Status      : {result.status_text}")
    print(f"Reservable  : {reservable_label}")
    print(f"URL         : {result.url}")
    print("----------------------------------------")
    print("Extracted row:")
    print(result.row_text or "(row not found)")
    print("========================================")


def selftest() -> int:
    samples = [
        ("2026年 8月 双六小屋 一般室 ○ 満 3 / ℡ 後日開始 予約不要 個室(2名)", 1, "○", True),
        ("2026年 8月 双六小屋 一般室 ○ 満 3 / ℡ 後日開始 予約不要 個室(2名)", 2, "満", False),
        ("2026年 8月 双六小屋 一般室 ○ 満 3 / ℡ 後日開始 予約不要 個室(2名)", 3, "3", True),
        ("2026年 8月 双六小屋 一般室 ○ 満 3 / ℡ 後日開始 予約不要 個室(2名)", 4, "/", False),
        ("2026年 8月 双六小屋 一般室 ○ 満 3 / ℡ 後日開始 予約不要 個室(2名)", 5, "℡", None),
    ]
    ok = True
    print("Selftest started")
    for row, day, expected_mark, expected_reservable in samples:
        mark, status_text, reservable, row_text, tokens = parse_calendar_status(row, day)
        passed = mark == expected_mark and reservable == expected_reservable
        ok = ok and passed
        print(f"day={day} mark={mark} reservable={reservable} -> {'OK' if passed else 'NG'}")
        if not passed:
            print("  tokens:", tokens)
            print("  status:", status_text)
            print("  row:", row_text)
    print("Selftest", "OK" if ok else "NG")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    parser.add_argument("--month", type=int, default=DEFAULT_MONTH)
    parser.add_argument("--day", type=int, default=DEFAULT_DAY)
    parser.add_argument("--nights", type=int, default=DEFAULT_NIGHTS)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--notify-line", action="store_true", help="Send LINE notification when reservable")
    parser.add_argument("--line-config", default=LINE_CONFIG_JSON, help="Path to LINE config JSON")
    parser.add_argument("--always-notify", action="store_true", help="Send LINE notification every run, even if not reservable")
    parser.add_argument("--month-summary", action="store_true", help="Read the whole target month and print/send monthly summary")
    parser.add_argument("--line-test", action="store_true", help="Send a test LINE message and exit")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    if args.line_test:
        send_line_message("双六小屋チェッカー：LINE通知テストです。", config_path=args.line_config)
        print("LINE test message sent.")
        return 0

    if args.month_summary:
        summary = check_month_summary(args.year, args.month, args.headless)
        save_month_csv(summary)
        print_month_summary(summary)
        if args.notify_line:
            send_line_message(build_month_summary_message(summary), config_path=args.line_config)
            print("LINE monthly summary : SENT")
        return 0

    result = check_sugoroku(args.year, args.month, args.day, args.nights, args.headless)
    save_csv(result)
    print_result(result)

    if args.notify_line:
        sent = maybe_notify_line(result, config_path=args.line_config, always_notify=args.always_notify)
        print("LINE notify : SENT" if sent else "LINE notify : not sent")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
