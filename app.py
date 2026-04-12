import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, date, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import requests
from psycopg.rows import dict_row
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    FlexBox,
    FlexBubble,
    FlexButton,
    FlexContainer,
    FlexMessage,
    FlexSeparator,
    FlexText,
    MessageAction,
    MessagingApi,
    PushMessageRequest,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    TextMessage,
    URIAction,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent

load_dotenv()

TZ = ZoneInfo(os.getenv("TZ", "Asia/Tokyo"))
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
BOT_PASSWORD = os.getenv("BOT_PASSWORD", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
CRON_SECRET = os.getenv("CRON_SECRET", "")

LINE_LOGIN_CHANNEL_ID = os.getenv("LINE_LOGIN_CHANNEL_ID", "")
LIFF_REMINDER_ID = os.getenv("LIFF_REMINDER_ID", "")
LIFF_WANT_ID = os.getenv("LIFF_WANT_ID", "")
LIFF_BACKUP_ID = os.getenv("LIFF_BACKUP_ID", "")
LIFF_REMINDER_URL = os.getenv("LIFF_REMINDER_URL", "")
LIFF_WANT_URL = os.getenv("LIFF_WANT_URL", "")
LIFF_BACKUP_URL = os.getenv("LIFF_BACKUP_URL", "")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET or not BOT_PASSWORD:
    raise RuntimeError(
        "環境変数 LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET / BOT_PASSWORD を設定してください。"
    )

if not DATABASE_URL:
    raise RuntimeError("環境変数 DATABASE_URL を設定してください。")

if not CRON_SECRET:
    raise RuntimeError("環境変数 CRON_SECRET を設定してください。")

if not LINE_LOGIN_CHANNEL_ID:
    raise RuntimeError("環境変数 LINE_LOGIN_CHANNEL_ID を設定してください。")

if not LIFF_REMINDER_ID or not LIFF_WANT_ID or not LIFF_BACKUP_ID or not LIFF_REMINDER_URL or not LIFF_WANT_URL or not LIFF_BACKUP_URL:
    raise RuntimeError("環境変数 LIFF_REMINDER_ID / LIFF_WANT_ID / LIFF_BACKUP_ID / LIFF_REMINDER_URL / LIFF_WANT_URL / LIFF_BACKUP_URL を設定してください。")

app = FastAPI()
parser = WebhookParser(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

STATE_NONE = "none"
STATE_WAITING_PASSWORD = "waiting_password"
STATE_WAITING_REMINDER_CONTENT = "waiting_reminder_content"
STATE_WAITING_REMINDER_DATETIME = "waiting_reminder_datetime"
STATE_WAITING_WANT_CONTENT = "waiting_want_content"

WEEKDAY_MAP = {
    "月": 0, "月曜": 0, "月曜日": 0,
    "火": 1, "火曜": 1, "火曜日": 1,
    "水": 2, "水曜": 2, "水曜日": 2,
    "木": 3, "木曜": 3, "木曜日": 3,
    "金": 4, "金曜": 4, "金曜日": 4,
    "土": 5, "土曜": 5, "土曜日": 5,
    "日": 6, "日曜": 6, "日曜日": 6,
}

JP_WEEK_FULL = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"]


@contextmanager
def get_conn():
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_jst() -> datetime:
    return datetime.now(TZ)


def init_db():
    with get_conn() as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_states (
            user_id TEXT PRIMARY KEY,
            state TEXT NOT NULL DEFAULT 'none',
            temp_content TEXT,
            updated_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            kind TEXT NOT NULL,
            scheduled_at TEXT,
            weekday INTEGER,
            time_hhmm TEXT NOT NULL,
            last_day_notice_date TEXT,
            last_1h_notice_date TEXT,
            last_10m_notice_date TEXT,
            last_exact_notice_date TEXT,
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS wants (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS backups (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            data JSONB NOT NULL,
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS authorized_users (
            user_id TEXT PRIMARY KEY,
            authorized_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS auth_attempts (
            user_id TEXT PRIMARY KEY,
            failed_count INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT,
            updated_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        ALTER TABLE reminders
        ADD COLUMN IF NOT EXISTS last_exact_notice_date TEXT
        """)


def get_state(user_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, state, temp_content, updated_at FROM user_states WHERE user_id = %s",
            (user_id,)
        ).fetchone()

        if row:
            return row

        current = {
            "user_id": user_id,
            "state": STATE_NONE,
            "temp_content": None,
            "updated_at": now_jst().isoformat()
        }
        conn.execute(
            """
            INSERT INTO user_states (user_id, state, temp_content, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                state = EXCLUDED.state,
                temp_content = EXCLUDED.temp_content,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, current["state"], current["temp_content"], current["updated_at"])
        )
        return current


def set_state(user_id: str, state: str, temp_content: str | None = None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_states (user_id, state, temp_content, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                state = EXCLUDED.state,
                temp_content = EXCLUDED.temp_content,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, state, temp_content, now_jst().isoformat())
        )


def reset_state(user_id: str):
    set_state(user_id, STATE_NONE, None)


def is_authorized_user(user_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM authorized_users WHERE user_id = %s",
            (user_id,)
        ).fetchone()
        return row is not None


def authorize_user(user_id: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO authorized_users (user_id, authorized_at)
            VALUES (%s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                authorized_at = EXCLUDED.authorized_at
            """,
            (user_id, now_jst().isoformat())
        )

def unauthorize_user(user_id: str):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM authorized_users WHERE user_id = %s",
             (user_id,)
        )


def get_auth_attempt(user_id: str) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT user_id, failed_count, locked_until, updated_at
            FROM auth_attempts
            WHERE user_id = %s
            """,
            (user_id,)
        ).fetchone()

        if row:
            return row

        current = {
            "user_id": user_id,
            "failed_count": 0,
            "locked_until": None,
            "updated_at": now_jst().isoformat(),
        }
        conn.execute(
            """
            INSERT INTO auth_attempts (user_id, failed_count, locked_until, updated_at)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, 0, None, now_jst().isoformat())
        )
        return current


def reset_auth_attempt(user_id: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO auth_attempts (user_id, failed_count, locked_until, updated_at)
            VALUES (%s, 0, NULL, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                failed_count = 0,
                locked_until = NULL,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, now_jst().isoformat())
        )


def register_auth_failure(user_id: str, lock_minutes: int = 10, max_failures: int = 3) -> tuple[int, str | None]:
    current = get_auth_attempt(user_id)
    failed_count = int(current["failed_count"]) + 1
    locked_until = None

    if failed_count >= max_failures:
        locked_until_dt = now_jst() + timedelta(minutes=lock_minutes)
        locked_until = locked_until_dt.isoformat()
        failed_count = 0

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO auth_attempts (user_id, failed_count, locked_until, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(user_id) DO UPDATE SET
                failed_count = EXCLUDED.failed_count,
                locked_until = EXCLUDED.locked_until,
                updated_at = EXCLUDED.updated_at
            """,
            (user_id, failed_count, locked_until, now_jst().isoformat())
        )

    return failed_count, locked_until


def is_auth_locked(user_id: str) -> tuple[bool, datetime | None]:
    current = get_auth_attempt(user_id)
    if not current["locked_until"]:
        return False, None

    locked_until = datetime.fromisoformat(current["locked_until"]).astimezone(TZ)
    if now_jst() < locked_until:
        return True, locked_until

    reset_auth_attempt(user_id)
    return False, None


def send_reply(reply_token: str, messages: list):
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=messages
            )
        )


def send_push(user_id: str, messages: list):
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.push_message(
            PushMessageRequest(
                to=user_id,
                messages=messages
            )
        )


def text_message(text: str, quick_reply: QuickReply | None = None) -> TextMessage:
    return TextMessage(text=text, quick_reply=quick_reply)


def liff_quick_reply(menu_type: str) -> QuickReply:
    if menu_type == "reminder":
        items = [
            QuickReplyItem(action=MessageAction(label="追加", text="リマインド追加")),
            QuickReplyItem(action=URIAction(label="一覧", uri=LIFF_REMINDER_URL)),
            QuickReplyItem(action=MessageAction(label="メニューに戻る", text="メニュー")),
        ]
    else:
        items = [
            QuickReplyItem(action=MessageAction(label="追加", text="ほしいもの追加")),
            QuickReplyItem(action=URIAction(label="一覧", uri=LIFF_WANT_URL)),
            QuickReplyItem(action=MessageAction(label="メニューに戻る", text="メニュー")),
        ]
    return QuickReply(items=items)

def cancel_quick_reply() -> QuickReply:
    return QuickReply(
        items=[
            QuickReplyItem(action=MessageAction(label="キャンセル", text="キャンセル"))
        ]
    )


def save_restore_quick_reply() -> QuickReply:
    return QuickReply(
        items=[
            QuickReplyItem(action=MessageAction(label="保存", text="保存")),
            QuickReplyItem(action=MessageAction(label="復元", text="復元")),
            QuickReplyItem(action=MessageAction(label="メニューに戻る", text="メニュー")),
        ]
    )

def safe_date_str(y: int, m: int, d: int) -> str:
    zw = "\u200b"
    return f"{y}-{zw}{m:02d}-{zw}{d:02d}"


def format_single_datetime_jp(dt: datetime) -> str:
    dt = dt.astimezone(TZ)
    safe_date = safe_date_str(dt.year, dt.month, dt.day)
    return f"{safe_date}（{JP_WEEK_FULL[dt.weekday()]}） {dt.strftime('%H:%M')}"


def format_weekly_label(weekday: int, hhmm: str) -> str:
    return f"毎週 {JP_WEEK_FULL[weekday]} {hhmm}"


def flex_notify_message(title: str, content: str, subtext: str, color: str) -> FlexMessage:
    bubble = FlexBubble(
        header=FlexBox(
            layout="vertical",
            background_color=color,
            padding_all="12px",
            contents=[
                FlexText(
                    text=title,
                    color="#FFFFFF",
                    weight="bold",
                    size="lg",
                    wrap=True,
                )
            ],
        ),
        body=FlexBox(
            layout="vertical",
            spacing="md",
            padding_all="16px",
            contents=[
                FlexText(
                    text=f"「{content}」",
                    weight="bold",
                    size="xl",
                    wrap=True,
                ),
                FlexText(
                    text=subtext,
                    size="sm",
                    color="#666666",
                    wrap=True,
                ),
            ],
        ),
    )
    return FlexMessage(
        alt_text=f"{title} {content}",
        contents=FlexContainer.from_json(json.dumps(bubble.to_dict())),
    )


def flex_today_digest_message(items: list[tuple[datetime, str]]) -> FlexMessage:
    lines = [
        FlexText(
            text=f"{event_dt.strftime('%H:%M')}  「{content}」",
            size="md",
            wrap=True,
        )
        for event_dt, content in items
    ]

    bubble = FlexBubble(
        header=FlexBox(
            layout="vertical",
            background_color="#10B981",
            padding_all="12px",
            contents=[
                FlexText(
                    text="📅 今日の予定",
                    color="#FFFFFF",
                    weight="bold",
                    size="lg",
                )
            ],
        ),
        body=FlexBox(
            layout="vertical",
            spacing="md",
            padding_all="16px",
            contents=lines if lines else [FlexText(text="今日は予定がないよ。", size="md")],
        ),
    )
    return FlexMessage(
        alt_text="今日の予定",
        contents=FlexContainer.from_json(json.dumps(bubble.to_dict())),
    )


def main_menu_message() -> FlexMessage:
    bubble = FlexBubble(
        body=FlexBox(
            layout="vertical",
            spacing="lg",
            padding_all="20px",
            contents=[
                FlexText(text="メニュー", weight="bold", size="xl"),
                FlexSeparator(margin="md"),
                FlexBox(
                    layout="horizontal",
                    spacing="lg",
                    margin="lg",
                    contents=[
                        menu_button("リマインド", "リマインド", primary=True),
                        menu_button("ほしいもの", "ほしいもの", primary=True),
                    ]
                ),
                FlexBox(
                    layout="horizontal",
                    spacing="lg",
                    margin="md",
                    contents=[
                        menu_button("Coming Soon", "Coming Soon", primary=False),
                        menu_button("保存・復元", "保存復元", primary=True, uri=LIFF_BACKUP_URL),
                    ]
                ),
            ]
        )
    )
    return FlexMessage(
        alt_text="メニュー",
        contents=FlexContainer.from_json(json.dumps(bubble.to_dict()))
    )


def menu_button(label: str, text: str, primary: bool = True, uri: str | None = None) -> FlexBox:
    action = URIAction(label=label, uri=uri) if uri else MessageAction(label=label, text=text)

    return FlexBox(
        layout="vertical",
        flex=1,
        height="72px",
        justify_content="center",
        align_items="center",
        background_color="#1EC94C" if primary else "#D9DDE3",
        corner_radius="14px",
        action=action,
        contents=[
            FlexText(
                text=label,
                color="#FFFFFF" if primary else "#222222",
                weight="bold",
                size="md",
                align="center",
                wrap=True
            )
        ]
    )


def normalize_digits(text: str) -> str:
    return text.translate(str.maketrans("０１２３４５６７８９：", "0123456789:"))


def parse_time_hhmm(s: str) -> str | None:
    s = normalize_digits(s)

    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"

    m = re.search(r"(\d{1,2})時(?:(\d{1,2})分)?", s)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2)) if m.group(2) is not None else 0
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"

    return None


def require_time_hhmm(s: str) -> str | None:
    return parse_time_hhmm(s)


def parse_relative_datetime(s: str) -> datetime | None:
    s = normalize_digits(s)
    now = now_jst().replace(second=0, microsecond=0)

    m = re.search(r"(\d+)\s*分後", s)
    if m:
        minutes = int(m.group(1))
        return now + timedelta(minutes=minutes)

    m = re.search(r"(\d+)\s*時間後", s)
    if m:
        hours = int(m.group(1))
        return now + timedelta(hours=hours)

    return None


def parse_time_only_datetime(s: str) -> tuple[datetime, str] | None:
    hhmm = parse_time_hhmm(s)
    if not hhmm:
        return None

    now = now_jst()
    hh, mm = map(int, hhmm.split(":"))
    candidate = datetime.combine(now.date(), time(hh, mm), tzinfo=TZ)

    if candidate <= now:
        candidate = candidate + timedelta(days=1)

    return candidate, hhmm


def parse_datetime_input(text: str) -> dict[str, Any] | None:
    s = normalize_digits(text.strip())
    now = now_jst()

    relative_dt = parse_relative_datetime(s)
    if relative_dt:
        return {
            "kind": "single",
            "scheduled_at": relative_dt,
            "time_hhmm": relative_dt.strftime("%H:%M")
        }

    time_only = parse_time_only_datetime(s)
    if time_only and not any(word in s for word in ["今日", "明日", "明後日", "来週", "月", "火", "水", "木", "金", "土", "日", "/"]):
        dt, hhmm = time_only
        return {
            "kind": "single",
            "scheduled_at": dt,
            "time_hhmm": hhmm
        }

    def need_time() -> dict[str, str]:
        return {"error": "time_required"}

    m = re.search(r"(今週|来週|再来週)\s*(月曜?日?|火曜?日?|水曜?日?|木曜?日?|金曜?日?|土曜?日?|日曜?日?)", s)
    if m:
        week_text = m.group(1)
        wd_text = m.group(2)
        weekday = WEEKDAY_MAP.get(wd_text)
        if weekday is not None:
            hhmm = require_time_hhmm(s)
            if not hhmm:
                return need_time()

            week_offset = {"今週": 0, "来週": 1, "再来週": 2}[week_text]
            current_weekday = now.weekday()
            start_of_this_week = now.date() - timedelta(days=current_weekday)
            target_date = start_of_this_week + timedelta(days=7 * week_offset + weekday)

            hh, mm = map(int, hhmm.split(":"))
            dt = datetime.combine(target_date, time(hh, mm), tzinfo=TZ)

            if dt < now:
                return {"error": "past_datetime"}

            return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    for key, add_days in [("今日", 0), ("明日", 1), ("明後日", 2)]:
        if key in s:
            hhmm = require_time_hhmm(s)
            if not hhmm:
                return need_time()
            target_date = (now + timedelta(days=add_days)).date()
            hh, mm = map(int, hhmm.split(":"))
            dt = datetime.combine(target_date, time(hh, mm), tzinfo=TZ)
            return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    m = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", s)
    if m:
        y, mo, d = map(int, m.groups())
        hhmm = require_time_hhmm(s)
        if not hhmm:
            return need_time()
        hh, mm = map(int, hhmm.split(":"))
        dt = datetime(y, mo, d, hh, mm, tzinfo=TZ)
        return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        y, mo, d = map(int, m.groups())
        hhmm = require_time_hhmm(s)
        if not hhmm:
            return need_time()
        hh, mm = map(int, hhmm.split(":"))
        dt = datetime(y, mo, d, hh, mm, tzinfo=TZ)
        return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", s)
    if m:
        mo, d = map(int, m.groups())
        year = now.year
        hhmm = require_time_hhmm(s)
        if not hhmm:
            return need_time()
        hh, mm = map(int, hhmm.split(":"))
        dt = datetime(year, mo, d, hh, mm, tzinfo=TZ)
        if dt < now:
            dt = datetime(year + 1, mo, d, hh, mm, tzinfo=TZ)
        return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    weekday_found = None
    for key, value in WEEKDAY_MAP.items():
        if key in s:
            weekday_found = value
            break

    if weekday_found is not None:
        hhmm = require_time_hhmm(s)
        if not hhmm:
            return need_time()
        return {"kind": "weekly", "weekday": weekday_found, "time_hhmm": hhmm}

    if "来週" in s:
        hhmm = require_time_hhmm(s)
        if not hhmm:
            return need_time()
        target_date = (now + timedelta(days=7)).date()
        hh, mm = map(int, hhmm.split(":"))
        dt = datetime.combine(target_date, time(hh, mm), tzinfo=TZ)
        return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    return None


def create_reminder(user_id: str, content: str, parsed: dict[str, Any]):
    with get_conn() as conn:
        if parsed["kind"] == "single":
            conn.execute(
                """
                INSERT INTO reminders (
                    user_id, content, kind, scheduled_at, weekday, time_hhmm,
                    last_day_notice_date, last_1h_notice_date, last_10m_notice_date, last_exact_notice_date, created_at
                )
                VALUES (%s, %s, 'single', %s, NULL, %s, NULL, NULL, NULL, NULL, %s)
                """,
                (
                    user_id,
                    content,
                    parsed["scheduled_at"].isoformat(),
                    parsed["time_hhmm"],
                    now_jst().isoformat(),
                )
            )
        else:
            conn.execute(
                """
                INSERT INTO reminders (
                    user_id, content, kind, scheduled_at, weekday, time_hhmm,
                    last_day_notice_date, last_1h_notice_date, last_10m_notice_date, last_exact_notice_date, created_at
                )
                VALUES (%s, %s, 'weekly', NULL, %s, %s, NULL, NULL, NULL, NULL, %s)
                """,
                (
                    user_id,
                    content,
                    parsed["weekday"],
                    parsed["time_hhmm"],
                    now_jst().isoformat(),
                )
            )


def add_want(user_id: str, content: str):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM wants
            WHERE user_id = %s
            ORDER BY created_at ASC
            """,
            (user_id,)
        ).fetchall()

        if len(rows) >= 5:
            conn.execute(
                "DELETE FROM wants WHERE id = %s",
                (rows[0]["id"],)
            )

        conn.execute(
            "INSERT INTO wants (user_id, content, created_at) VALUES (%s, %s, %s)",
            (user_id, content, now_jst().isoformat())
        )


def save_backup(user_id: str) -> str:
    with get_conn() as conn:
        reminders = conn.execute(
            """
            SELECT content, kind, scheduled_at, weekday, time_hhmm
            FROM reminders
            WHERE user_id = %s
            ORDER BY created_at ASC
            """,
            (user_id,)
        ).fetchall()

        wants = conn.execute(
            """
            SELECT content
            FROM wants
            WHERE user_id = %s
            ORDER BY created_at ASC
            """,
            (user_id,)
        ).fetchall()

        if not reminders and not wants:
            return "skip"

        data = {
            "reminders": [dict(r) for r in reminders],
            "wants": [dict(w) for w in wants],
        }

        backups = conn.execute(
            """
            SELECT id
            FROM backups
            WHERE user_id = %s
            ORDER BY created_at ASC
            """,
            (user_id,)
        ).fetchall()

        if len(backups) >= 5:
            conn.execute(
                "DELETE FROM backups WHERE id = %s",
                (backups[0]["id"],)
            )

        conn.execute(
            "INSERT INTO backups (user_id, data, created_at) VALUES (%s, %s, %s)",
            (user_id, json.dumps(data, ensure_ascii=False), now_jst().isoformat())
        )

        return "saved"


def list_backups(user_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, created_at
            FROM backups
            WHERE user_id = %s
            ORDER BY created_at DESC
            """,
            (user_id,)
        ).fetchall()


def restore_backup(user_id: str, backup_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT data FROM backups WHERE id = %s AND user_id = %s",
            (backup_id, user_id)
        ).fetchone()

        if not row:
            return False

        data = row["data"]

        conn.execute("DELETE FROM reminders WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM wants WHERE user_id = %s", (user_id,))

        for r in data.get("reminders", []):
            conn.execute(
                """
                INSERT INTO reminders (
                    user_id, content, kind, scheduled_at, weekday, time_hhmm,
                    last_day_notice_date, last_1h_notice_date, last_10m_notice_date, last_exact_notice_date, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, NULL, NULL, %s)
                """,
                (
                    user_id,
                    r.get("content", ""),
                    r.get("kind", "single"),
                    r.get("scheduled_at"),
                    r.get("weekday"),
                    r.get("time_hhmm", "00:00"),
                    now_jst().isoformat(),
                )
            )

        for w in data.get("wants", []):
            conn.execute(
                "INSERT INTO wants (user_id, content, created_at) VALUES (%s, %s, %s)",
                (user_id, w.get("content", ""), now_jst().isoformat())
            )

        return True


def delete_reminder_by_id(user_id: str, reminder_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM reminders WHERE id = %s AND user_id = %s",
            (reminder_id, user_id)
        )
        return cur.rowcount > 0


def delete_want_by_id(user_id: str, want_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM wants WHERE id = %s AND user_id = %s",
            (want_id, user_id)
        )
        return cur.rowcount > 0


def list_reminders_text(user_id: str) -> str:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, content, kind, scheduled_at, weekday, time_hhmm
            FROM reminders
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (user_id,)
        ).fetchall()

    if not rows:
        return "リマインダーはまだないよ。"

    lines = ["リマインダー一覧"]
    for row in rows:
        if row["kind"] == "single":
            dt = datetime.fromisoformat(row["scheduled_at"]).astimezone(TZ)
            when = format_single_datetime_jp(dt)
        else:
            when = format_weekly_label(row["weekday"], row["time_hhmm"])
        lines.append(f"・ID:{row['id']} / {row['content']} / {when}")
    return "\n".join(lines)


def list_wants_text(user_id: str) -> str:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, content
            FROM wants
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (user_id,)
        ).fetchall()

    if not rows:
        return "ほしいものはまだないよ。"

    lines = ["ほしいもの一覧"]
    for row in rows:
        lines.append(f"・{row['content']}")
    return "\n".join(lines)


