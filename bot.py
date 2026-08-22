import os
import sys
import asyncio
import logging
import aiosqlite
import discord
from discord.ext import commands
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from flask import Flask
import threading

for var in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(var, None)

TOKEN_DISCORD = "MTU0MDM2MDY0MzAwODQwNTU4NQ.GGgtnM.piU5QzFqktCrBDmZNViH1ZdO-VSbJOel8GGyuY"
TOKEN_TELEGRAM = "8865125561:AAEMFzX6zW425mSFsu2pCs4WV8SNFGV3cVQ"
AUTHORIZED_USERS = [1324141374752165958]
MAX_GROUPS = 1
LOG_CHANNEL_ID = 1540022456079753231

DB_PATH = "groups.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LOGGER_ENABLED = False
telegram_app = None
discord_bot = None

intents = discord.Intents.default()
intents.message_content = True
discord_bot = commands.Bot(command_prefix="-", intents=intents)

flask_app = Flask(__name__)

@flask_app.route('/shutdown')
def shutdown():
    logger.info("Shutdown request received, stopping bot...")
    os._exit(0)

def run_flask():
    flask_app.run(host='0.0.0.0', port=8080)

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS groups (chat_id INTEGER PRIMARY KEY)")
        await db.commit()

async def add_group(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO groups (chat_id) VALUES (?)", (chat_id,))
        await db.commit()

async def remove_group(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM groups WHERE chat_id = ?", (chat_id,))
        await db.commit()

async def get_all_groups():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT chat_id FROM groups")
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

async def get_group_count():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM groups")
        count = await cursor.fetchone()
        return count[0]

async def track_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            chat_id = update.effective_chat.id
            current_count = await get_group_count()
            if current_count >= MAX_GROUPS:
                await update.message.reply_text(f"Cannot add to more groups. Maximum limit is {MAX_GROUPS} groups.")
                return
            await add_group(chat_id)
            logger.info(f"Added group {chat_id}")
            await update.message.reply_text(f"I have been added to this group. Current groups: {current_count + 1}/{MAX_GROUPS}")
            break

async def track_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.left_chat_member and update.message.left_chat_member.id == context.bot.id:
        chat_id = update.effective_chat.id
        await remove_group(chat_id)
        logger.info(f"Removed group {chat_id}")

async def telegram_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LOGGER_ENABLED
    if not LOGGER_ENABLED:
        return
    if not update.message or not update.message.text:
        return
    if update.effective_user and update.effective_user.id == context.bot.id:
        return
    if update.effective_chat.type == "private":
        return
    user_name = update.effective_user.full_name if update.effective_user else "Unknown"
    platform = "Telegram"
    text = update.message.text
    logger.info(f"Telegram message from {user_name} in group: {text}")
    channel = discord_bot.get_channel(LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await discord_bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception as e:
            logger.warning(f"Failed to fetch Discord channel {LOG_CHANNEL_ID}: {e}")
            return
    if channel:
        try:
            await channel.send(f"message from {user_name} on {platform}\n{text}")
            logger.info(f"Message sent to Discord channel {LOG_CHANNEL_ID}")
        except Exception as e:
            logger.warning(f"Failed to send to Discord channel: {e}")

@discord_bot.event
async def on_ready():
    logger.info(f"Discord bot logged in as {discord_bot.user}")

@discord_bot.event
async def on_message(message):
    global LOGGER_ENABLED
    if message.author.bot:
        return
    if message.content.startswith("-"):
        await discord_bot.process_commands(message)
        return
    if LOGGER_ENABLED and message.guild:
        user_name = message.author.display_name
        platform = "Discord"
        text = message.content
        groups = await get_all_groups()
        for chat_id in groups:
            try:
                await telegram_app.bot.send_message(chat_id=chat_id, text=f"message from {user_name} on {platform}\n{text}")
            except Exception as e:
                logger.warning(f"Failed to send to {chat_id}: {e}")
    await discord_bot.process_commands(message)

@discord_bot.command(name="global")
async def global_command(ctx, link: str = None):
    if -1 not in AUTHORIZED_USERS and ctx.author.id not in AUTHORIZED_USERS:
        await ctx.send("You are not authorized to use this command.")
        return
    if not link:
        await ctx.send("Usage: -global <link>")
        return
    groups = await get_all_groups()
    if not groups:
        await ctx.send("No groups found in database.")
        return
    success = 0
    fail = 0
    for chat_id in groups:
        try:
            await telegram_app.bot.send_message(chat_id=chat_id, text=f"Broadcast from Discord: {link}")
            success += 1
        except Exception as e:
            logger.warning(f"Failed to send to {chat_id}: {e}")
            if "kicked" in str(e) or "chat not found" in str(e):
                await remove_group(chat_id)
            fail += 1
    await ctx.send(f"Sent to {success} groups, failed {fail} groups.")

@discord_bot.command(name="logger")
async def logger_command(ctx):
    global LOGGER_ENABLED
    if -1 not in AUTHORIZED_USERS and ctx.author.id not in AUTHORIZED_USERS:
        await ctx.send("You are not authorized to use this command.")
        return
    LOGGER_ENABLED = not LOGGER_ENABLED
    status = "enabled" if LOGGER_ENABLED else "disabled"
    await ctx.send(f"Message logger is now {status}.")

async def run_bots():
    global telegram_app
    await init_db()
    request = HTTPXRequest(proxy=None)
    telegram_app = Application.builder().token(TOKEN_TELEGRAM).request(request).build()
    telegram_app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, track_new_members))
    telegram_app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, track_left_member))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_message_handler))
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    await discord_bot.start(TOKEN_DISCORD)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_bots())
