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
import io

for var in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(var, None)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN_DISCORD = os.getenv("TOKEN_DISCORD")
TOKEN_TELEGRAM = os.getenv("TOKEN_TELEGRAM")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "123456789"))
MAX_GROUPS = 1
MAX_FILE_SIZE = 20 * 1024 * 1024

auth_users_str = os.getenv("AUTHORIZED_USERS", "-1")
try:
    AUTHORIZED_USERS = [int(x.strip()) for x in auth_users_str.split(",") if x.strip()]
except ValueError:
    AUTHORIZED_USERS = [-1]

static_groups_str = os.getenv("STATIC_GROUPS", "")
STATIC_GROUPS = []
if static_groups_str:
    try:
        STATIC_GROUPS = [int(x.strip()) for x in static_groups_str.split(",") if x.strip()]
        logger.info(f"Static groups loaded: {STATIC_GROUPS}")
    except ValueError:
        logger.warning("Invalid STATIC_GROUPS format.")
else:
    logger.info("STATIC_GROUPS not set, using database mode")

DB_PATH = "groups.db"
LOGGER_ENABLED = False
telegram_app = None
discord_bot = None

intents = discord.Intents.default()
intents.message_content = True
discord_bot = commands.Bot(command_prefix="-", intents=intents)

flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is running!"

@flask_app.route('/health')
def health():
    return "OK", 200

@flask_app.route('/shutdown')
def shutdown():
    logger.info("Shutdown request received, stopping bot...")
    os._exit(0)

def run_flask():
    flask_app.run(host='0.0.0.0', port=8080, threaded=True)

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS groups (chat_id INTEGER PRIMARY KEY)")
        await db.commit()

async def add_group(chat_id: int):
    if STATIC_GROUPS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO groups (chat_id) VALUES (?)", (chat_id,))
        await db.commit()

async def remove_group(chat_id: int):
    if STATIC_GROUPS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM groups WHERE chat_id = ?", (chat_id,))
        await db.commit()

async def get_all_groups():
    if STATIC_GROUPS:
        return STATIC_GROUPS.copy()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT chat_id FROM groups")
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

async def get_group_count():
    if STATIC_GROUPS:
        return len(STATIC_GROUPS)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM groups")
        count = await cursor.fetchone()
        return count[0]

async def track_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if STATIC_GROUPS:
        return
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
    if STATIC_GROUPS:
        return
    if update.message.left_chat_member and update.message.left_chat_member.id == context.bot.id:
        chat_id = update.effective_chat.id
        await remove_group(chat_id)
        logger.info(f"Removed group {chat_id}")

async def download_telegram_file(file, context):
    try:
        file_obj = await context.bot.get_file(file.file_id)
        file_bytes = await file_obj.download_as_bytearray()
        return file_bytes, file.file_name if file.file_name else f"file_{file.file_id}.bin"
    except Exception as e:
        logger.error(f"Failed to download file: {e}")
        return None, None

async def send_file_to_discord(channel, file_bytes, filename, caption):
    try:
        file_obj = discord.File(io.BytesIO(file_bytes), filename=filename)
        await channel.send(file=file_obj, content=caption if caption else None)
        logger.info(f"File {filename} sent to Discord")
        return True
    except Exception as e:
        logger.error(f"Failed to send file to Discord: {e}")
        return False

async def send_file_to_telegram(chat_id, file_bytes, filename, caption):
    try:
        file_obj = io.BytesIO(file_bytes)
        file_obj.name = filename
        await telegram_app.bot.send_document(chat_id=chat_id, document=file_obj, caption=caption)
        logger.info(f"File {filename} sent to Telegram group {chat_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send file to Telegram: {e}")
        return False