def should_timeout(updated_at_iso: str) -> bool:
    updated_at = datetime.fromisoformat(updated_at_iso).astimezone(TZ)
    return now_jst() - updated_at > timedelta(minutes=10)


def get_today_occurrences(row: dict[str, Any], base_now: datetime) -> tuple[date, datetime] | None:
    today = base_now.date()

    if row["kind"] == "single":
        event_dt = datetime.fromisoformat(row["scheduled_at"]).astimezone(TZ)
        if event_dt.date() == today:
            return today, event_dt
        return None

    if row["kind"] == "weekly":
        if row["weekday"] != base_now.weekday():
            return None
        hh, mm = map(int, row["time_hhmm"].split(":"))
        event_dt = datetime.combine(today, time(hh, mm), tzinfo=TZ)
        return today, event_dt

    return None


def send_due_notifications() -> dict[str, Any]:
    current = now_jst()
    today_str = current.date().isoformat()

    sent_1h = 0
    sent_10m = 0
    sent_exact = 0
    sent_today_digest = 0
    deleted_single = 0

    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM reminders").fetchall()

        today_items_by_user: dict[str, list[tuple[int, datetime, str]]] = {}

        for row in rows:
            occurrence = get_today_occurrences(row, current)
            if not occurrence:
                continue

            _, event_dt = occurrence
            one_hour_before = event_dt - timedelta(hours=1)
            ten_min_before = event_dt - timedelta(minutes=10)

            if row["last_day_notice_date"] != today_str:
                today_items_by_user.setdefault(row["user_id"], []).append(
                    (row["id"], event_dt, row["content"])
                )

            if row["last_1h_notice_date"] != today_str:
                if current >= one_hour_before and current < one_hour_before + timedelta(minutes=1):
                    send_push(row["user_id"], [
                        flex_notify_message(
                            "⏰ 1時間前",
                            row["content"],
                            f"予定時刻: {event_dt.strftime('%H:%M')}",
                            "#3B82F6"
                        )
                    ])
                    conn.execute(
                        "UPDATE reminders SET last_1h_notice_date = %s WHERE id = %s",
                        (today_str, row["id"])
                    )
                    sent_1h += 1

            if row["last_10m_notice_date"] != today_str:
                if current >= ten_min_before and current < ten_min_before + timedelta(minutes=1):
                    send_push(row["user_id"], [
                        flex_notify_message(
                            "🔔 10分前",
                            row["content"],
                            f"まもなく {event_dt.strftime('%H:%M')}",
                            "#F59E0B"
                        )
                    ])
                    conn.execute(
                        "UPDATE reminders SET last_10m_notice_date = %s WHERE id = %s",
                        (today_str, row["id"])
                    )
                    sent_10m += 1

            if row["last_exact_notice_date"] != today_str:
                if current >= event_dt and current < event_dt + timedelta(minutes=1):
                    send_push(row["user_id"], [
                        flex_notify_message(
                            "⚠️ 時間です",
                            row["content"],
                            f"{event_dt.strftime('%H:%M')} の予定",
                            "#EF4444"
                        )
                    ])
                    conn.execute(
                        "UPDATE reminders SET last_exact_notice_date = %s WHERE id = %s",
                        (today_str, row["id"])
                    )
                    sent_exact += 1

        if current.hour == 0 and current.minute == 0:
            for user_id, items in today_items_by_user.items():
                items.sort(key=lambda x: x[1])

                digest_items = [(event_dt, content) for _, event_dt, content in items]
                send_push(user_id, [flex_today_digest_message(digest_items)])

                for reminder_id, _, _ in items:
                    conn.execute(
                        "UPDATE reminders SET last_day_notice_date = %s WHERE id = %s",
                        (today_str, reminder_id)
                    )

                sent_today_digest += 1

        deleted_cur = conn.execute(
            """
            DELETE FROM reminders
            WHERE kind = 'single'
              AND CAST(scheduled_at AS timestamptz) < CAST(%s AS timestamptz)
            """,
            (current.isoformat(),)
        )
        deleted_single = deleted_cur.rowcount

        timeout_border = (current - timedelta(minutes=10)).isoformat()
        conn.execute(
            """
            UPDATE user_states
            SET state = %s, temp_content = NULL, updated_at = %s
            WHERE state != %s
              AND updated_at < %s
            """,
            (STATE_NONE, current.isoformat(), STATE_NONE, timeout_border)
        )

    return {
        "ok": True,
        "sent_today_digest": sent_today_digest,
        "sent_1h": sent_1h,
        "sent_10m": sent_10m,
        "sent_exact": sent_exact,
        "deleted_single": deleted_single,
        "checked_at": current.isoformat(),
    }


