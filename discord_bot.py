import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TZ = ZoneInfo(os.getenv("TZ", "Asia/Tokyo"))
JP_WEEK_FULL = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"]

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


def build_reminder_lines() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, content, kind, scheduled_at, weekday, time_hhmm, created_at
            FROM reminders
            ORDER BY
                CASE WHEN kind = 'single' THEN 0 ELSE 1 END,
                CAST(scheduled_at AS timestamptz) NULLS LAST,
                created_at ASC NULLS LAST,
                id ASC
            """
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


def get_account_link_by_discord_user_id(discord_user_id: str) -> dict | None:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT line_user_id, discord_user_id, linked_at
            FROM account_links
            WHERE discord_user_id = %s
            """,
            (discord_user_id,)
        ).fetchone()


def delete_account_link_by_discord_user_id(discord_user_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM account_links WHERE discord_user_id = %s",
            (discord_user_id,)
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
            (code,)
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
            (code_row["line_user_id"],)
        ).fetchone()
        if existing_line_link:
            return False, "このLINEアカウントはすでに別のDiscordと連携済みだよ。"

        existing_discord_link = conn.execute(
            """
            SELECT line_user_id, discord_user_id
            FROM account_links
            WHERE discord_user_id = %s
            """,
            (discord_user_id,)
        ).fetchone()
        if existing_discord_link:
            return False, "このDiscordアカウントはすでに別のLINEと連携済みだよ。"

        conn.execute(
            """
            INSERT INTO account_links (line_user_id, discord_user_id, linked_at)
            VALUES (%s, %s, %s)
            """,
            (code_row["line_user_id"], discord_user_id, current.isoformat())
        )
        conn.execute(
            """
            UPDATE account_link_codes
            SET used_at = %s
            WHERE code = %s
            """,
            (current.isoformat(), code)
        )

    return True, "LINEアカウントとDiscordを連携したよ。"


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
        await self.send_button_message(interaction, "リマインド機能は準備中")

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

        lines = build_reminder_lines()
        chunks = split_message_lines(lines)

        for index, chunk in enumerate(chunks):
            if index == 0:
                await interaction.followup.send(chunk, ephemeral=True)
            else:
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
        await self.send_button_message(interaction, "ほしいもの機能は準備中")

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
        await self.send_button_message(interaction, "バックアップ機能は準備中")


class DiscordMenuBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        init_db()
        self.add_view(MenuView())
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
