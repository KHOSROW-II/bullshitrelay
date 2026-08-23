async def get_telegram_avatar_bytes(user_id, context):
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id
            file = await context.bot.get_file(file_id)
            file_bytes = await file.download_as_bytearray()
            return file_bytes
    except Exception as e:
        logger.warning(f"Failed to get avatar for {user_id}: {e}")
    return None

async def send_file_to_discord(channel, file_bytes, filename, caption, user_name, platform, reply_info=None, avatar_bytes=None):
    try:
        files = []
        embed = discord.Embed(
            description=caption if caption else "",
            color=8305407,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=user_name)
        embed.set_footer(text=f"from {platform}")
        if reply_info:
            embed.add_field(name="Reply to", value=f"{reply_info['replied_user']}: \"{reply_info['short_text']}\"", inline=False)
        
        # اضافه کردن فایل اصلی
        main_file = discord.File(io.BytesIO(file_bytes), filename=filename)
        files.append(main_file)
        
        # اگر آواتار موجود باشد، آن را به عنوان thumbnail اضافه می‌کنیم
        if avatar_bytes:
            avatar_file = discord.File(io.BytesIO(avatar_bytes), filename="avatar.jpg")
            embed.set_thumbnail(url="attachment://avatar.jpg")
            files.append(avatar_file)
        
        await channel.send(files=files, embed=embed)
        logger.info(f"File {filename} sent to Discord with avatar thumbnail")
        return True
    except Exception as e:
        logger.error(f"Failed to send file to Discord: {e}")
        return False