def format_card_date(dt_str: str) -> dict[str, str]:
    dt = datetime.fromisoformat(dt_str).astimezone(TZ)
    return {
        "date": f"{dt.month}/{dt.day}",
        "weekday": JP_WEEK_FULL[dt.weekday()].replace("曜日", ""),
        "time": dt.strftime("%H:%M"),
        "iso_date": dt.date().isoformat(),
    }


def reminder_row_to_card(row: dict[str, Any]) -> dict[str, Any]:
    if row["kind"] == "single":
        fmt = format_card_date(row["scheduled_at"])
        return {
            "id": row["id"],
            "kind": row["kind"],
            "content": row["content"],
            "date": fmt["date"],
            "weekday": fmt["weekday"],
            "time": fmt["time"],
            "iso_date": fmt["iso_date"],
        }

    return {
        "id": row["id"],
        "kind": row["kind"],
        "content": row["content"],
        "date": "",
        "weekday": JP_WEEK_FULL[row["weekday"]].replace("曜日", ""),
        "time": row["time_hhmm"],
        "iso_date": "",
    }


def wants_row_to_card(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "content": row["content"],
    }


def verify_line_id_token(id_token: str) -> dict[str, Any]:
    res = requests.post(
        "https://api.line.me/oauth2/v2.1/verify",
        data={
            "id_token": id_token,
            "client_id": LINE_LOGIN_CHANNEL_ID,
        },
        timeout=20,
    )
    if not res.ok:
        raise HTTPException(status_code=401, detail="invalid id token")
    return res.json()


