import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

import discord
import psycopg
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TZ = ZoneInfo(os.getenv("TZ", "Asia/Tokyo"))
JP_WEEK_FULL = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"]
WEEKDAY_MAP = {
    "月": 0, "月曜": 0, "月曜日": 0,
    "火": 1, "火曜": 1, "火曜日": 1,
    "水": 2, "水曜": 2, "水曜日": 2,
    "木": 3, "木曜": 3, "木曜日": 3,
    "金": 4, "金曜": 4, "金曜日": 4,
    "土": 5, "土曜": 5, "土曜日": 5,
    "日": 6, "日曜": 6, "日曜日": 6,
}

if not DISCORD_BOT_TOKEN:
    raise RuntimeError("環境変数 DISCORD_BOT_TOKEN を設定してください。")

if not DATABASE_URL:
    raise RuntimeError("環境変数 DATABASE_URL を設定してください。")


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


def normalize_digits(text: str) -> str:
    return text.translate(str.maketrans("０１２３４５６７８９：／", "0123456789:/"))


def init_db() -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account_links (
                line_user_id TEXT PRIMARY KEY,
                discord_user_id TEXT UNIQUE NOT NULL,
                linked_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account_link_codes (
                code TEXT PRIMARY KEY,
                line_user_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )


def format_single_datetime(dt_text: str | None) -> str:
    if not dt_text:
        return "日時未設定"

    dt = datetime.fromisoformat(dt_text).astimezone(TZ)
    return dt.strftime("%Y/%m/%d %H:%M")


def format_weekly_datetime(weekday: int | None, hhmm: str | None) -> str:
    if weekday is None or weekday < 0 or weekday >= len(JP_WEEK_FULL):
        weekday_text = "曜日未設定"
    else:
        weekday_text = JP_WEEK_FULL[weekday]

    return f"毎週 {weekday_text} {hhmm or '時刻未設定'}"


def parse_time_hhmm(text: str) -> str | None:
    normalized = normalize_digits(text)

    match = re.search(r"(\d{1,2}):(\d{2})", normalized)
    if match:
        hh = int(match.group(1))
        mm = int(match.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"

    match = re.search(r"(\d{1,2})時(?:\s*(\d{1,2})分?)?", normalized)
    if match:
        hh = int(match.group(1))
        mm = int(match.group(2) or "0")
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"

    return None


def parse_relative_datetime(text: str) -> datetime | None:
    normalized = normalize_digits(text)
    current = now_jst().replace(second=0, microsecond=0)

    match = re.search(r"(\d+)\s*分後", normalized)
    if match:
        return current + timedelta(minutes=int(match.group(1)))

    match = re.search(r"(\d+)\s*時間後", normalized)
    if match:
        return current + timedelta(hours=int(match.group(1)))

    return None


def parse_time_only_datetime(text: str) -> tuple[datetime, str] | None:
    hhmm = parse_time_hhmm(text)
    if not hhmm:
        return None

    current = now_jst()
    hh, mm = map(int, hhmm.split(":"))
    candidate = datetime.combine(current.date(), dt_time(hh, mm), tzinfo=TZ)
    if candidate <= current:
        candidate += timedelta(days=1)

    return candidate, hhmm


def parse_datetime_input(text: str) -> dict[str, Any] | None:
    normalized = normalize_digits(text.strip())
    current = now_jst()

    def time_required() -> dict[str, str]:
        return {"error": "time_required"}

    match = re.search(r"\b(\d{4})(\d{2})(\d{2})\b", normalized)
    if match:
        year, month, day = map(int, match.groups())
        hhmm = parse_time_hhmm(normalized)
        if not hhmm:
            return time_required()
        hh, mm = map(int, hhmm.split(":"))
        dt = datetime(year, month, day, hh, mm, tzinfo=TZ)
        if dt < current:
            return {"error": "past_datetime"}
        return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    match = re.search(r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b", normalized)
    if match:
        year, month, day = map(int, match.groups())
        hhmm = parse_time_hhmm(normalized)
        if not hhmm:
            return time_required()
        hh, mm = map(int, hhmm.split(":"))
        dt = datetime(year, month, day, hh, mm, tzinfo=TZ)
        if dt < current:
            return {"error": "past_datetime"}
        return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    match = re.search(r"\b(\d{1,2})/(\d{1,2})\b", normalized)
    if match:
        month, day = map(int, match.groups())
        hhmm = parse_time_hhmm(normalized)
        if not hhmm:
            return time_required()
        hh, mm = map(int, hhmm.split(":"))
        dt = datetime(current.year, month, day, hh, mm, tzinfo=TZ)
        if dt < current:
            return {"error": "past_datetime"}
        return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    relative_dt = parse_relative_datetime(normalized)
    if relative_dt:
        return {
            "kind": "single",
            "scheduled_at": relative_dt,
            "time_hhmm": relative_dt.strftime("%H:%M"),
        }

    for key, add_days in [("今日", 0), ("明日", 1), ("明後日", 2)]:
        if key in normalized:
            hhmm = parse_time_hhmm(normalized)
            if not hhmm:
                return time_required()
            hh, mm = map(int, hhmm.split(":"))
            target_date = (current + timedelta(days=add_days)).date()
            dt = datetime.combine(target_date, dt_time(hh, mm), tzinfo=TZ)
            if dt < current:
                return {"error": "past_datetime"}
            return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    weekday_found = None
    for key, value in WEEKDAY_MAP.items():
        if key in normalized:
            weekday_found = value
            break
    if weekday_found is not None:
        hhmm = parse_time_hhmm(normalized)
        if not hhmm:
            return time_required()
        return {"kind": "weekly", "weekday": weekday_found, "time_hhmm": hhmm}

    time_only = parse_time_only_datetime(normalized)
    if time_only:
        dt, hhmm = time_only
        return {"kind": "single", "scheduled_at": dt, "time_hhmm": hhmm}

    return None


def build_reminder_lines(line_user_id: str) -> list[str]:
    rows = get_reminders_for_user(line_user_id)

    if not rows:
        return ["リマインダーはありません"]

    lines = ["【リマインダー一覧】", ""]
    for index, row in enumerate(rows):
        if row["kind"] == "single":
            when = format_single_datetime(row["scheduled_at"])
        else:
            when = format_weekly_datetime(row["weekday"], row["time_hhmm"])

        lines.append(when)
        lines.append(str(row["content"]))

        if index != len(rows) - 1:
            lines.append("")

    return lines


def get_reminders_for_user(line_user_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT id, content, kind, scheduled_at, weekday, time_hhmm, created_at
        FROM reminders
        WHERE user_id = %s
        ORDER BY
            CASE WHEN kind = 'single' THEN 0 ELSE 1 END,
            CAST(scheduled_at AS timestamptz) NULLS LAST,
            created_at ASC NULLS LAST,
            id ASC
    """
    params: list[Any] = [line_user_id]
    if limit is not None:
        query += "\nLIMIT %s"
        params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()

    return [dict(row) for row in rows]


def build_reminder_delete_label(reminder: dict[str, Any]) -> str:
    if reminder["kind"] == "single":
        when_text = format_single_datetime(reminder["scheduled_at"])
    else:
        weekday = reminder.get("weekday")
        weekday_text = JP_WEEK_FULL[weekday] if isinstance(weekday, int) and 0 <= weekday < len(JP_WEEK_FULL) else "曜日未設定"
        when_text = f"毎週 {weekday_text} {reminder.get('time_hhmm') or '時刻未設定'}"

    content = str(reminder.get("content", "")).strip()
    return f"{when_text} {content}".strip()[:100]


def delete_reminder_for_user(line_user_id: str, reminder_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM reminders WHERE id = %s AND user_id = %s",
            (reminder_id, line_user_id),
        )
        return cur.rowcount > 0


def build_reminder_list_chunks(line_user_id: str) -> list[str]:
    return split_message_lines(build_reminder_lines(line_user_id))


async def restore_reminder_delete_list(
    interaction: discord.Interaction,
    line_user_id: str,
) -> None:
    reminders = get_reminders_for_user(line_user_id)
    if not reminders:
        await interaction.edit_original_response(
            content="リマインダーはありません",
            view=ReminderListBackOnlyView(),
        )
        return

    chunks = build_reminder_list_chunks(line_user_id)
    for chunk in chunks[:-1]:
        await interaction.followup.send(chunk, ephemeral=True)

    await interaction.edit_original_response(
        content=chunks[-1],
        view=ReminderListView(),
    )


class SuccessBackToMenuView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.button(
        label="戻る",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_success_back_to_menu",
    )
    async def back_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="使いたい機能を選んでください。",
            view=MenuView(),
        )


def split_message_lines(lines: list[str], max_length: int = 1900) -> list[str]:
    chunks: list[str] = []
    current = ""

    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_length:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = line
        else:
            chunks.append(line[:max_length])
            current = line[max_length:]

    if current:
        chunks.append(current)

    return chunks


def get_account_link_by_discord_user_id(discord_user_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT line_user_id, discord_user_id, linked_at
            FROM account_links
            WHERE discord_user_id = %s
            """,
            (discord_user_id,),
        ).fetchone()


def delete_account_link_by_discord_user_id(discord_user_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM account_links WHERE discord_user_id = %s",
            (discord_user_id,),
        )
        return cur.rowcount > 0


def link_discord_account(discord_user_id: str, code: str) -> tuple[bool, str]:
    current = now_jst()

    with get_conn() as conn:
        code_row = conn.execute(
            """
            SELECT code, line_user_id, expires_at, used_at, created_at
            FROM account_link_codes
            WHERE code = %s
            """,
            (code,),
        ).fetchone()

        if not code_row:
            return False, "連携コードが見つからないよ。"

        if code_row["used_at"]:
            return False, "この連携コードはすでに使用済みだよ。"

        expires_at = datetime.fromisoformat(code_row["expires_at"]).astimezone(TZ)
        if expires_at < current:
            return False, "この連携コードは期限切れだよ。"

        existing_line_link = conn.execute(
            """
            SELECT line_user_id, discord_user_id
            FROM account_links
            WHERE line_user_id = %s
            """,
            (code_row["line_user_id"],),
        ).fetchone()
        if existing_line_link:
            return False, "このLINEアカウントはすでに別のDiscordと連携済みだよ。"

        existing_discord_link = conn.execute(
            """
            SELECT line_user_id, discord_user_id
            FROM account_links
            WHERE discord_user_id = %s
            """,
            (discord_user_id,),
        ).fetchone()
        if existing_discord_link:
            return False, "このDiscordアカウントはすでに別のLINEと連携済みだよ。"

        conn.execute(
            """
            INSERT INTO account_links (line_user_id, discord_user_id, linked_at)
            VALUES (%s, %s, %s)
            """,
            (code_row["line_user_id"], discord_user_id, current.isoformat()),
        )
        conn.execute(
            """
            UPDATE account_link_codes
            SET used_at = %s
            WHERE code = %s
            """,
            (current.isoformat(), code),
        )

    return True, "LINEアカウントとDiscordを連携したよ。"


def create_reminder(line_user_id: str, content: str, parsed: dict[str, Any]) -> None:
    current = now_jst().isoformat()
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
                    line_user_id,
                    content,
                    parsed["scheduled_at"].isoformat(),
                    parsed["time_hhmm"],
                    current,
                ),
            )
            return

        conn.execute(
            """
            INSERT INTO reminders (
                user_id, content, kind, scheduled_at, weekday, time_hhmm,
                last_day_notice_date, last_1h_notice_date, last_10m_notice_date, last_exact_notice_date, created_at
            )
            VALUES (%s, %s, 'weekly', NULL, %s, %s, NULL, NULL, NULL, NULL, %s)
            """,
            (
                line_user_id,
                content,
                parsed["weekday"],
                parsed["time_hhmm"],
                current,
            ),
        )


async def save_reminder_from_parsed_input(
    interaction: discord.Interaction,
    line_user_id: str,
    content: str,
    datetime_text: str,
) -> None:
    parsed = parse_datetime_input(datetime_text)

    if not parsed or parsed.get("error") == "time_required":
        await interaction.followup.send(
            "日時がうまく読めなかったよ。例：明日 10:00、6/20 18:00、3分後",
            ephemeral=True,
        )
        return

    if parsed.get("error") == "past_datetime":
        await interaction.followup.send("その日時はもう過ぎているよ。", ephemeral=True)
        return

    create_reminder(line_user_id, content, parsed)

    if parsed["kind"] == "single":
        when_text = parsed["scheduled_at"].astimezone(TZ).strftime("%Y/%m/%d %H:%M")
        await interaction.followup.send(
            f"OK！『{content}』を {when_text} に通知するね！",
            ephemeral=True,
            view=SuccessBackToMenuView(),
        )
        return

    weekly_text = format_weekly_datetime(parsed["weekday"], parsed["time_hhmm"])
    await interaction.followup.send(
        f"OK！『{content}』を {weekly_text} に通知するね！",
        ephemeral=True,
        view=SuccessBackToMenuView(),
    )


def create_want(line_user_id: str, content: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO wants (user_id, content, created_at)
            VALUES (%s, %s, %s)
            """,
            (line_user_id, content, now_jst().isoformat()),
        )


def build_want_lines(line_user_id: str) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT content
            FROM wants
            WHERE user_id = %s
            ORDER BY created_at DESC
            """,
            (line_user_id,),
        ).fetchall()

    if not rows:
        return ["メモはありません"]

    lines = ["【メモ一覧】", ""]
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. {row['content']}")
    return lines


def get_wants_for_user(line_user_id: str, limit: int = 25) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, content
            FROM wants
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (line_user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_want_for_user(line_user_id: str, want_id: int) -> bool:
    with get_conn() as conn:
        deleted = conn.execute(
            "DELETE FROM wants WHERE id = %s AND user_id = %s",
            (want_id, line_user_id),
        )
    return deleted.rowcount > 0


def update_want_for_user(line_user_id: str, want_id: int, content: str) -> bool:
    with get_conn() as conn:
        updated = conn.execute(
            "UPDATE wants SET content = %s WHERE id = %s AND user_id = %s",
            (content, want_id, line_user_id),
        )
    return updated.rowcount > 0


def build_want_delete_label(want: dict[str, Any]) -> str:
    content = str(want.get("content", "")).strip() or "内容なし"
    return content.replace("\n", " ")[:100]


def build_want_edit_label(want: dict[str, Any]) -> str:
    return build_want_delete_label(want)


def save_backup(line_user_id: str) -> str:
    with get_conn() as conn:
        reminders = conn.execute(
            """
            SELECT content, kind, scheduled_at, weekday, time_hhmm
            FROM reminders
            WHERE user_id = %s
            ORDER BY created_at ASC
            """,
            (line_user_id,),
        ).fetchall()

        wants = conn.execute(
            """
            SELECT content
            FROM wants
            WHERE user_id = %s
            ORDER BY created_at ASC
            """,
            (line_user_id,),
        ).fetchall()

        if not reminders and not wants:
            return "skip"

        data = {
            "reminders": [dict(row) for row in reminders],
            "wants": [dict(row) for row in wants],
        }

        backups = conn.execute(
            """
            SELECT id
            FROM backups
            WHERE user_id = %s
            ORDER BY created_at ASC
            """,
            (line_user_id,),
        ).fetchall()

        if len(backups) >= 5:
            conn.execute("DELETE FROM backups WHERE id = %s", (backups[0]["id"],))

        conn.execute(
            "INSERT INTO backups (user_id, data, created_at) VALUES (%s, %s, %s)",
            (line_user_id, json.dumps(data, ensure_ascii=False), now_jst().isoformat()),
        )

        return "saved"


def list_backups(line_user_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return list(
            conn.execute(
                """
                SELECT id, created_at
                FROM backups
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (line_user_id,),
            ).fetchall()
        )


def restore_backup(line_user_id: str, backup_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT data FROM backups WHERE id = %s AND user_id = %s",
            (backup_id, line_user_id),
        ).fetchone()

        if not row:
            return False

        data = row["data"]

        conn.execute("DELETE FROM reminders WHERE user_id = %s", (line_user_id,))
        conn.execute("DELETE FROM wants WHERE user_id = %s", (line_user_id,))

        for reminder in data.get("reminders", []):
            conn.execute(
                """
                INSERT INTO reminders (
                    user_id, content, kind, scheduled_at, weekday, time_hhmm,
                    last_day_notice_date, last_1h_notice_date, last_10m_notice_date, last_exact_notice_date, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, NULL, NULL, %s)
                """,
                (
                    line_user_id,
                    reminder.get("content", ""),
                    reminder.get("kind", "single"),
                    reminder.get("scheduled_at"),
                    reminder.get("weekday"),
                    reminder.get("time_hhmm", "00:00"),
                    now_jst().isoformat(),
                ),
            )

        for want in data.get("wants", []):
            conn.execute(
                "INSERT INTO wants (user_id, content, created_at) VALUES (%s, %s, %s)",
                (line_user_id, want.get("content", ""), now_jst().isoformat()),
            )

        return True


def format_backup_created_at(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value.astimezone(TZ) if value.tzinfo else value.replace(tzinfo=TZ)
        return dt.strftime("%Y/%m/%d %H:%M")

    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        else:
            dt = dt.astimezone(TZ)
        return dt.strftime("%Y/%m/%d %H:%M")

    return str(value)


def build_backup_lines(line_user_id: str) -> list[str]:
    backups = list_backups(line_user_id)
    if not backups:
        return ["バックアップはありません"]

    lines = ["【バックアップ一覧】", ""]
    for index, backup in enumerate(backups, start=1):
        lines.append(f"{index}. {format_backup_created_at(backup['created_at'])}")
    return lines


class ReminderModal(discord.ui.Modal, title="リマインダー追加"):
    def __init__(self) -> None:
        super().__init__()
        self.content_input = discord.ui.TextInput(
            label="通知内容",
            placeholder="例：数学の宿題",
            required=True,
            max_length=200,
        )
        self.datetime_input = discord.ui.TextInput(
            label="日時",
            placeholder="例：明日 10:00 / 水曜日 08:00 / 3分後 / 6/20 18:00",
            required=True,
            max_length=100,
        )
        self.add_item(self.content_input)
        self.add_item(self.datetime_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await interaction.followup.send(
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                ephemeral=True,
            )
            return

        content = str(self.content_input.value).strip()
        await save_reminder_from_parsed_input(
            interaction,
            link["line_user_id"],
            content,
            str(self.datetime_input.value),
        )


class WantModal(discord.ui.Modal, title="メモ追加"):
    def __init__(self) -> None:
        super().__init__()
        self.want_input = discord.ui.TextInput(
            label="メモ",
            placeholder="例：買うもの、あとで確認すること",
            required=True,
            max_length=200,
        )
        self.add_item(self.want_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await interaction.followup.send(
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                ephemeral=True,
            )
            return

        content = str(self.want_input.value).strip()
        create_want(link["line_user_id"], content)
        await interaction.followup.send(
            "メモを保存したよ！",
            ephemeral=True,
            view=SuccessBackToMenuView(),
        )


class WantEditModal(discord.ui.Modal, title="メモ編集"):
    def __init__(
        self,
        owner_discord_user_id: str,
        line_user_id: str,
        want_id: int,
        current_content: str,
    ) -> None:
        super().__init__()
        self.owner_discord_user_id = owner_discord_user_id
        self.line_user_id = line_user_id
        self.want_id = want_id
        self.want_input = discord.ui.TextInput(
            label="メモ",
            default=current_content[:200],
            required=True,
            max_length=200,
        )
        self.add_item(self.want_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        content = str(self.want_input.value).strip()
        updated = update_want_for_user(self.line_user_id, self.want_id, content)
        if not updated:
            await interaction.followup.send("更新対象が見つからなかったよ。", ephemeral=True)
            return

        await interaction.followup.send(
            "メモを更新したよ！",
            ephemeral=True,
            view=SuccessBackToMenuView(),
        )


class WantReminderDatetimeModal(discord.ui.Modal, title="リマインダー日時"):
    def __init__(
        self,
        owner_discord_user_id: str,
        line_user_id: str,
        want_content: str,
    ) -> None:
        super().__init__()
        self.owner_discord_user_id = owner_discord_user_id
        self.line_user_id = line_user_id
        self.want_content = want_content
        self.datetime_input = discord.ui.TextInput(
            label="日時",
            placeholder="例：明日 10:00 / 3分後 / 6/20 18:00 / 毎週 月曜日 08:00",
            required=True,
            max_length=100,
        )
        self.add_item(self.datetime_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await save_reminder_from_parsed_input(
            interaction,
            self.line_user_id,
            self.want_content,
            str(self.datetime_input.value),
        )


class BackupRestoreSelect(discord.ui.Select):
    def __init__(self, owner_discord_user_id: str, line_user_id: str, backups: list[dict[str, Any]]) -> None:
        options: list[discord.SelectOption] = []
        for index, backup in enumerate(backups):
            label = format_backup_created_at(backup["created_at"])
            if index == 0:
                label = f"{label}（最新）"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(backup["id"]),
                )
            )

        super().__init__(
            placeholder="復元するバックアップを選んでね",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="discord_backup_restore_select",
        )
        self.owner_discord_user_id = owner_discord_user_id
        self.line_user_id = line_user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("この復元メニューは操作できないよ。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        backup_id = int(self.values[0])
        restored = restore_backup(self.line_user_id, backup_id)
        if not restored:
            await interaction.followup.send("バックアップの復元に失敗したよ。", ephemeral=True)
            return

        await interaction.followup.send(
            "OK！バックアップを復元したよ！",
            ephemeral=True,
            view=SuccessBackToMenuView(),
        )


class BackupRestoreView(discord.ui.View):
    def __init__(self, owner_discord_user_id: str, line_user_id: str, backups: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.add_item(BackupRestoreSelect(owner_discord_user_id, line_user_id, backups))


class ReminderDeleteSelect(discord.ui.Select):
    def __init__(self, owner_discord_user_id: str, line_user_id: str, reminders: list[dict[str, Any]]) -> None:
        options = [
            discord.SelectOption(
                label=build_reminder_delete_label(reminder),
                value=str(reminder["id"]),
            )
            for reminder in reminders[:25]
        ]

        super().__init__(
            placeholder="削除するリマインダーを選んでね",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="discord_reminder_delete_select",
        )
        self.owner_discord_user_id = owner_discord_user_id
        self.line_user_id = line_user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            reminder_id = int(self.values[0])
        except (TypeError, ValueError):
            await interaction.followup.send("削除対象が見つからなかったよ。", ephemeral=True)
            return

        if isinstance(self.view, ReminderDeleteSelectView):
            self.view.selected_reminder_id = reminder_id


class ReminderDeleteSelectView(discord.ui.View):
    def __init__(self, owner_discord_user_id: str, line_user_id: str, reminders: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.owner_discord_user_id = owner_discord_user_id
        self.line_user_id = line_user_id
        self.selected_reminder_id: int | None = None
        self.add_item(ReminderDeleteSelect(owner_discord_user_id, line_user_id, reminders))

    @discord.ui.button(
        label="削除",
        style=discord.ButtonStyle.danger,
        custom_id="discord_reminder_delete_confirm",
    )
    async def delete_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        if self.selected_reminder_id is None:
            await interaction.response.send_message("削除する項目を選んでね。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        deleted = delete_reminder_for_user(self.line_user_id, self.selected_reminder_id)
        if not deleted:
            await interaction.edit_original_response(
                content="削除対象が見つからなかったよ。",
                view=None,
            )
            return

        await interaction.edit_original_response(
            content="リマインダーを削除したよ。",
            view=None,
        )

    @discord.ui.button(
        label="キャンセル",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_reminder_delete_cancel",
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await restore_reminder_delete_list(interaction, self.line_user_id)


class ReminderListBackOnlyView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.button(
        label="戻る",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_reminder_list_back",
    )
    async def back_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="使いたい機能を選んでください。",
            view=MenuView(),
        )


class ReminderListView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.button(
        label="削除",
        style=discord.ButtonStyle.danger,
        custom_id="discord_reminder_delete_open",
    )
    async def open_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await interaction.followup.send(
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                ephemeral=True,
            )
            return

        reminders = get_reminders_for_user(link["line_user_id"], limit=25)
        if not reminders:
            await interaction.followup.send("リマインダーはありません", ephemeral=True)
            return

        await interaction.followup.send(
            "削除するリマインダーを選んでね。",
            view=ReminderDeleteSelectView(str(interaction.user.id), link["line_user_id"], reminders),
            ephemeral=True,
        )

    @discord.ui.button(
        label="戻る",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_reminder_list_back",
    )
    async def back_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="使いたい機能を選んでください。",
            view=MenuView(),
        )


class WantDeleteSelect(discord.ui.Select):
    def __init__(self, owner_discord_user_id: str, line_user_id: str, wants: list[dict[str, Any]]) -> None:
        options = [
            discord.SelectOption(
                label=build_want_delete_label(want),
                value=str(want["id"]),
            )
            for want in wants[:25]
        ]

        super().__init__(
            placeholder="削除するメモを選んでね",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="discord_memo_delete_select",
        )
        self.owner_discord_user_id = owner_discord_user_id
        self.line_user_id = line_user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            want_id = int(self.values[0])
        except (TypeError, ValueError):
            await interaction.followup.send("削除対象が見つからなかったよ。", ephemeral=True)
            return

        if isinstance(self.view, WantDeleteSelectView):
            self.view.selected_want_id = want_id


class WantDeleteSelectView(discord.ui.View):
    def __init__(self, owner_discord_user_id: str, line_user_id: str, wants: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.owner_discord_user_id = owner_discord_user_id
        self.line_user_id = line_user_id
        self.selected_want_id: int | None = None
        self.add_item(WantDeleteSelect(owner_discord_user_id, line_user_id, wants))

    @discord.ui.button(
        label="削除",
        style=discord.ButtonStyle.danger,
        custom_id="discord_memo_delete_confirm",
    )
    async def delete_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        if self.selected_want_id is None:
            await interaction.response.send_message("削除する項目を選んでね。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        deleted = delete_want_for_user(self.line_user_id, self.selected_want_id)
        if not deleted:
            await interaction.edit_original_response(
                content="削除対象が見つからなかったよ。",
                view=None,
            )
            return

        await interaction.edit_original_response(
            content="メモを削除したよ。",
            view=None,
        )

    @discord.ui.button(
        label="キャンセル",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_memo_delete_cancel",
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        await interaction.response.edit_message(
            content="📝 メモ",
            view=WantsMenuView(),
        )


class WantEditSelect(discord.ui.Select):
    def __init__(self, owner_discord_user_id: str, line_user_id: str, wants: list[dict[str, Any]]) -> None:
        options = [
            discord.SelectOption(
                label=build_want_edit_label(want),
                value=str(want["id"]),
            )
            for want in wants[:25]
        ]

        super().__init__(
            placeholder="編集するメモを選んでね",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="discord_memo_edit_select",
        )
        self.owner_discord_user_id = owner_discord_user_id
        self.line_user_id = line_user_id
        self.wants_by_id = {int(want["id"]): want for want in wants[:25]}

    async def callback(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            want_id = int(self.values[0])
        except (TypeError, ValueError):
            await interaction.followup.send("更新対象が見つからなかったよ。", ephemeral=True)
            return

        selected_want = self.wants_by_id.get(want_id)
        if selected_want is None:
            await interaction.followup.send("更新対象が見つからなかったよ。", ephemeral=True)
            return

        if isinstance(self.view, WantEditSelectView):
            self.view.selected_want_id = want_id
            self.view.selected_want_content = str(selected_want.get("content", ""))


class WantEditSelectView(discord.ui.View):
    def __init__(self, owner_discord_user_id: str, line_user_id: str, wants: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.owner_discord_user_id = owner_discord_user_id
        self.line_user_id = line_user_id
        self.selected_want_id: int | None = None
        self.selected_want_content = ""
        self.add_item(WantEditSelect(owner_discord_user_id, line_user_id, wants))

    @discord.ui.button(
        label="編集",
        style=discord.ButtonStyle.primary,
        custom_id="discord_memo_edit_confirm",
    )
    async def edit_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        if self.selected_want_id is None:
            await interaction.response.send_message("編集する項目を選んでね。", ephemeral=True)
            return

        await interaction.response.send_modal(
            WantEditModal(
                self.owner_discord_user_id,
                self.line_user_id,
                self.selected_want_id,
                self.selected_want_content,
            )
        )

    @discord.ui.button(
        label="キャンセル",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_memo_edit_cancel",
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        await interaction.response.edit_message(
            content="📝 メモ",
            view=WantsMenuView(),
        )


class WantReminderSelect(discord.ui.Select):
    def __init__(self, owner_discord_user_id: str, line_user_id: str, wants: list[dict[str, Any]]) -> None:
        options = [
            discord.SelectOption(
                label=build_want_edit_label(want),
                value=str(want["id"]),
            )
            for want in wants[:25]
        ]

        super().__init__(
            placeholder="リマインダー化するメモを選んでね",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="discord_memo_reminder_select",
        )
        self.owner_discord_user_id = owner_discord_user_id
        self.line_user_id = line_user_id
        self.wants_by_id = {int(want["id"]): want for want in wants[:25]}

    async def callback(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            want_id = int(self.values[0])
        except (TypeError, ValueError):
            await interaction.followup.send("対象のメモが見つからなかったよ。", ephemeral=True)
            return

        selected_want = self.wants_by_id.get(want_id)
        if selected_want is None:
            await interaction.followup.send("対象のメモが見つからなかったよ。", ephemeral=True)
            return

        if isinstance(self.view, WantReminderSelectView):
            self.view.selected_want_id = want_id
            self.view.selected_want_content = str(selected_want.get("content", ""))


class WantReminderSelectView(discord.ui.View):
    def __init__(self, owner_discord_user_id: str, line_user_id: str, wants: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.owner_discord_user_id = owner_discord_user_id
        self.line_user_id = line_user_id
        self.selected_want_id: int | None = None
        self.selected_want_content = ""
        self.add_item(WantReminderSelect(owner_discord_user_id, line_user_id, wants))

    @discord.ui.button(
        label="リマインダー化",
        style=discord.ButtonStyle.primary,
        custom_id="discord_memo_reminder_confirm",
    )
    async def remind_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        if self.selected_want_id is None:
            await interaction.response.send_message("リマインダー化する項目を選んでね。", ephemeral=True)
            return

        await interaction.response.send_modal(
            WantReminderDatetimeModal(
                self.owner_discord_user_id,
                self.line_user_id,
                self.selected_want_content,
            )
        )

    @discord.ui.button(
        label="キャンセル",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_memo_reminder_cancel",
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if str(interaction.user.id) != self.owner_discord_user_id:
            await interaction.response.send_message("このメニューは操作できないよ。", ephemeral=True)
            return

        await interaction.response.edit_message(
            content="📝 メモ",
            view=WantsMenuView(),
        )


class WantsMenuView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="追加",
        style=discord.ButtonStyle.primary,
        custom_id="discord_wants_add",
        row=0,
    )
    async def add_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(WantModal())

    @discord.ui.button(
        label="一覧",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_wants_list",
        row=0,
    )
    async def list_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await interaction.followup.send(
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                ephemeral=True,
            )
            return

        lines = build_want_lines(link["line_user_id"])
        chunks = split_message_lines(lines)
        for chunk in chunks:
            await interaction.followup.send(chunk, ephemeral=True)

    @discord.ui.button(
        label="編集",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_memo_edit_open",
        row=1,
    )
    async def edit_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await interaction.followup.send(
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                ephemeral=True,
            )
            return

        wants = get_wants_for_user(link["line_user_id"], limit=25)
        if not wants:
            await interaction.followup.send("メモはありません", ephemeral=True)
            return

        await interaction.followup.send(
            "編集するメモを選んでね。",
            view=WantEditSelectView(str(interaction.user.id), link["line_user_id"], wants),
            ephemeral=True,
        )

    @discord.ui.button(
        label="削除",
        style=discord.ButtonStyle.danger,
        custom_id="discord_memo_delete_open",
        row=1,
    )
    async def delete_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await interaction.followup.send(
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                ephemeral=True,
            )
            return

        wants = get_wants_for_user(link["line_user_id"], limit=25)
        if not wants:
            await interaction.followup.send("メモはありません", ephemeral=True)
            return

        await interaction.followup.send(
            "削除するメモを選んでね。",
            view=WantDeleteSelectView(str(interaction.user.id), link["line_user_id"], wants),
            ephemeral=True,
        )

    @discord.ui.button(
        label="リマインダー化",
        style=discord.ButtonStyle.primary,
        custom_id="discord_memo_reminder_open",
        row=2,
    )
    async def reminder_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await interaction.followup.send(
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                ephemeral=True,
            )
            return

        wants = get_wants_for_user(link["line_user_id"], limit=25)
        if not wants:
            await interaction.followup.send("メモはありません", ephemeral=True)
            return

        await interaction.followup.send(
            "リマインダー化するメモを選んでね。",
            view=WantReminderSelectView(str(interaction.user.id), link["line_user_id"], wants),
            ephemeral=True,
        )

    @discord.ui.button(
        label="戻る",
        style=discord.ButtonStyle.success,
        custom_id="discord_wants_back",
        row=2,
    )
    async def back_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "使いたい機能を選んでください。",
            view=MenuView(),
            ephemeral=True,
        )


class BackupMenuView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="保存",
        style=discord.ButtonStyle.primary,
        custom_id="discord_backup_save",
    )
    async def save_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await interaction.followup.send(
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                ephemeral=True,
            )
            return

        status = save_backup(link["line_user_id"])
        if status == "skip":
            await interaction.followup.send("保存できるデータがまだないよ。", ephemeral=True)
            return

        await interaction.followup.send(
            "バックアップを保存したよ。",
            ephemeral=True,
            view=SuccessBackToMenuView(),
        )

    @discord.ui.button(
        label="一覧",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_backup_list",
    )
    async def list_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await interaction.followup.send(
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                ephemeral=True,
            )
            return

        lines = build_backup_lines(link["line_user_id"])
        chunks = split_message_lines(lines)
        for chunk in chunks:
            await interaction.followup.send(chunk, ephemeral=True)

    @discord.ui.button(
        label="復元",
        style=discord.ButtonStyle.danger,
        custom_id="discord_backup_restore",
    )
    async def restore_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await interaction.followup.send(
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                ephemeral=True,
            )
            return

        backups = list_backups(link["line_user_id"])[:5]
        if not backups:
            await interaction.followup.send("バックアップはありません", ephemeral=True)
            return

        await interaction.followup.send(
            "復元するバックアップを選んでね。",
            view=BackupRestoreView(str(interaction.user.id), link["line_user_id"], backups),
            ephemeral=True,
        )

    @discord.ui.button(
        label="戻る",
        style=discord.ButtonStyle.success,
        custom_id="discord_backup_back",
    )
    async def back_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "使いたい機能を選んでください。",
            view=MenuView(),
            ephemeral=True,
        )


class MenuView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @staticmethod
    async def send_button_message(
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
            return

        await interaction.response.send_message(content, ephemeral=True)

    @discord.ui.button(
        label="リマインド",
        style=discord.ButtonStyle.primary,
        custom_id="discord_menu_reminder",
    )
    async def remind_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(ReminderModal())

    @discord.ui.button(
        label="一覧",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_menu_list",
    )
    async def list_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await interaction.followup.send(
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                ephemeral=True,
            )
            return

        reminders = get_reminders_for_user(link["line_user_id"])
        if not reminders:
            await interaction.followup.send(
                "リマインダーはありません",
                view=ReminderListBackOnlyView(),
                ephemeral=True,
            )
            return

        lines = build_reminder_lines(link["line_user_id"])
        chunks = split_message_lines(lines)

        for chunk in chunks[:-1]:
            await interaction.followup.send(chunk, ephemeral=True)

        await interaction.followup.send(
            chunks[-1],
            view=ReminderListView(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="📝 メモ",
        style=discord.ButtonStyle.success,
        custom_id="discord_menu_want",
    )
    async def want_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "📝 メモ",
            view=WantsMenuView(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="バックアップ",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_menu_backup",
    )
    async def backup_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "バックアップメニューだよ。操作を選んでね。",
            view=BackupMenuView(),
            ephemeral=True,
        )


class DiscordMenuBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        init_db()
        self.add_view(MenuView())
        self.add_view(WantsMenuView())
        self.add_view(BackupMenuView())
        print("Discord application commands sync started. The first sync can take a little time.")
        synced = await self.tree.sync()
        print(f"Discord application commands sync completed: {len(synced)} command(s).")

    async def on_ready(self) -> None:
        if self.user:
            print(f"Discord Bot logged in: {self.user} (id={self.user.id})")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        if message.content == "ping":
            await message.channel.send("pong")

        await self.process_commands(message)


bot = DiscordMenuBot()


@bot.tree.command(name="menu", description="LINE Bot 連携メニューを表示します")
async def menu_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=False)
    await interaction.followup.send(
        "使いたい機能を選んでください。",
        view=MenuView(),
    )


@bot.tree.command(name="link", description="LINEアカウントとDiscordを連携します")
@app_commands.describe(code="LINE側で発行された6桁コード")
async def link_command(interaction: discord.Interaction, code: str) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        normalized_code = code.strip()
        if not normalized_code.isdigit() or len(normalized_code) != 6:
            await interaction.followup.send("連携コードは6桁の数字で入力してね。", ephemeral=True)
            return

        _success, message = link_discord_account(str(interaction.user.id), normalized_code)
        await interaction.followup.send(message, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(
            f"連携処理でエラーが発生したよ。\n{exc}",
            ephemeral=True,
        )


@bot.tree.command(name="unlink", description="LINEアカウントとの連携を解除します")
async def unlink_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = delete_account_link_by_discord_user_id(str(interaction.user.id))
        if deleted:
            await interaction.followup.send("LINEアカウントとの連携を解除したよ。", ephemeral=True)
            return

        await interaction.followup.send("まだ連携されていないよ。", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(
            f"解除処理でエラーが発生したよ。\n{exc}",
            ephemeral=True,
        )


def main() -> None:
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
