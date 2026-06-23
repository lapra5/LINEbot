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
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, content, kind, scheduled_at, weekday, time_hhmm, created_at
            FROM reminders
            WHERE user_id = %s
            ORDER BY
                CASE WHEN kind = 'single' THEN 0 ELSE 1 END,
                CAST(scheduled_at AS timestamptz) NULLS LAST,
                created_at ASC NULLS LAST,
                id ASC
            """,
            (line_user_id,),
        ).fetchall()

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
        return ["ほしいものはありません"]

    lines = ["【ほしいもの一覧】", ""]
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. {row['content']}")
    return lines


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
        parsed = parse_datetime_input(str(self.datetime_input.value))

        if not parsed or parsed.get("error") == "time_required":
            await interaction.followup.send(
                "日時がうまく読めなかったよ。例：明日 10:00、6/20 18:00、3分後",
                ephemeral=True,
            )
            return

        if parsed.get("error") == "past_datetime":
            await interaction.followup.send("その日時はもう過ぎているよ。", ephemeral=True)
            return

        create_reminder(link["line_user_id"], content, parsed)

        if parsed["kind"] == "single":
            when_text = parsed["scheduled_at"].astimezone(TZ).strftime("%Y/%m/%d %H:%M")
            await interaction.followup.send(
                f"OK！『{content}』を {when_text} に通知するね！",
                ephemeral=True,
            )
            return

        weekly_text = format_weekly_datetime(parsed["weekday"], parsed["time_hhmm"])
        await interaction.followup.send(
            f"OK！『{content}』を {weekly_text} に通知するね！",
            ephemeral=True,
        )


class WantModal(discord.ui.Modal, title="ほしいもの追加"):
    def __init__(self) -> None:
        super().__init__()
        self.want_input = discord.ui.TextInput(
            label="ほしいもの",
            placeholder="例：参考書、イヤホン",
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
            f"OK！『{content}』をほしいものに追加したよ！",
            ephemeral=True,
        )


class BackupRestoreModal(discord.ui.Modal, title="バックアップ復元"):
    def __init__(self) -> None:
        super().__init__()
        self.number_input = discord.ui.TextInput(
            label="復元する番号",
            placeholder="例：1",
            required=True,
            max_length=10,
        )
        self.add_item(self.number_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await interaction.followup.send(
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                ephemeral=True,
            )
            return

        raw = str(self.number_input.value).strip()
        if not raw.isdigit():
            await interaction.followup.send("復元する番号を数字で入力してね。", ephemeral=True)
            return

        index = int(raw)
        backups = list_backups(link["line_user_id"])
        if index < 1 or index > len(backups):
            await interaction.followup.send("その番号のバックアップは見つからないよ。", ephemeral=True)
            return

        target = backups[index - 1]
        restored = restore_backup(link["line_user_id"], int(target["id"]))
        if not restored:
            await interaction.followup.send("バックアップの復元に失敗したよ。", ephemeral=True)
            return

        await interaction.followup.send(
            f"OK！バックアップ {index} を復元したよ！",
            ephemeral=True,
        )


class WantsMenuView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="追加",
        style=discord.ButtonStyle.primary,
        custom_id="discord_wants_add",
    )
    async def add_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                    ephemeral=True,
                )
            return

        await interaction.response.send_modal(WantModal())

    @discord.ui.button(
        label="一覧",
        style=discord.ButtonStyle.secondary,
        custom_id="discord_wants_list",
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
        label="戻る",
        style=discord.ButtonStyle.success,
        custom_id="discord_wants_back",
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

        await interaction.followup.send("バックアップを保存したよ。", ephemeral=True)

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
        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
                    ephemeral=True,
                )
            return

        await interaction.response.send_modal(BackupRestoreModal())

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
        link = get_account_link_by_discord_user_id(str(interaction.user.id))
        if not link:
            await self.send_button_message(
                interaction,
                "LINEアカウントと連携されていません。\nLINEで『Discord連携』を実行してね。",
            )
            return

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

        lines = build_reminder_lines(link["line_user_id"])
        chunks = split_message_lines(lines)

        for chunk in chunks:
            await interaction.followup.send(chunk, ephemeral=True)

    @discord.ui.button(
        label="ほしいもの",
        style=discord.ButtonStyle.success,
        custom_id="discord_menu_want",
    )
    async def want_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "ほしいものメニューだよ。操作を選んでね。",
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