def get_user_id_from_verified_id_token(x_line_id_token: str | None) -> str:
    if not x_line_id_token:
        raise HTTPException(status_code=401, detail="x-line-id-token がありません。")

    verified = verify_line_id_token(x_line_id_token)
    user_id = verified.get("sub", "")

    if not user_id:
        raise HTTPException(status_code=401, detail="user id not found in id token")

    return user_id


def build_reminders_liff_html() -> str:
    return f"""
<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
  <title>リマインド一覧</title>
  <script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      background: #f5f6f8;
      color: #222;
    }}
    .wrap {{ padding: 14px 14px 24px; }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 12px;
    }}
    .title {{
      font-size: 20px;
      font-weight: 700;
    }}
    .close-btn {{
      width: 36px;
      height: 36px;
      border: none;
      border-radius: 18px;
      background: #e9ecef;
      font-size: 20px;
      cursor: pointer;
    }}
    .tabs {{
      display: flex;
      gap: 8px;
      margin-bottom: 14px;
    }}
    .tab {{
      flex: 1;
      border: none;
      border-radius: 12px;
      padding: 10px 0;
      background: #e9ecef;
      font-weight: 700;
      cursor: pointer;
    }}
    .tab.active {{
      background: #12b85a;
      color: #fff;
    }}
    .panel {{ display: none; }}
    .panel.active {{ display: block; }}
    .card {{
      position: relative;
      background: #fff;
      border-radius: 16px;
      padding: 14px 44px 14px 14px;
      margin-bottom: 12px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.06);
    }}
    .card-top {{
      display: flex;
      gap: 8px;
      align-items: center;
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 8px;
    }}
    .card-content {{
      font-size: 16px;
      line-height: 1.4;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .card-x {{
      position: absolute;
      top: 10px;
      right: 10px;
      width: 28px;
      height: 28px;
      border: none;
      border-radius: 14px;
      background: #f1f3f5;
      font-size: 16px;
      cursor: pointer;
    }}
    .empty {{
      padding: 18px;
      text-align: center;
      color: #666;
    }}
    .calendar-box {{
      background: #fff;
      border-radius: 16px;
      padding: 12px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.06);
      margin-bottom: 12px;
    }}
    .calendar-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
    }}
    .cal-nav {{
      border: none;
      background: #e9ecef;
      border-radius: 10px;
      width: 34px;
      height: 34px;
      cursor: pointer;
      font-size: 16px;
    }}
    .month-label {{
      font-weight: 700;
      font-size: 16px;
    }}
    .week-row, .grid {{
      display: grid;
      grid-template-columns: repeat(7, 1fr);
      gap: 6px;
    }}
    .week-cell {{
      text-align: center;
      font-size: 12px;
      color: #666;
      padding: 4px 0;
    }}
    .day-cell {{
      position: relative;
      min-height: 48px;
      border: none;
      border-radius: 12px;
      background: #f6f7f9;
      cursor: pointer;
      font-size: 14px;
      font-weight: 600;
    }}
    .day-cell.selected {{
      background: #12b85a;
      color: #fff;
    }}
    .day-cell.other {{
      opacity: 0.35;
    }}
    .dot {{
      position: absolute;
      left: 50%;
      bottom: 6px;
      width: 6px;
      height: 6px;
      margin-left: -3px;
      border-radius: 3px;
      background: #111;
    }}
    .day-cell.selected .dot {{ background: #fff; }}
    .modal-backdrop {{
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.35);
      display: none;
      align-items: flex-end;
      justify-content: center;
      padding: 14px;
    }}
    .modal-backdrop.show {{ display: flex; }}
    .modal {{
      width: 100%;
      max-width: 420px;
      background: #fff;
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.18);
    }}
    .modal-text {{
      font-size: 16px;
      line-height: 1.5;
      margin-bottom: 14px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .modal-actions {{
      display: flex;
      justify-content: flex-end;
      gap: 10px;
    }}
    .btn {{
      border: none;
      border-radius: 12px;
      padding: 10px 16px;
      font-weight: 700;
      cursor: pointer;
    }}
    .btn-no {{ background: #e9ecef; }}
    .btn-yes {{ background: #12b85a; color: #fff; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="title">リマインド一覧</div>
      <button class="close-btn" id="closeBtn">×</button>
    </div>

    <div class="tabs">
      <button class="tab active" data-tab="list">一覧</button>
      <button class="tab" data-tab="repeat">繰り返し</button>
      <button class="tab" data-tab="calendar">カレンダー</button>
    </div>

    <div class="panel active" id="panel-list">
      <div id="singleList"></div>
    </div>

    <div class="panel" id="panel-repeat">
      <div id="repeatList"></div>
    </div>

    <div class="panel" id="panel-calendar">
      <div class="calendar-box">
        <div class="calendar-head">
          <button class="cal-nav" id="prevMonth">‹</button>
          <div class="month-label" id="monthLabel"></div>
          <button class="cal-nav" id="nextMonth">›</button>
        </div>
        <div class="week-row">
          <div class="week-cell">日</div>
          <div class="week-cell">月</div>
          <div class="week-cell">火</div>
          <div class="week-cell">水</div>
          <div class="week-cell">木</div>
          <div class="week-cell">金</div>
          <div class="week-cell">土</div>
        </div>
        <div class="grid" id="calendarGrid"></div>
      </div>
      <div id="calendarItems"></div>
    </div>
  </div>

  <div class="modal-backdrop" id="deleteModal">
    <div class="modal">
      <div class="modal-text" id="deleteText"></div>
      <div class="modal-actions">
        <button class="btn btn-no" id="deleteNo">いいえ</button>
        <button class="btn btn-yes" id="deleteYes">はい</button>
      </div>
    </div>
  </div>

<script>
const LIFF_ID = "{LIFF_REMINDER_ID}";
let idToken = "";
let deleteTargetId = null;
let currentCalendarDate = new Date();
let selectedDate = null;

const singleList = document.getElementById("singleList");
const repeatList = document.getElementById("repeatList");
const calendarItems = document.getElementById("calendarItems");
const monthLabel = document.getElementById("monthLabel");
const calendarGrid = document.getElementById("calendarGrid");
const deleteModal = document.getElementById("deleteModal");
const deleteText = document.getElementById("deleteText");

document.getElementById("closeBtn").addEventListener("click", () => {{
  if (window.liff) liff.closeWindow();
}});

document.querySelectorAll(".tab").forEach(btn => {{
  btn.addEventListener("click", () => {{
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("panel-" + btn.dataset.tab).classList.add("active");
  }});
}});

document.getElementById("deleteNo").addEventListener("click", () => {{
  deleteModal.classList.remove("show");
  deleteTargetId = null;
}});

document.getElementById("deleteYes").addEventListener("click", async () => {{
  if (!deleteTargetId) return;
  await fetch(`/api/reminders/${{deleteTargetId}}`, {{
    method: "DELETE",
    headers: {{
      "x-line-id-token": idToken
    }}
  }});
  deleteModal.classList.remove("show");
  deleteTargetId = null;
  await loadAllReminderViews();
}});

document.getElementById("prevMonth").addEventListener("click", async () => {{
  currentCalendarDate = new Date(currentCalendarDate.getFullYear(), currentCalendarDate.getMonth() - 1, 1);
  await loadCalendar();
}});

document.getElementById("nextMonth").addEventListener("click", async () => {{
  currentCalendarDate = new Date(currentCalendarDate.getFullYear(), currentCalendarDate.getMonth() + 1, 1);
  await loadCalendar();
}});

function openDeleteModal(id, content) {{
  deleteTargetId = id;
  deleteText.textContent = `「${{content}}」を削除しますか？`;
  deleteModal.classList.add("show");
}}

function escapeHtml(text) {{
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}}

function authHeaders() {{
  return {{
    "x-line-id-token": idToken
  }};
}}

function withAuth(url, options = {{}}) {{
  const headers = Object.assign({{}}, options.headers || {{}}, authHeaders());
  return fetch(url, Object.assign({{}}, options, {{ headers }}));
}}

async function apiGet(url) {{
  const res = await withAuth(url);
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}}

async function loadSingleList() {{
  const data = await apiGet("/api/reminders/one-time");
  renderReminderCards(singleList, data.items, "単発予定はまだないよ。");
}}

async function loadRepeatList() {{
  const data = await apiGet("/api/reminders/repeat");
  renderReminderCards(repeatList, data.items, "繰り返し予定はまだないよ。");
}}

async function loadCalendar() {{
  const year = currentCalendarDate.getFullYear();
  const month = currentCalendarDate.getMonth() + 1;
  const data = await apiGet(`/api/reminders/calendar?year=${{year}}&month=${{month}}`);

  monthLabel.textContent = `${{year}}/${{String(month).padStart(2, "0")}}`;

  if (!selectedDate) {{
    const today = new Date();
    selectedDate = `${{today.getFullYear()}}-${{String(today.getMonth() + 1).padStart(2, "0")}}-${{String(today.getDate()).padStart(2, "0")}}`;
  }}

  renderCalendarGrid(data.days, year, month);
  await loadCalendarItems(selectedDate);
}}

function renderCalendarGrid(days, year, month) {{
  calendarGrid.innerHTML = "";

  const firstDay = new Date(year, month - 1, 1);
  const startWeekday = firstDay.getDay();
  const lastDate = new Date(year, month, 0).getDate();
  const prevLastDate = new Date(year, month - 1, 0).getDate();

  let cells = [];

  for (let i = 0; i < startWeekday; i++) {{
    const d = prevLastDate - startWeekday + i + 1;
    cells.push({{
      day: d,
      date: "",
      has_items: false,
      other: true
    }});
  }}

  for (let d = 1; d <= lastDate; d++) {{
    const dateStr = `${{year}}-${{String(month).padStart(2, "0")}}-${{String(d).padStart(2, "0")}}`;
    const match = days.find(x => x.date === dateStr);
    cells.push({{
      day: d,
      date: dateStr,
      has_items: !!match?.has_items,
      other: false
    }});
  }}

  while (cells.length % 7 !== 0) {{
    cells.push({{
      day: "",
      date: "",
      has_items: false,
      other: true
    }});
  }}

  cells.forEach(cell => {{
    const btn = document.createElement("button");
    btn.className = "day-cell";
    if (cell.other) btn.classList.add("other");
    if (cell.date && cell.date === selectedDate) btn.classList.add("selected");
    btn.innerHTML = `<span>${{cell.day}}</span>${{cell.has_items ? '<span class="dot"></span>' : ''}}`;

    if (cell.date) {{
      btn.addEventListener("click", async () => {{
        selectedDate = cell.date;
        renderCalendarGrid(days, year, month);
        await loadCalendarItems(cell.date);
      }});
    }}

    calendarGrid.appendChild(btn);
  }});
}}

async function loadCalendarItems(dateStr) {{
  const data = await apiGet(`/api/reminders/by-date?date=${{dateStr}}`);
  renderReminderCards(calendarItems, data.items, "この日の予定はないよ。");
}}

function renderReminderCards(target, items, emptyText) {{
  if (!items.length) {{
    target.innerHTML = `<div class="empty">${{escapeHtml(emptyText)}}</div>`;
    return;
  }}

  target.innerHTML = items.map(item => `
    <div class="card">
      <button class="card-x" data-id="${{item.id}}" data-content="${{escapeHtml(item.content)}}">×</button>
      <div class="card-top">
        <span>${{escapeHtml(item.date || "")}}</span>
        <span>${{escapeHtml(item.weekday || "")}}</span>
        <span>${{escapeHtml(item.time || "")}}</span>
      </div>
      <div class="card-content">${{escapeHtml(item.content)}}</div>
    </div>
  `).join("");

  target.querySelectorAll(".card-x").forEach(btn => {{
    btn.addEventListener("click", () => {{
      openDeleteModal(Number(btn.dataset.id), btn.dataset.content);
    }});
  }});
}}

async function loadAllReminderViews() {{
  await loadSingleList();
  await loadRepeatList();
  await loadCalendar();
}}

async function boot() {{
  try {{
    await liff.init({{ liffId: LIFF_ID }});

    if (!liff.isLoggedIn()) {{
      liff.login();
      return;
    }}

    idToken = liff.getIDToken() || "";
    if (!idToken) {{
      throw new Error("IDトークンを取得できなかったよ。LIFFのscopeで openid を確認してね。");
    }}

    await loadAllReminderViews();
  }} catch (err) {{
    document.body.innerHTML = `
      <div style="padding:16px;font-family:sans-serif;">
        <h3>LIFFの読み込みに失敗したよ</h3>
        <pre style="white-space:pre-wrap;">${{escapeHtml(String(err))}}</pre>
      </div>
    `;
  }}
}}

boot();
</script>
</body>
</html>
"""