async def telegram_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LOGGER_ENABLED
    if not LOGGER_ENABLED:
        return
    if update.effective_user and update.effective_user.id == context.bot.id:
        return
    if update.effective_chat.type == "private":
        return
    user_name = update.effective_user.full_name if update.effective_user else "Unknown"
    platform = "Telegram"
    caption = None
    
    channel = discord_bot.get_channel(LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await discord_bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception as e:
            logger.warning(f"Failed to fetch Discord channel {LOG_CHANNEL_ID}: {e}")
            return
    if not channel:
        return

    if update.message and update.message.text:
        text = update.message.text
        logger.info(f"Telegram message from {user_name}: {text}")
        await channel.send(f"message from {user_name} on {platform}\n{text}")
        return

    if update.message and update.message.caption:
        caption = update.message.caption
        logger.info(f"Telegram media with caption from {user_name}: {caption[:100] if caption else ''}")

    if update.message and update.message.photo:
        file_id = update.message.photo[-1].file_id
        file = await context.bot.get_file(file_id)
        if file.file_size and file.file_size > MAX_FILE_SIZE:
            await channel.send(f"File too large ({file.file_size/1024/1024:.1f}MB). Max size: 20MB")
            return
        file_bytes, filename = await download_telegram_file(file, context)
        if file_bytes:
            await send_file_to_discord(channel, file_bytes, "image.jpg", f"photo from {user_name} on Telegram\n{caption if caption else ''}")
        return

    if update.message and update.message.document:
        file = update.message.document
        if file.file_size > MAX_FILE_SIZE:
            await channel.send(f"File too large ({file.file_size/1024/1024:.1f}MB). Max size: 20MB")
            return
        file_bytes, filename = await download_telegram_file(file, context)
        if file_bytes:
            await send_file_to_discord(channel, file_bytes, filename, f"document from {user_name} on Telegram\n{caption if caption else ''}")
        return

    if update.message and update.message.audio:
        file = update.message.audio
        if file.file_size > MAX_FILE_SIZE:
            await channel.send(f"File too large ({file.file_size/1024/1024:.1f}MB). Max size: 20MB")
            return
        file_bytes, filename = await download_telegram_file(file, context)
        if file_bytes:
            await send_file_to_discord(channel, file_bytes, filename, f"audio from {user_name} on Telegram\n{caption if caption else ''}")
        return

    if update.message and update.message.video:
        file = update.message.video
        if file.file_size > MAX_FILE_SIZE:
            await channel.send(f"File too large ({file.file_size/1024/1024:.1f}MB). Max size: 20MB")
            return
        file_bytes, filename = await download_telegram_file(file, context)
        if file_bytes:
            await send_file_to_discord(channel, file_bytes, filename, f"video from {user_name} on Telegram\n{caption if caption else ''}")
        return

    if update.message and update.message.voice:
        file = update.message.voice
        if file.file_size > MAX_FILE_SIZE:
            await channel.send(f"File too large ({file.file_size/1024/1024:.1f}MB). Max size: 20MB")
            return
        file_bytes, filename = await download_telegram_file(file, context)
        if file_bytes:
            await send_file_to_discord(channel, file_bytes, "voice.ogg", f"voice from {user_name} on Telegram\n{caption if caption else ''}")
        return

    if update.message and update.message.video_note:
        file = update.message.video_note
        if file.file_size > MAX_FILE_SIZE:
            await channel.send(f"File too large ({file.file_size/1024/1024:.1f}MB). Max size: 20MB")
            return
        file_bytes, filename = await download_telegram_file(file, context)
        if file_bytes:
            await send_file_to_discord(channel, file_bytes, "video_note.mp4", f"video note from {user_name} on Telegram\n{caption if caption else ''}")
        return

    if update.message and update.message.animation:
        file = update.message.animation
        if file.file_size > MAX_FILE_SIZE:
            await channel.send(f"File too large ({file.file_size/1024/1024:.1f}MB). Max size: 20MB")
            return
        file_bytes, filename = await download_telegram_file(file, context)
        if file_bytes:
            await send_file_to_discord(channel, file_bytes, filename, f"animation from {user_name} on Telegram\n{caption if caption else ''}")
        return

    if update.message and update.message.sticker:
        file = update.message.sticker
        file_bytes, filename = await download_telegram_file(file, context)
        if file_bytes:
            await send_file_to_discord(channel, file_bytes, "sticker.webp", f"sticker from {user_name} on Telegram")
        return

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
    if not LOGGER_ENABLED or not message.guild:
        await discord_bot.process_commands(message)
        return

    user_name = message.author.display_name
    platform = "Discord"
    groups = await get_all_groups()
    if not groups:
        await discord_bot.process_commands(message)
        return

    if message.content and not message.attachments:
        for chat_id in groups:
            try:
                await telegram_app.bot.send_message(chat_id=chat_id, text=f"message from {user_name} on {platform}\n{message.content}")
            except Exception as e:
                logger.warning(f"Failed to send to {chat_id}: {e}")
        await discord_bot.process_commands(message)
        return

    if message.attachments:
        for attachment in message.attachments:
            if attachment.size > MAX_FILE_SIZE:
                await message.channel.send(f"File too large ({attachment.size/1024/1024:.1f}MB). Max size: 20MB")
                continue
            try:
                file_bytes = await attachment.read()
                for chat_id in groups:
                    await send_file_to_telegram(chat_id, file_bytes, attachment.filename, f"file from {user_name} on Discord")
            except Exception as e:
                logger.error(f"Failed to process attachment: {e}")
                await message.channel.send(f"Failed to send file: {e}")

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
    telegram_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, telegram_message_handler))
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    await discord_bot.start(TOKEN_DISCORD)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_bots())
