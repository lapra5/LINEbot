import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

if not DISCORD_BOT_TOKEN:
    raise RuntimeError("環境変数 DISCORD_BOT_TOKEN を設定してください。")


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
        await self.send_button_message(interaction, "一覧機能は準備中")

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


def main() -> None:
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