def build_wants_liff_html() -> str:
    return f"""
<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
  <title>ほしいもの一覧</title>
  <script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      background: #f5f6f8;
      color: #222;
    }}
    .wrap {{ padding: 14px 14px 24px; }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 12px;
    }}
    .title {{
      font-size: 20px;
      font-weight: 700;
    }}
    .close-btn {{
      width: 36px;
      height: 36px;
      border: none;
      border-radius: 18px;
      background: #e9ecef;
      font-size: 20px;
      cursor: pointer;
    }}
    .card {{
      position: relative;
      background: #fff;
      border-radius: 16px;
      padding: 14px 44px 14px 14px;
      margin-bottom: 12px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.06);
    }}
    .card-content {{
      font-size: 16px;
      line-height: 1.4;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .card-x {{
      position: absolute;
      top: 10px;
      right: 10px;
      width: 28px;
      height: 28px;
      border: none;
      border-radius: 14px;
      background: #f1f3f5;
      font-size: 16px;
      cursor: pointer;
    }}
    .empty {{
      padding: 18px;
      text-align: center;
      color: #666;
    }}
    .modal-backdrop {{
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.35);
      display: none;
      align-items: flex-end;
      justify-content: center;
      padding: 14px;
    }}
    .modal-backdrop.show {{ display: flex; }}
    .modal {{
      width: 100%;
      max-width: 420px;
      background: #fff;
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.18);
    }}
    .modal-text {{
      font-size: 16px;
      line-height: 1.5;
      margin-bottom: 14px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .modal-actions {{
      display: flex;
      justify-content: flex-end;
      gap: 10px;
    }}
    .btn {{
      border: none;
      border-radius: 12px;
      padding: 10px 16px;
      font-weight: 700;
      cursor: pointer;
    }}
    .btn-no {{ background: #e9ecef; }}
    .btn-yes {{ background: #12b85a; color: #fff; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="title">ほしいもの一覧</div>
      <button class="close-btn" id="closeBtn">×</button>
    </div>
    <div id="wantList"></div>
  </div>

  <div class="modal-backdrop" id="deleteModal">
    <div class="modal">
      <div class="modal-text" id="deleteText"></div>
      <div class="modal-actions">
        <button class="btn btn-no" id="deleteNo">いいえ</button>
        <button class="btn btn-yes" id="deleteYes">はい</button>
      </div>
    </div>
  </div>

<script>
const LIFF_ID = "{LIFF_WANT_ID}";
let idToken = "";
let deleteTargetId = null;

const wantList = document.getElementById("wantList");
const deleteModal = document.getElementById("deleteModal");
const deleteText = document.getElementById("deleteText");

document.getElementById("closeBtn").addEventListener("click", () => {{
  if (window.liff) liff.closeWindow();
}});

document.getElementById("deleteNo").addEventListener("click", () => {{
  deleteModal.classList.remove("show");
  deleteTargetId = null;
}});

document.getElementById("deleteYes").addEventListener("click", async () => {{
  if (!deleteTargetId) return;
  await fetch(`/api/wants/${{deleteTargetId}}`, {{
    method: "DELETE",
    headers: {{
      "x-line-id-token": idToken
    }}
  }});
  deleteModal.classList.remove("show");
  deleteTargetId = null;
  await loadWants();
}});

function openDeleteModal(id, content) {{
  deleteTargetId = id;
  deleteText.textContent = `「${{content}}」を削除しますか？`;
  deleteModal.classList.add("show");
}}

function escapeHtml(text) {{
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}}

function withAuth(url, options = {{}}) {{
  const headers = Object.assign({{}}, options.headers || {{}}, {{
    "x-line-id-token": idToken
  }});
  return fetch(url, Object.assign({{}}, options, {{ headers }}));
}}

async function apiGet(url) {{
  const res = await withAuth(url);
  if (!res.ok) throw new Error(await res.text());
  return await res.json();
}}

async function loadWants() {{
  const data = await apiGet("/api/wants");
  if (!data.items.length) {{
    wantList.innerHTML = '<div class="empty">ほしいものはまだないよ。</div>';
    return;
  }}

  wantList.innerHTML = data.items.map(item => `
    <div class="card">
      <button class="card-x" data-id="${{item.id}}" data-content="${{escapeHtml(item.content)}}">×</button>
      <div class="card-content">${{escapeHtml(item.content)}}</div>
    </div>
  `).join("");

  wantList.querySelectorAll(".card-x").forEach(btn => {{
    btn.addEventListener("click", () => {{
      openDeleteModal(Number(btn.dataset.id), btn.dataset.content);
    }});
  }});
}}

async function boot() {{
  try {{
    await liff.init({{ liffId: LIFF_ID }});

    if (!liff.isLoggedIn()) {{
      liff.login();
      return;
    }}

    idToken = liff.getIDToken() || "";
    if (!idToken) {{
      throw new Error("IDトークンを取得できなかったよ。LIFFのscopeで openid を確認してね。");
    }}

    await loadWants();
  }} catch (err) {{
    document.body.innerHTML = `
      <div style="padding:16px;font-family:sans-serif;">
        <h3>LIFFの読み込みに失敗したよ</h3>
        <pre style="white-space:pre-wrap;">${{escapeHtml(String(err))}}</pre>
      </div>
    `;
  }}
}}

boot();
</script>
</body>
</html>
"""


