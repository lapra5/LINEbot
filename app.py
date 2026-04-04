import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler

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
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent

load_dotenv()

TZ = ZoneInfo(os.getenv("TZ", "Asia/Tokyo"))
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
BOT_PASSWORD = os.getenv("BOT_PASSWORD", "")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET or not BOT_PASSWORD:
    raise RuntimeError(
        "環境変数 LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET / BOT_PASSWORD を設定してください。"
    )

app = FastAPI()
parser = WebhookParser(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
scheduler = BackgroundScheduler(timezone=str(TZ))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot.db")

STATE_NONE = "none"
STATE_WAITING_PASSWORD = "waiting_password"
STATE_WAITING_REMINDER_CONTENT = "waiting_reminder_content"
STATE_WAITING_REMINDER_DATETIME = "waiting_reminder_datetime"
STATE_WAITING_REMINDER_DELETE = "waiting_reminder_delete"
STATE_WAITING_WANT_CONTENT = "waiting_want_content"
STATE_WAITING_WANT_DELETE = "waiting_want_delete"

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            kind TEXT NOT NULL,                  -- single / weekly
            scheduled_at TEXT,                   -- 単発予定: ISO datetime
            weekday INTEGER,                     -- 繰り返し予定: 0=月 ... 6=日
            time_hhmm TEXT NOT NULL,             -- HH:MM
            last_day_notice_date TEXT,           -- YYYY-MM-DD
            last_1h_notice_date TEXT,            -- YYYY-MM-DD
            last_10m_notice_date TEXT,           -- YYYY-MM-DD
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS wants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
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


def get_state(user_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, state, temp_content, updated_at FROM user_states WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row:
            return dict(row)

        current = {
            "user_id": user_id,
            "state": STATE_NONE,
            "temp_content": None,
            "updated_at": now_jst().isoformat()
        }
        conn.execute(
            "INSERT OR REPLACE INTO user_states (user_id, state, temp_content, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, current["state"], current["temp_content"], current["updated_at"])
        )
        return current


def set_state(user_id: str, state: str, temp_content: str | None = None):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_states (user_id, state, temp_content, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                state = excluded.state,
                temp_content = excluded.temp_content,
                updated_at = excluded.updated_at
            """,
            (user_id, state, temp_content, now_jst().isoformat())
        )


def reset_state(user_id: str):
    set_state(user_id, STATE_NONE, None)

def is_authorized_user(user_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM authorized_users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        return row is not None


def authorize_user(user_id: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO authorized_users (user_id, authorized_at)
            VALUES (?, ?)
            """,
            (user_id, now_jst().isoformat())
        )

def get_auth_attempt(user_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT user_id, failed_count, locked_until, updated_at
            FROM auth_attempts
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

        if row:
            return dict(row)

        current = {
            "user_id": user_id,
            "failed_count": 0,
            "locked_until": None,
            "updated_at": now_jst().isoformat(),
        }
        conn.execute(
            """
            INSERT INTO auth_attempts (user_id, failed_count, locked_until, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, 0, None, now_jst().isoformat())
        )
        return current


def reset_auth_attempt(user_id: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO auth_attempts (user_id, failed_count, locked_until, updated_at)
            VALUES (?, 0, NULL, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                failed_count = 0,
                locked_until = NULL,
                updated_at = excluded.updated_at
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
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                failed_count = excluded.failed_count,
                locked_until = excluded.locked_until,
                updated_at = excluded.updated_at
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


def text_message(text: str) -> TextMessage:
    return TextMessage(text=text)

def safe_date_str(y: int, m: int, d: int) -> str:
    # ゼロ幅スペースでリンク防止
    zw = "\u200b"
    return f"{y}-{zw}{m:02d}-{zw}{d:02d}"


def format_single_datetime_jp(dt: datetime) -> str:
    dt = dt.astimezone(TZ)
    safe_date = safe_date_str(dt.year, dt.month, dt.day)
    return f"{safe_date}（{JP_WEEK_FULL[dt.weekday()]}） {dt.strftime('%H:%M')}"


def format_weekly_label(weekday: int, hhmm: str) -> str:
    return f"毎週 {JP_WEEK_FULL[weekday]} {hhmm}"


def main_menu_message() -> FlexMessage:
    bubble = FlexBubble(
        body=FlexBox(
            layout="vertical",
            spacing="md",
            contents=[
                FlexText(text="メニュー", weight="bold", size="xl"),
                FlexSeparator(),
                FlexBox(
                    layout="horizontal",
                    spacing="sm",
                    contents=[
                        menu_button("リマインド", "リマインド"),
                        menu_button("ほしいもの", "ほしいもの"),
                    ]
                ),
                FlexBox(
                    layout="horizontal",
                    spacing="sm",
                    contents=[
                        menu_button("Coming Soon", "Coming Soon"),
                        menu_button("Coming Soon", "Coming Soon"),
                    ]
                ),
            ]
        )
    )
    return FlexMessage(
        alt_text="メニュー",
        contents=FlexContainer.from_json(json.dumps(bubble.to_dict()))
    )


def reminder_menu_message() -> FlexMessage:
    bubble = FlexBubble(
        body=FlexBox(
            layout="vertical",
            spacing="md",
            contents=[
                FlexText(text="リマインド", weight="bold", size="xl"),
                FlexBox(
                    layout="vertical",
                    spacing="sm",
                    contents=[
                        full_button("追加", "リマインド追加"),
                        full_button("一覧", "リマインド一覧"),
                        full_button("削除", "リマインド削除"),
                        full_button("メニューに戻る", "メニュー"),
                    ]
                ),
            ]
        )
    )
    return FlexMessage(
        alt_text="リマインドメニュー",
        contents=FlexContainer.from_json(json.dumps(bubble.to_dict()))
    )


def want_menu_message() -> FlexMessage:
    bubble = FlexBubble(
        body=FlexBox(
            layout="vertical",
            spacing="md",
            contents=[
                FlexText(text="ほしいもの", weight="bold", size="xl"),
                FlexBox(
                    layout="vertical",
                    spacing="sm",
                    contents=[
                        full_button("追加", "ほしいもの追加"),
                        full_button("一覧", "ほしいもの一覧"),
                        full_button("削除", "ほしいもの削除"),
                        full_button("メニューに戻る", "メニュー"),
                    ]
                ),
            ]
        )
    )
    return FlexMessage(
        alt_text="ほしいものメニュー",
        contents=FlexContainer.from_json(json.dumps(bubble.to_dict()))
    )


def menu_button(label: str, text: str) -> FlexBox:
    return FlexBox(
        layout="vertical",
        flex=1,
        padding_all="10px",
        background_color="#F5F5F5",
        corner_radius="md",
        contents=[
            FlexButton(
                style="primary" if "Coming Soon" not in label else "secondary",
                action=MessageAction(label=label, text=text)
            )
        ]
    )


def full_button(label: str, text: str) -> FlexButton:
    return FlexButton(
        style="primary",
        action=MessageAction(label=label, text=text)
    )


def normalize_digits(text: str) -> str:
    return text.translate(str.maketrans("０１２３４５６７８９：", "0123456789:"))


def parse_time_hhmm(s: str) -> str | None:
    s = normalize_digits(s)

    # 10:30
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"

    # 10時 / 10時30分
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
    now = now_jst()

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

def parse_datetime_input(text: str) -> dict | None:
    """
    戻り値:
    単発:
      {"kind":"single", "scheduled_at": datetime, "time_hhmm":"HH:MM"}
    毎週:
      {"kind":"weekly", "weekday": 0-6, "time_hhmm":"HH:MM"}
    エラー:
      {"error":"time_required"}
    """
    s = text.strip()
    s = normalize_digits(text.strip())
    now = now_jst()

    # 0) 3分後 / 2時間後
    relative_dt = parse_relative_datetime(s)
    if relative_dt:
        return {
            "kind": "single",
            "scheduled_at": relative_dt,
            "time_hhmm": relative_dt.strftime("%H:%M")
        }

    # 0-2) 10時 / 10時30分
    time_only = parse_time_only_datetime(s)
    if time_only and not any(word in s for word in ["今日", "明日", "明後日", "来週", "月", "火", "水", "木", "金", "土", "日", "/"]):
        dt, hhmm = time_only
        return {
            "kind": "single",
            "scheduled_at": dt,
            "time_hhmm": hhmm
        }

    def need_time() -> dict:
        return {"error": "time_required"}

    # 1) 来週 + 曜日 + 時刻
    m = re.search(r"来週\s*(月曜?日?|火曜?日?|水曜?日?|木曜?日?|金曜?日?|土曜?日?|日曜?日?)", s)
    if m:
        wd_text = m.group(1)
        weekday = WEEKDAY_MAP.get(wd_text)
        if weekday is not None:
            hhmm = require_time_hhmm(s)
            if not hhmm:
                return need_time()
            current_weekday = now.weekday()
            days_until_next_monday = 7 - current_weekday
            next_monday = (now + timedelta(days=days_until_next_monday)).date()
            target_date = next_monday + timedelta(days=weekday)
            hh, mm = map(int, hhmm.split(":"))
            dt = datetime.combine(target_date, time(hh, mm), tzinfo=TZ)
            return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    # 2) 今日 / 明日 / 明後日 + 時刻
    for key, add_days in [("今日", 0), ("明日", 1), ("明後日", 2)]:
        if key in s:
            hhmm = require_time_hhmm(s)
            if not hhmm:
                return need_time()
            target_date = (now + timedelta(days=add_days)).date()
            hh, mm = map(int, hhmm.split(":"))
            dt = datetime.combine(target_date, time(hh, mm), tzinfo=TZ)
            return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    # 3) YYYYMMDD
    m = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", s)
    if m:
        y, mo, d = map(int, m.groups())
        hhmm = require_time_hhmm(s)
        if not hhmm:
            return need_time()
        hh, mm = map(int, hhmm.split(":"))
        dt = datetime(y, mo, d, hh, mm, tzinfo=TZ)
        return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    # 4) YYYY-MM-DD
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        y, mo, d = map(int, m.groups())
        hhmm = require_time_hhmm(s)
        if not hhmm:
            return need_time()
        hh, mm = map(int, hhmm.split(":"))
        dt = datetime(y, mo, d, hh, mm, tzinfo=TZ)
        return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    # 5) M/D
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

    # 6) 曜日 + 時刻 => 毎週繰り返し
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

    # 7) 来週のみ => 来週の今日
    if "来週" in s:
        hhmm = require_time_hhmm(s)
        if not hhmm:
            return need_time()
        target_date = (now + timedelta(days=7)).date()
        hh, mm = map(int, hhmm.split(":"))
        dt = datetime.combine(target_date, time(hh, mm), tzinfo=TZ)
        return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    return None

def create_reminder(user_id: str, content: str, parsed: dict):
    with get_conn() as conn:
        if parsed["kind"] == "single":
            conn.execute(
                """
                INSERT INTO reminders (
                    user_id, content, kind, scheduled_at, weekday, time_hhmm,
                    last_day_notice_date, last_1h_notice_date, last_10m_notice_date, created_at
                )
                VALUES (?, ?, 'single', ?, NULL, ?, NULL, NULL, NULL, ?)
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
                    last_day_notice_date, last_1h_notice_date, last_10m_notice_date, created_at
                )
                VALUES (?, ?, 'weekly', NULL, ?, ?, NULL, NULL, NULL, ?)
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
        conn.execute(
            "INSERT INTO wants (user_id, content, created_at) VALUES (?, ?, ?)",
            (user_id, content, now_jst().isoformat())
        )

def delete_reminder_by_id(user_id: str, reminder_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id)
        )
        return cur.rowcount > 0


def delete_want_by_id(user_id: str, want_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM wants WHERE id = ? AND user_id = ?",
            (want_id, user_id)
        )
        return cur.rowcount > 0

def list_reminders_text(user_id: str) -> str:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, content, kind, scheduled_at, weekday, time_hhmm
            FROM reminders
            WHERE user_id = ?
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
            WHERE user_id = ?
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


def handle_text_message(user_id: str, text: str, reply_token: str):
    state = get_state(user_id)

    # =========================
    # 未認証チェック（最優先）
    # =========================
    if not is_authorized_user(user_id):
        locked, locked_until = is_auth_locked(user_id)

        if locked:
            until_text = locked_until.strftime("%H:%M")
            send_reply(reply_token, [
                text_message(f"入力を制限中だよ。しばらくしてからもう一度試してね。")
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
            until_text = datetime.fromisoformat(locked_until).strftime("%H:%M")
            send_reply(reply_token, [
                text_message(f"3回間違えたのでロックしたよ。しばらくしてから試してね。")
            ])
            return

        remain = 3 - failed_count
        send_reply(reply_token, [
            text_message(f"パスワードが違うよ。あと {remain} 回でロックされるよ。")
        ])
        return
    
    # =========================
    # 状態タイムアウト
    # =========================
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
        send_reply(reply_token, [text_message("ここは準備中だよ。")])
        return

    if text == "リマインド":
        reset_state(user_id)
        send_reply(reply_token, [reminder_menu_message()])
        return

    if text == "ほしいもの":
        reset_state(user_id)
        send_reply(reply_token, [want_menu_message()])
        return

    if text == "リマインド一覧":
        send_reply(reply_token, [text_message(list_reminders_text(user_id)), reminder_menu_message()])
        return

    if text == "ほしいもの一覧":
        send_reply(reply_token, [text_message(list_wants_text(user_id)), want_menu_message()])
        return
    
    if text == "リマインド削除":
        reminder_text = list_reminders_text(user_id)
        if reminder_text == "リマインダーはまだないよ。":
            send_reply(reply_token, [text_message(reminder_text), reminder_menu_message()])
            return
        set_state(user_id, STATE_WAITING_REMINDER_DELETE, None)
        send_reply(reply_token, [
            text_message(reminder_text),
            text_message("削除したいリマインダーのIDを送ってね。\nやめるときは「キャンセル」")
        ])
        return

    if text == "ほしいもの削除":
        want_text = list_wants_text(user_id)
        if want_text == "ほしいものはまだないよ。":
            send_reply(reply_token, [text_message(want_text), want_menu_message()])
            return
        set_state(user_id, STATE_WAITING_WANT_DELETE, None)
        send_reply(reply_token, [
            text_message(want_text),
            text_message("削除したいほしいもののIDを送ってね。\nやめるときは「キャンセル」")
        ])
        return

    if text == "リマインド追加":
        set_state(user_id, STATE_WAITING_REMINDER_CONTENT, None)
        send_reply(reply_token, [text_message("通知したい内容を送ってね。\n例：ランチ\n\nやめるときは「キャンセル」")])
        return

    if text == "ほしいもの追加":
        set_state(user_id, STATE_WAITING_WANT_CONTENT, None)
        send_reply(reply_token, [text_message("ほしいものを送ってね。\n例：イヤホン\n\nやめるときは「キャンセル」")])
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
            "・来週金曜日 15:00\n"
            "・10時\n"
            "・10時30分\n"
            "・3分後\n"
            "・YYYYMMDD HH:MM\n"
            "・M/D HH:MM\n"
            "\n"
            "やめるときは「キャンセル」"
        )])
        return

    if state["state"] == STATE_WAITING_REMINDER_DATETIME:
        parsed = parse_datetime_input(text.strip())

        if parsed and parsed.get("error") == "time_required":
            send_reply(reply_token, [text_message(
            "時刻もいっしょに送ってね。\n"
            "例：\n"
            "・明日 10:00\n"
            "・水曜日 08:00\n"
            "・来週金曜日 15:00\n"
            "・YYYYMMDD HH:MM\n"
            "・M/D HH:MM\n"
            "やめるときは「キャンセル」"
            )])
            return

        if not parsed:
            send_reply(reply_token, [text_message(
            "日時がうまく読めなかったよ。\n"
            "\n"
            "次の形式で送ってね。\n"
            "・明日 10:00\n"
            "・水曜日 08:00\n"
            "・来週金曜日 15:00\n"
            "・10時\n"
            "・10時30分\n"
            "・3分後\n"
            "・YYYYMMDD HH:MM\n"
            "・M/D HH:MM\n"
            "\n"
            "やめるときは「キャンセル」"
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

        send_reply(reply_token, [text_message(msg), reminder_menu_message()])
        return

    if state["state"] == STATE_WAITING_WANT_CONTENT:
        add_want(user_id, text.strip())
        reset_state(user_id)
        send_reply(reply_token, [
            text_message(f"OK！「{text.strip()}」を追加したよ！"),
            want_menu_message()
        ])
        return
    
    if state["state"] == STATE_WAITING_REMINDER_DELETE:
        try:
            reminder_id = int(text.strip())
        except ValueError:
            send_reply(reply_token, [text_message("数字のIDを送ってね。\nやめるときは「キャンセル」")])
            return

        deleted = delete_reminder_by_id(user_id, reminder_id)
        if not deleted:
            send_reply(reply_token, [text_message("そのIDは見つからなかったよ。もう一度送ってね。\nやめるときは「キャンセル」")])
            return

        reset_state(user_id)
        send_reply(reply_token, [
            text_message(f"リマインダー ID:{reminder_id} を削除したよ！"),
            reminder_menu_message()
        ])
        return

    if state["state"] == STATE_WAITING_WANT_DELETE:
        try:
            want_id = int(text.strip())
        except ValueError:
            send_reply(reply_token, [text_message("数字のIDを送ってね。\nやめるときは「キャンセル」")])
            return

        deleted = delete_want_by_id(user_id, want_id)
        if not deleted:
            send_reply(reply_token, [text_message("そのIDは見つからなかったよ。もう一度送ってね。\nやめるときは「キャンセル」")])
            return

        reset_state(user_id)
        send_reply(reply_token, [
            text_message(f"ほしいもの ID:{want_id} を削除したよ！"),
            want_menu_message()
        ])
        return

    send_reply(reply_token, [
        text_message("「メニュー」を押すか送ってね。"),
        main_menu_message()
    ])


def get_today_occurrences(row: sqlite3.Row, base_now: datetime) -> tuple[date, datetime] | None:
    """
    そのリマインダーが今日発生するなら (today, event_dt) を返す
    """
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


def send_due_notifications():
    current = now_jst()
    today_str = current.date().isoformat()

    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM reminders").fetchall()

        for row in rows:
            occurrence = get_today_occurrences(row, current)
            if not occurrence:
                continue

            _, event_dt = occurrence
            one_hour_before = event_dt - timedelta(hours=1)
            ten_min_before = event_dt - timedelta(minutes=10)

            # 0:00通知
            if row["last_day_notice_date"] != today_str and current.hour == 0 and current.minute == 0:
                send_push(row["user_id"], [text_message(f"今日の予定の一つだよ。\n{event_dt.strftime('%H:%M')} {row['content']}")])
                conn.execute(
                    "UPDATE reminders SET last_day_notice_date = ? WHERE id = ?",
                    (today_str, row["id"])
                )

            # 1時間前通知
            if row["last_1h_notice_date"] != today_str and current >= one_hour_before:
                send_push(row["user_id"], [text_message(f"1時間前だよ。\n{event_dt.strftime('%H:%M')} {row['content']}")])
                conn.execute(
                    "UPDATE reminders SET last_1h_notice_date = ? WHERE id = ?",
                    (today_str, row["id"])
                )

            # 10分前通知
            if row["last_10m_notice_date"] != today_str and current >= ten_min_before:
                send_push(row["user_id"], [text_message(f"10分前だよ。\n{event_dt.strftime('%H:%M')} {row['content']}")])
                conn.execute(
                    "UPDATE reminders SET last_10m_notice_date = ? WHERE id = ?",
                    (today_str, row["id"])
                )

        # 単発予定の削除
        conn.execute(
            """
            DELETE FROM reminders
            WHERE kind = 'single'
              AND datetime(scheduled_at) < datetime(?)
            """,
            (current.isoformat(),)
        )

        # 状態のタイムアウト
        timeout_border = (current - timedelta(minutes=10)).isoformat()
        conn.execute(
            """
            UPDATE user_states
            SET state = ?, temp_content = NULL, updated_at = ?
            WHERE state != ?
              AND updated_at < ?
            """,
            (STATE_NONE, current.isoformat(), STATE_NONE, timeout_border)
        )


@app.on_event("startup")
def startup():
    init_db()
    scheduler.add_job(send_due_notifications, "interval", minutes=1, id="notify_job", replace_existing=True)
    scheduler.start()


@app.on_event("shutdown")
def shutdown():
    if scheduler.running:
        scheduler.shutdown()


@app.get("/")
def healthcheck():
    return {"ok": True}


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
                send_reply(event.reply_token, [
                    text_message("友だち追加ありがとう！"),
                    main_menu_message()
                ])

        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            user_id = getattr(event.source, "user_id", None)
            if not user_id:
                continue
            handle_text_message(user_id, event.message.text, event.reply_token)

    return JSONResponse({"ok": True})