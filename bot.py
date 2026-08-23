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
import time
from datetime import datetime, timezone
import html
import re

for var in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(var, None)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN_DISCORD = os.getenv("TOKEN_DISCORD")
TOKEN_TELEGRAM = os.getenv("TOKEN_TELEGRAM")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "123456789"))
MAX_GROUPS = 1
MAX_FILE_SIZE = 10 * 1024 * 1024

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
intents.messages = True
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

async def download_telegram_file(file_obj, context):
    try:
        file = await context.bot.get_file(file_obj.file_id)
        file_bytes = await file.download_as_bytearray()
        return file_bytes
    except Exception as e:
        logger.error(f"Failed to download file: {e}")
        return None

async def get_telegram_avatar_url(user_id, context):
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id
            file = await context.bot.get_file(file_id)
            # file.file_path قبلاً آدرس کامل است
            avatar_url = file.file_path
            logger.info(f"Generated avatar URL for user {user_id}: {avatar_url}")
            return avatar_url
    except Exception as e:
        logger.warning(f"Failed to get avatar URL for {user_id}: {e}")
    return None

def shorten_text(text, max_len=50):
    if not text:
        return ""
    clean = re.sub(r'<[^>]+>', '', text)
    if len(clean) <= max_len:
        return clean
    return clean[:max_len] + "..."

def escape_html(text):
    if not text:
        return ""
    return html.escape(text)

def get_discord_reply_text(message):
    if message.content:
        return message.content
    if message.embeds and message.embeds[0].description:
        return message.embeds[0].description
    return ""

def get_telegram_reply_text(message):
    if not message:
        return ""
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    return ""

async def send_file_to_discord(channel, file_bytes, filename, caption, user_name, platform, reply_info=None, avatar_url=None):
    try:
        embed = discord.Embed(
            description=caption if caption else "",
            color=8305407,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=user_name, icon_url=avatar_url if avatar_url else None)
        embed.set_footer(text=f"from {platform}")
        if reply_info:
            embed.add_field(name="Reply to", value=f"{reply_info['replied_user']}: \"{reply_info['short_text']}\"", inline=False)
        
        file_obj = discord.File(io.BytesIO(file_bytes), filename=filename)
        await channel.send(file=file_obj, embed=embed)
        logger.info(f"File {filename} sent to Discord with icon_url: {avatar_url}")
        return True
    except Exception as e:
        logger.error(f"Failed to send file to Discord: {e}")
        return False

async def send_file_to_telegram(chat_id, file_bytes, filename, caption, user_name, platform, reply_info=None):
    try:
        file_obj = io.BytesIO(file_bytes)
        file_obj.name = filename
        escaped_name = escape_html(user_name)
        escaped_caption = escape_html(caption) if caption else ""
        text = f"<b>{escaped_name}</b> <code>(on {platform})</code>"
        if escaped_caption:
            text += f"\n<blockquote>{escaped_caption}</blockquote>"
        if reply_info:
            replied_user = escape_html(reply_info['replied_user'])
            short_text = escape_html(reply_info['short_text'])
            text += f"\n\n<blockquote><b>{replied_user}:</b> {short_text}</blockquote>"
        
        ext = filename.split('.')[-1].lower()
        if ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
            await telegram_app.bot.send_photo(chat_id=chat_id, photo=file_obj, caption=text, parse_mode='HTML')
        elif ext in ['mp4', 'mov', 'avi', 'mkv']:
            await telegram_app.bot.send_video(chat_id=chat_id, video=file_obj, caption=text, parse_mode='HTML')
        elif ext in ['mp3', 'wav', 'ogg', 'flac']:
            await telegram_app.bot.send_audio(chat_id=chat_id, audio=file_obj, caption=text, parse_mode='HTML')
        elif ext in ['ogg'] and filename.startswith('voice'):
            await telegram_app.bot.send_voice(chat_id=chat_id, voice=file_obj, caption=text, parse_mode='HTML')
        else:
            await telegram_app.bot.send_document(chat_id=chat_id, document=file_obj, caption=text, parse_mode='HTML')
        logger.info(f"File {filename} sent to Telegram group {chat_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send file to Telegram: {e}")
        return False

async def send_text_to_discord(channel, text, user_name, avatar_url=None, reply_info=None):
    try:
        embed = discord.Embed(
            description=text,
            color=8305407,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=user_name, icon_url=avatar_url if avatar_url else None)
        embed.set_footer(text="from Telegram")
        if reply_info:
            embed.add_field(name="Reply to", value=f"{reply_info['replied_user']}: \"{reply_info['short_text']}\"", inline=False)
        await channel.send(embed=embed)
        logger.info(f"Text message sent to Discord with icon_url: {avatar_url}")
    except Exception as e:
        logger.error(f"Failed to send text to Discord: {e}")