def build_backups_liff_html() -> str:
    return f"""
<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
  <title>バックアップ</title>
  <script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      background: #f5f6f8;
      color: #222;
    }}
    .wrap {{ padding: 14px 14px 24px; }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 12px;
    }}
    .title {{
      font-size: 20px;
      font-weight: 700;
    }}
    .close-btn {{
      width: 36px;
      height: 36px;
      border: none;
      border-radius: 18px;
      background: #e9ecef;
      font-size: 20px;
      cursor: pointer;
    }}
    .action-card, .card {{
      background: #fff;
      border-radius: 16px;
      padding: 14px;
      margin-bottom: 12px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.06);
    }}
    .save-btn, .restore-btn {{
      width: 100%;
      border: none;
      border-radius: 12px;
      padding: 12px 14px;
      font-weight: 700;
      cursor: pointer;
      background: #12b85a;
      color: #fff;
      font-size: 15px;
    }}
    .restore-btn {{
      width: auto;
      min-width: 96px;
      padding: 10px 14px;
    }}
    .sub {{
      font-size: 13px;
      color: #666;
      margin-top: 8px;
    }}
    .latest {{
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 10px;
    }}
    .card-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }}
    .card-title {{
      font-size: 15px;
      font-weight: 700;
      line-height: 1.4;
      word-break: break-word;
    }}
    .empty {{
      padding: 18px;
      text-align: center;
      color: #666;
      background: #fff;
      border-radius: 16px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.06);
    }}
    .toast {{
      position: fixed;
      left: 50%;
      bottom: 18px;
      transform: translateX(-50%);
      background: rgba(34,34,34,0.92);
      color: #fff;
      padding: 10px 14px;
      border-radius: 999px;
      font-size: 13px;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s ease;
      z-index: 1000;
    }}
    .toast.show {{ opacity: 1; }}
    .modal-backdrop {{
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.35);
      display: none;
      align-items: flex-end;
      justify-content: center;
      padding: 14px;
    }}
    .modal-backdrop.show {{ display: flex; }}
    .modal {{
      width: 100%;
      max-width: 420px;
      background: #fff;
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.18);
    }}
    .modal-text {{
      font-size: 16px;
      line-height: 1.5;
      margin-bottom: 14px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .modal-actions {{
      display: flex;
      justify-content: flex-end;
      gap: 10px;
    }}
    .btn {{
      border: none;
      border-radius: 12px;
      padding: 10px 16px;
      font-weight: 700;
      cursor: pointer;
    }}
    .btn-no {{ background: #e9ecef; }}
    .btn-yes {{ background: #12b85a; color: #fff; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="title">バックアップ</div>
      <button class="close-btn" id="closeBtn">×</button>
    </div>

    <div class="action-card">
      <button class="save-btn" id="saveBtn">保存する</button>
      <div class="sub">最大5件まで保存。6件目は一番古い保存を上書きするよ。</div>
    </div>

    <div class="latest" id="latestText">最終保存日時: なし</div>
    <div id="backupList"></div>
  </div>

  <div class="modal-backdrop" id="restoreModal">
    <div class="modal">
      <div class="modal-text" id="restoreText"></div>
      <div class="modal-actions">
        <button class="btn btn-no" id="restoreNo">キャンセル</button>
        <button class="btn btn-yes" id="restoreYes">復元する</button>
      </div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

<script>
const LIFF_ID = "{LIFF_BACKUP_ID}";
let idToken = "";
let restoreTargetId = null;

const backupList = document.getElementById("backupList");
const latestText = document.getElementById("latestText");
const restoreModal = document.getElementById("restoreModal");
const restoreText = document.getElementById("restoreText");
const toast = document.getElementById("toast");

document.getElementById("closeBtn").addEventListener("click", () => {{
  if (window.liff) liff.closeWindow();
}});

document.getElementById("saveBtn").addEventListener("click", async () => {{
  const res = await fetch("/api/backups/save", {{
    method: "POST",
    headers: {{ "x-line-id-token": idToken }}
  }});
  const data = await res.json();

  if (data.status === "skip") {{
    showToast("保存できるデータがまだないよ。", 2200);
    return;
  }}

  showToast("保存成功", 1800);
  await loadBackups();
}});

document.getElementById("restoreNo").addEventListener("click", () => {{
  restoreModal.classList.remove("show");
  restoreTargetId = null;
}});

document.getElementById("restoreYes").addEventListener("click", async () => {{
  if (!restoreTargetId) return;

  const res = await fetch(`/api/backups/restore/${{restoreTargetId}}`, {{
    method: "POST",
    headers: {{ "x-line-id-token": idToken }}
  }});
  const data = await res.json();

  restoreModal.classList.remove("show");
  restoreTargetId = null;

  if (data.ok) {{
    showToast("復元成功", 1800);
  }} else {{
    showToast("復元に失敗したよ。", 2200);
  }}
}});

function escapeHtml(text) {{
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}}

function showToast(message, ms) {{
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => {{
    toast.classList.remove("show");
  }}, ms);
}}

function formatBackupDate(iso) {{
  const d = new Date(iso);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${{y}}/${{m}}/${{day}} ${{hh}}:${{mm}}`;
}}

function openRestoreModal(id, createdAt) {{
  restoreTargetId = id;
  restoreText.textContent = `この保存データ（${{formatBackupDate(createdAt)}}）を復元しますか？
現在のリマインドとほしいものはこの内容で置き換わるよ。`;
  restoreModal.classList.add("show");
}}

async function loadBackups() {{
  const res = await fetch("/api/backups", {{
    headers: {{ "x-line-id-token": idToken }}
  }});
  const data = await res.json();

  if (!data.items.length) {{
    latestText.textContent = "最終保存日時: なし";
    backupList.innerHTML = '<div class="empty">まだ保存データはないよ。</div>';
    return;
  }}

  latestText.textContent = `最終保存日時: ${{formatBackupDate(data.items[0].created_at)}}`;

  backupList.innerHTML = data.items.map(item => `
    <div class="card">
      <div class="card-head">
        <div class="card-title">${{escapeHtml(formatBackupDate(item.created_at))}}</div>
        <button class="restore-btn" data-id="${{item.id}}" data-created-at="${{item.created_at}}">復元</button>
      </div>
    </div>
  `).join("");

  backupList.querySelectorAll(".restore-btn").forEach(btn => {{
    btn.addEventListener("click", () => {{
      openRestoreModal(Number(btn.dataset.id), btn.dataset.createdAt);
    }});
  }});
}}

async function boot() {{
  try {{
    await liff.init({{ liffId: LIFF_ID }});

    if (!liff.isLoggedIn()) {{
      liff.login();
      return;
    }}

    idToken = liff.getIDToken() || "";
    if (!idToken) {{
      throw new Error("IDトークンを取得できなかったよ。LIFFのscopeで openid を確認してね。");
    }}

    await loadBackups();
  }} catch (err) {{
    document.body.innerHTML = `
      <div style="padding:16px;font-family:sans-serif;">
        <h3>LIFFの読み込みに失敗したよ</h3>
        <pre style="white-space:pre-wrap;">${{escapeHtml(String(err))}}</pre>
      </div>
    `;
  }}
}}

boot();
</script>
</body>
</html>
"""


def handle_text_message(user_id: str, text: str, reply_token: str):
    state = get_state(user_id)

    if not is_authorized_user(user_id):
        locked, _locked_until = is_auth_locked(user_id)

        if locked:
            send_reply(reply_token, [
                text_message("入力を制限中だよ。しばらくしてからもう一度試してね。")
            ])
            return

        if state["state"] != STATE_WAITING_PASSWORD:
            set_state(user_id, STATE_WAITING_PASSWORD, None)
            send_reply(reply_token, [
                text_message("このBotを使うにはパスワードを入力してね。")
            ])
            return

        if text.strip() == BOT_PASSWORD:
            authorize_user(user_id)
            reset_auth_attempt(user_id)
            reset_state(user_id)
            send_reply(reply_token, [
                text_message("認証できたよ！使えるようになったよ。"),
                main_menu_message()
            ])
            return

        failed_count, locked_until = register_auth_failure(user_id)

        if locked_until:
            send_reply(reply_token, [
                text_message("3回間違えたのでロックしたよ。しばらくしてから試してね。")
            ])
            return

        remain = 3 - failed_count
        send_reply(reply_token, [
            text_message(f"パスワードが違うよ。あと {remain} 回でロックされるよ。")
        ])
        return

    if should_timeout(state["updated_at"]) and state["state"] != STATE_NONE:
        reset_state(user_id)
        state = get_state(user_id)

    if text == "キャンセル":
        reset_state(user_id)
        send_reply(reply_token, [text_message("キャンセルしたよ！"), main_menu_message()])
        return

    if text == "メニュー":
        reset_state(user_id)
        send_reply(reply_token, [main_menu_message()])
        return
    
    if text == "Coming Soon":
        reset_state(user_id)
        send_reply(reply_token, [
            text_message("ここは準備中だよ。"),
            main_menu_message()
        ])
        return

    if text == "保存復元":
        reset_state(user_id)
        send_reply(reply_token, [
            text_message("どっちにする？", quick_reply=save_restore_quick_reply())
        ])
        return

    if text == "保存":
        reset_state(user_id)
        status = save_backup(user_id)
        if status == "skip":
            send_reply(reply_token, [
                text_message("保存できるデータがまだないよ。"),
                main_menu_message()
            ])
        else:
            send_reply(reply_token, [
                text_message("保存したよ！"),
                main_menu_message()
            ])
        return

    if text == "復元":
        reset_state(user_id)
        send_reply(reply_token, [
            text_message("バックアップ一覧を開くよ", quick_reply=QuickReply(items=[
                QuickReplyItem(action=URIAction(label="開く", uri=LIFF_BACKUP_URL)),
                QuickReplyItem(action=MessageAction(label="メニューに戻る", text="メニュー")),
            ]))
        ])
        return

    if text == "リマインド":
        reset_state(user_id)
        send_reply(reply_token, [text_message("リマインド", quick_reply=liff_quick_reply("reminder"))])
        return

    if text == "ほしいもの":
        reset_state(user_id)
        send_reply(reply_token, [text_message("ほしいもの", quick_reply=liff_quick_reply("want"))])
        return

    if text == "リマインド一覧":
        send_reply(reply_token, [text_message("一覧は下の「一覧」から開いてね。", quick_reply=liff_quick_reply("reminder"))])
        return

    if text == "ほしいもの一覧":
        send_reply(reply_token, [text_message("一覧は下の「一覧」から開いてね。", quick_reply=liff_quick_reply("want"))])
        return

    if text == "リマインド追加":
        set_state(user_id, STATE_WAITING_REMINDER_CONTENT, None)
        send_reply(reply_token, [
            text_message(
                "通知したい内容を送ってね。\n例：ランチ",
                quick_reply=cancel_quick_reply()
            )
        ])
        return

    if text == "ほしいもの追加":
        set_state(user_id, STATE_WAITING_WANT_CONTENT, None)
        send_reply(reply_token, [
            text_message(
                "ほしいものを送ってね。\n例：イヤホン",
                quick_reply=cancel_quick_reply()
            )
        ])
        return

    if state["state"] == STATE_WAITING_REMINDER_CONTENT:
        content = text.strip()
        set_state(user_id, STATE_WAITING_REMINDER_DATETIME, content)
        send_reply(reply_token, [text_message(
            f"「{content}」だね！覚えたよ！\n"
            "いつ教えてほしい？\n"
            "\n"
            "次の形式で送ってね。\n"
            "・明日 10:00\n"
            "・水曜日 08:00\n"
            "・今週日曜日 15:00\n"
            "・来週金曜日 15:00\n"
            "・再来週火曜日 18:30\n"
            "・10時\n"
            "・10時30分\n"
            "・3分後\n"
            "・YYYYMMDD HH:MM\n"
            "・M/D HH:MM",
            quick_reply=cancel_quick_reply()
        )])
        return

    if state["state"] == STATE_WAITING_REMINDER_DATETIME:
        parsed = parse_datetime_input(text.strip())

        if parsed and parsed.get("error") == "time_required":
            send_reply(reply_token, [text_message(
                "時刻もいっしょに送ってね。\n"
                "例：\n"
                "・明日 10:00\n"
                "・今週日曜日 15:00\n"
                "・来週金曜日 15:00\n"
                "・再来週火曜日 18:30\n"
                "・YYYYMMDD HH:MM\n"
                "・M/D HH:MM",
                quick_reply=cancel_quick_reply()
            )])
            return

        if parsed and parsed.get("error") == "past_datetime":
            send_reply(reply_token, [text_message(
                "その日時はもう過ぎているよ。\n"
                "別の日時を送ってね。\n"
                "例：\n"
                "・今日 18:00\n"
                "・明日 10:00\n"
                "・今週日曜日 15:00\n"
                "・来週金曜日 15:00\n"
                "・再来週火曜日 18:30",
                quick_reply=cancel_quick_reply()
            )])
            return

        if not parsed:
            send_reply(reply_token, [text_message(
                "日時がうまく読めなかったよ。\n"
                "\n"
                "次の形式で送ってね。\n"
                "・明日 10:00\n"
                "・水曜日 08:00\n"
                "・今週日曜日 15:00\n"
                "・来週金曜日 15:00\n"
                "・再来週火曜日 18:30\n"
                "・10時\n"
                "・10時30分\n"
                "・3分後\n"
                "・YYYYMMDD HH:MM\n"
                "・M/D HH:MM",
                quick_reply=cancel_quick_reply()
            )])
            return

        content = state["temp_content"] or "無題"
        create_reminder(user_id, content, parsed)
        reset_state(user_id)

        if parsed["kind"] == "single":
            dt_text = format_single_datetime_jp(parsed["scheduled_at"])
            msg = f"OK！「{content}」を {dt_text} に通知するね！"
        else:
            weekly_text = format_weekly_label(parsed["weekday"], parsed["time_hhmm"])
            msg = f"OK！「{content}」を {weekly_text} に通知するね！"

        send_reply(reply_token, [text_message(msg), text_message("リマインド", quick_reply=liff_quick_reply("reminder"))])
        return

    if state["state"] == STATE_WAITING_WANT_CONTENT:
        add_want(user_id, text.strip())
        reset_state(user_id)
        send_reply(reply_token, [
            text_message(f"OK！「{text.strip()}」を追加したよ！"),
            text_message("ほしいもの", quick_reply=liff_quick_reply("want"))
        ])
        return

    send_reply(reply_token, [
        text_message("「メニュー」を押すか送ってね。"),
        main_menu_message()
    ])