async def telegram_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LOGGER_ENABLED
    if not LOGGER_ENABLED:
        return
    if update.effective_user and update.effective_user.id == context.bot.id:
        return
    if update.effective_chat.type == "private":
        return
    
    user_name = update.effective_user.full_name if update.effective_user else "Unknown"
    user_id = update.effective_user.id if update.effective_user else None
    platform = "Telegram"
    channel = discord_bot.get_channel(LOG_CHANNEL_ID)
    if not channel:
        try:
            channel = await discord_bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception as e:
            logger.warning(f"Failed to fetch Discord channel {LOG_CHANNEL_ID}: {e}")
            return
    if not channel:
        return

    msg = update.message
    caption = msg.caption if msg and msg.caption else ""
    
    avatar_url = await get_telegram_avatar_url(user_id, context) if user_id else None
    
    reply_info = None
    if msg and msg.reply_to_message:
        replied_msg = msg.reply_to_message
        replied_user = replied_msg.from_user.full_name if replied_msg.from_user else "Unknown"
        replied_text = get_telegram_reply_text(replied_msg)
        short = shorten_text(replied_text, 50)
        reply_info = {
            "replied_user": replied_user,
            "short_text": short
        }

    if msg and msg.text and not msg.photo and not msg.document and not msg.audio and not msg.video and not msg.voice and not msg.video_note and not msg.animation and not msg.sticker:
        await send_text_to_discord(channel, msg.text, user_name, avatar_url, reply_info)
        return

    if msg and (msg.photo or msg.document or msg.audio or msg.video or msg.voice or msg.video_note or msg.animation or msg.sticker):
        file_obj = None
        filename = ""
        if msg.photo:
            file_obj = await context.bot.get_file(msg.photo[-1].file_id)
            filename = f"photo_{int(time.time())}.jpg"
        elif msg.document:
            file_obj = await context.bot.get_file(msg.document.file_id)
            filename = msg.document.file_name or f"document_{int(time.time())}.bin"
        elif msg.audio:
            file_obj = await context.bot.get_file(msg.audio.file_id)
            filename = msg.audio.file_name or f"audio_{int(time.time())}.mp3"
        elif msg.video:
            file_obj = await context.bot.get_file(msg.video.file_id)
            filename = f"video_{int(time.time())}.mp4"
        elif msg.voice:
            file_obj = await context.bot.get_file(msg.voice.file_id)
            filename = f"voice_{int(time.time())}.ogg"
        elif msg.video_note:
            file_obj = await context.bot.get_file(msg.video_note.file_id)
            filename = f"video_note_{int(time.time())}.mp4"
        elif msg.animation:
            file_obj = await context.bot.get_file(msg.animation.file_id)
            filename = f"animation_{int(time.time())}.gif"
        elif msg.sticker:
            file_obj = await context.bot.get_file(msg.sticker.file_id)
            ext = "webp" if not msg.sticker.is_animated else "tgs"
            filename = f"sticker_{int(time.time())}.{ext}"
        
        if file_obj:
            if file_obj.file_size and file_obj.file_size > MAX_FILE_SIZE:
                await channel.send(f"File too large ({file_obj.file_size/1024/1024:.1f}MB). Max size: 10MB")
                return
            file_bytes = await download_telegram_file(file_obj, context)
            if file_bytes:
                await send_file_to_discord(channel, file_bytes, filename, caption, user_name, platform, reply_info, avatar_url)
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

    caption = message.content if message.content else ""
    
    reply_info = None
    if message.reference:
        try:
            ref_msg = await message.channel.fetch_message(message.reference.message_id)
            replied_user = ref_msg.author.display_name
            replied_text = get_discord_reply_text(ref_msg)
            short = shorten_text(replied_text, 50)
            reply_info = {
                "replied_user": replied_user,
                "short_text": short
            }
        except Exception as e:
            logger.warning(f"Failed to fetch replied message: {e}")

    if message.content and not message.attachments:
        escaped_name = escape_html(user_name)
        escaped_caption = escape_html(caption)
        text = f"<b>{escaped_name}</b> <code>(on {platform})</code>"
        if escaped_caption:
            text += f"\n<blockquote>{escaped_caption}</blockquote>"
        if reply_info:
            replied_user = escape_html(reply_info['replied_user'])
            short_text = escape_html(reply_info['short_text'])
            text += f"\n\n<blockquote><b>{replied_user}:</b> {short_text}</blockquote>"
        for chat_id in groups:
            try:
                await telegram_app.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
            except Exception as e:
                logger.warning(f"Failed to send to {chat_id}: {e}")
        await discord_bot.process_commands(message)
        return

    if message.attachments:
        for attachment in message.attachments:
            if attachment.size > MAX_FILE_SIZE:
                await message.channel.send(f"File too large ({attachment.size/1024/1024:.1f}MB). Max size: 10MB")
                continue
            try:
                file_bytes = await attachment.read()
                for chat_id in groups:
                    await send_file_to_telegram(chat_id, file_bytes, attachment.filename, caption, user_name, platform, reply_info)
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
    telegram_app.add_handler(MessageHandler(filters.ALL, telegram_message_handler))
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    await discord_bot.start(TOKEN_DISCORD)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_bots())