@app.on_event("startup")
def startup():
    init_db()


@app.on_event("shutdown")
def shutdown():
    return


@app.get("/")
def healthcheck():
    return {"ok": True}


@app.get("/liff/reminders", response_class=HTMLResponse)
def liff_reminders():
    return HTMLResponse(build_reminders_liff_html())


@app.get("/liff/wants", response_class=HTMLResponse)
def liff_wants():
    return HTMLResponse(build_wants_liff_html())


@app.get("/liff/backups", response_class=HTMLResponse)
def liff_backups():
    return HTMLResponse(build_backups_liff_html())


@app.get("/api/reminders/one-time")
def api_reminders_one_time(x_line_id_token: str | None = Header(default=None)):
    user_id = get_user_id_from_verified_id_token(x_line_id_token)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, content, kind, scheduled_at, weekday, time_hhmm
            FROM reminders
            WHERE user_id = %s AND kind = 'single'
            ORDER BY CAST(scheduled_at AS timestamptz) ASC
            """,
            (user_id,)
        ).fetchall()
    return {"items": [reminder_row_to_card(x) for x in rows]}


@app.get("/api/reminders/repeat")
def api_reminders_repeat(x_line_id_token: str | None = Header(default=None)):
    user_id = get_user_id_from_verified_id_token(x_line_id_token)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, content, kind, scheduled_at, weekday, time_hhmm
            FROM reminders
            WHERE user_id = %s AND kind <> 'single'
            ORDER BY created_at ASC
            """,
            (user_id,)
        ).fetchall()
    return {"items": [reminder_row_to_card(x) for x in rows]}


@app.get("/api/reminders/calendar")
def api_reminders_calendar(year: int, month: int, x_line_id_token: str | None = Header(default=None)):
    user_id = get_user_id_from_verified_id_token(x_line_id_token)
    start_date = date(year, month, 1)
    if month == 12:
        next_date = date(year + 1, 1, 1)
    else:
        next_date = date(year, month + 1, 1)

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT scheduled_at
            FROM reminders
            WHERE user_id = %s
              AND kind = 'single'
              AND CAST(scheduled_at AS timestamptz) >= CAST(%s AS timestamptz)
              AND CAST(scheduled_at AS timestamptz) < CAST(%s AS timestamptz)
            """,
            (
                user_id,
                datetime.combine(start_date, time(0, 0), tzinfo=TZ).isoformat(),
                datetime.combine(next_date, time(0, 0), tzinfo=TZ).isoformat(),
            )
        ).fetchall()

    day_map = {}
    for row in rows:
        d = datetime.fromisoformat(row["scheduled_at"]).astimezone(TZ).date().isoformat()
        day_map[d] = True

    return {
        "days": [{"date": k, "has_items": True} for k in sorted(day_map.keys())]
    }


@app.get("/api/reminders/by-date")
def api_reminders_by_date(date: str, x_line_id_token: str | None = Header(default=None)):
    user_id = get_user_id_from_verified_id_token(x_line_id_token)
    start_dt = datetime.fromisoformat(f"{date}T00:00:00+09:00")
    end_dt = datetime.fromisoformat(f"{date}T23:59:59+09:00")

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, content, kind, scheduled_at, weekday, time_hhmm
            FROM reminders
            WHERE user_id = %s
              AND kind = 'single'
              AND CAST(scheduled_at AS timestamptz) >= CAST(%s AS timestamptz)
              AND CAST(scheduled_at AS timestamptz) <= CAST(%s AS timestamptz)
            ORDER BY CAST(scheduled_at AS timestamptz) ASC
            """,
            (user_id, start_dt.isoformat(), end_dt.isoformat())
        ).fetchall()
    return {"items": [reminder_row_to_card(x) for x in rows]}


@app.delete("/api/reminders/{reminder_id}")
def api_delete_reminder(reminder_id: int, x_line_id_token: str | None = Header(default=None)):
    user_id = get_user_id_from_verified_id_token(x_line_id_token)
    ok = delete_reminder_by_id(user_id, reminder_id)
    return {"ok": ok}


@app.get("/api/wants")
def api_wants(x_line_id_token: str | None = Header(default=None)):
    user_id = get_user_id_from_verified_id_token(x_line_id_token)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, content
            FROM wants
            WHERE user_id = %s
            ORDER BY created_at DESC
            """,
            (user_id,)
        ).fetchall()
    return {"items": [wants_row_to_card(x) for x in rows]}


@app.delete("/api/wants/{want_id}")
def api_delete_want(want_id: int, x_line_id_token: str | None = Header(default=None)):
    user_id = get_user_id_from_verified_id_token(x_line_id_token)
    ok = delete_want_by_id(user_id, want_id)
    return {"ok": ok}


@app.post("/api/backups/save")
def api_save_backup(x_line_id_token: str | None = Header(default=None)):
    user_id = get_user_id_from_verified_id_token(x_line_id_token)
    return {"status": save_backup(user_id)}


@app.get("/api/backups")
def api_backups(x_line_id_token: str | None = Header(default=None)):
    user_id = get_user_id_from_verified_id_token(x_line_id_token)
    return {"items": list_backups(user_id)}


@app.post("/api/backups/restore/{backup_id}")
def api_restore_backup(backup_id: int, x_line_id_token: str | None = Header(default=None)):
    user_id = get_user_id_from_verified_id_token(x_line_id_token)
    return {"ok": restore_backup(user_id, backup_id)}


@app.post("/jobs/send-due-notifications")
async def run_send_due_notifications(x_cron_secret: str = Header(None)):
    if x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    result = send_due_notifications()
    return JSONResponse(result)


@app.post("/callback")
async def callback(request: Request, x_line_signature: str = Header(None)):
    if not x_line_signature:
        raise HTTPException(status_code=400, detail="X-Line-Signature がありません。")

    body = await request.body()
    body_text = body.decode("utf-8")

    try:
        events = parser.parse(body_text, x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="署名が不正です。")

    for event in events:
        if isinstance(event, FollowEvent):
            user_id = getattr(event.source, "user_id", None)
            if user_id and event.reply_token:
                unauthorize_user(user_id)
                reset_auth_attempt(user_id)
                set_state(user_id, STATE_WAITING_PASSWORD, None)
                send_reply(event.reply_token, [
                    text_message("友だち追加ありがとう！\nこのBotを使うにはパスワードを入力してね。")
                ])

        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            user_id = getattr(event.source, "user_id", None)
            if not user_id:
                continue
            handle_text_message(user_id, event.message.text, event.reply_token)

    return JSONResponse({"ok": True})