import os
import asyncio
from datetime import datetime

import discord
from discord.ext import commands, tasks

from config import (
    TOKEN,
    GUILD_ID,
    VOICE_CHANNEL_ID,
    OWNER_ID,
    CLOCK_ADMIN_ROLE_ID,
    BELL_ALLOWED_ROLE_ID,
    BELL_PUBLIC_ENABLED_DEFAULT,
)

# Path to the bell sound file
BELL_FILE = os.path.join("sounds", "clock.wav")
# Whether anyone can use !bell (True) or only allowed users/roles (False)
bell_public_enabled = BELL_PUBLIC_ENABLED_DEFAULT

# Simple manual help text for commands we expose
COMMAND_DESCRIPTIONS = {
    "bell": "Ring the bell once in the configured voice channel (respects public/restricted).",
    "bellpublic": "Toggle whether !bell is public or restricted to allowed users/roles.",
    "clock": "Hourly clock control: on/off/status.",
    "commands": "List available commands and what they do.",
}

# Global state for the hourly clock
clocktower_enabled = True  # !clock on/off changes this
last_rung_hour = None  # Keeps track of the last hour we rang so we don't double-ring

# ---------- INTENTS SETUP ----------

intents = discord.Intents.default()
intents.message_content = True  # So we can read !bell and !clock commands
intents.members = True  # So we can see which users are in the voice channel
intents.voice_states = True  # So we can see voice channel states

# Bot with "!" as prefix
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- HELPER: PERMISSION CHECK FOR !bell ----------

def has_bell_permission(ctx: commands.Context) -> bool:
    """
    Allow the owner or members with the specific role to use !bell.
    """
    if ctx.author.id == OWNER_ID:
        return True

    if BELL_ALLOWED_ROLE_ID and BELL_ALLOWED_ROLE_ID != 0:
        role = ctx.guild.get_role(BELL_ALLOWED_ROLE_ID)
        if role and role in ctx.author.roles:
            return True

    return False


def is_bell_admin():
    async def predicate(ctx: commands.Context) -> bool:
        if has_bell_permission(ctx):
            return True
        await ctx.send("You do not have permission to manage bell access.")
        return False

    return commands.check(predicate)


# ---------- HELPER: PERMISSION CHECK FOR !clock ----------

def is_clock_admin():
    async def predicate(ctx: commands.Context) -> bool:
        # Allow the owner by user ID
        if ctx.author.id == OWNER_ID:
            return True

        # Allow members with a specific role (if provided)
        if CLOCK_ADMIN_ROLE_ID and CLOCK_ADMIN_ROLE_ID != 0:
            role = ctx.guild.get_role(CLOCK_ADMIN_ROLE_ID)
            if role and role in ctx.author.roles:
                return True

        await ctx.send("You do not have permission to control the clock.")
        return False

    return commands.check(predicate)


# ---------- HELPER: PLAY THE BELL IN THE VOICE CHANNEL ----------

async def play_bell_once():
    """Connects to the configured voice channel, plays the bell sound once, then disconnects."""
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print("Guild not found. Check GUILD_ID.")
        return

    channel = guild.get_channel(VOICE_CHANNEL_ID)
    if channel is None or not isinstance(channel, discord.VoiceChannel):
        print("Voice channel not found or is not a voice channel. Check VOICE_CHANNEL_ID.")
        return

    # Check that the sound file exists
    if not os.path.isfile(BELL_FILE):
        print(f"Bell file not found at: {BELL_FILE}")
        return

    voice_client = guild.voice_client

    try:
        if voice_client and voice_client.is_connected():
            # Move to correct channel if already connected somewhere in this guild
            if voice_client.channel.id != VOICE_CHANNEL_ID:
                await voice_client.move_to(channel)
        else:
            # Not connected yet, connect now
            voice_client = await channel.connect()

        # Create FFmpeg audio source
        audio_source = discord.FFmpegPCMAudio(BELL_FILE)

        # If something is already playing, stop it
        if voice_client.is_playing():
            voice_client.stop()

        voice_client.play(audio_source)
        print("Bell is playing...")

        # Wait until playback finishes
        while voice_client.is_playing():
            await asyncio.sleep(0.5)

        print("Bell finished playing.")

    except Exception as e:
        print(f"Error while playing bell: {e}")

    finally:
        # Disconnect after playing
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect()
            print("Disconnected from voice channel.")


# ---------- COMMAND: !bell ----------

@bot.command(name="bell")
async def bell_command(ctx: commands.Context):
    """Manually trigger the bell: bot joins vc, plays sound, leaves."""
    # Permission check first to avoid unnecessary work
    if (not bell_public_enabled) and (not has_bell_permission(ctx)):
        await ctx.send("You do not have permission to use !bell.")
        return

    # Check if the user is in a voice channel
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.send("You must be in a voice channel to use !bell.")
        return

    # Optional: ensure they are in the configured channel
    if ctx.author.voice.channel.id != VOICE_CHANNEL_ID:
        await ctx.send("Please join the clocktower voice channel first.")
        return

    await ctx.send("Ringing the bell once...")
    await play_bell_once()


# ---------- COMMAND: !bellpublic (toggle public access to !bell) ----------

@bot.command(name="bellpublic")
@is_bell_admin()
async def bell_public_command(ctx: commands.Context, arg: str = None):
    """
    !bellpublic on/public/enable -> anyone can use !bell
    !bellpublic off/restricted   -> only allowed users/roles can use !bell
    !bellpublic                  -> show current mode
    """
    global bell_public_enabled

    if arg is None:
        status = "public" if bell_public_enabled else "restricted"
        await ctx.send(f"Bell access is currently **{status}**.")
        return

    arg = arg.lower()

    if arg in ("on", "enable", "public"):
        bell_public_enabled = True
        await ctx.send("Bell access set to public: anyone can use !bell.")
    elif arg in ("off", "disable", "restricted", "private"):
        bell_public_enabled = False
        await ctx.send("Bell access restricted: only allowed roles/users can use !bell.")
    else:
        await ctx.send("Use `!bellpublic on|off` (aliases: public/restricted).")


# ---------- COMMAND: !commands (list commands with descriptions) ----------

@bot.command(name="commands")
async def list_commands(ctx: commands.Context):
    lines = [f"!{name} - {desc}" for name, desc in COMMAND_DESCRIPTIONS.items()]
    msg = "Available commands:\n" + "\n".join(lines)
    await ctx.send(msg)


# ---------- HOURLY TASK ----------

@tasks.loop(minutes=1)
async def hourly_bell_task():
    """
    Runs every minute.
    If minute == 0 and clocktower is enabled and someone is in the target voice channel,
    play the bell once (but only once per hour).
    """
    global last_rung_hour

    now = datetime.now()  # local time
    current_hour = now.hour
    current_minute = now.minute

    # Only act at the top of the hour (minute == 0)
    if current_minute != 0:
        return

    if not clocktower_enabled:
        # Feature turned off
        return

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print("Hourly task: guild not found.")
        return

    channel = guild.get_channel(VOICE_CHANNEL_ID)
    if channel is None or not isinstance(channel, discord.VoiceChannel):
        print("Hourly task: voice channel not found or is not a voice channel.")
        return

    # Check for at least one non-bot member in the channel
    non_bot_members = [m for m in channel.members if not m.bot]
    if not non_bot_members:
        print("Hourly task: no non-bot users in the channel at the top of the hour.")
        return

    # Prevent ringing more than once per hour
    if last_rung_hour == current_hour:
        print("Hourly task: already rang this hour.")
        return

    print(f"Hourly task: ringing bell at hour {current_hour:02d}:00.")
    await play_bell_once()
    last_rung_hour = current_hour


@hourly_bell_task.before_loop
async def before_hourly_bell_task():
    print("Waiting for bot to be ready before starting hourly task...")
    await bot.wait_until_ready()
    print("Hourly task started.")


# ---------- COMMAND: !clock ----------

@bot.command(name="clock")
@is_clock_admin()
async def clock_command(ctx: commands.Context, arg: str = None):
    """
    !clock on     -> enable hourly bell
    !clock off    -> disable hourly bell
    !clock status -> show current state
    """
    global clocktower_enabled

    if arg is None:
        await ctx.send("Usage: `!clock on`, `!clock off`, or `!clock status`.")
        return

    arg = arg.lower()

    if arg == "on":
        clocktower_enabled = True
        await ctx.send("Hourly clocktower bell is now **ON**.")
    elif arg == "off":
        clocktower_enabled = False
        await ctx.send("Hourly clocktower bell is now **OFF**.")
    elif arg == "status":
        status = "ON" if clocktower_enabled else "OFF"
        await ctx.send(f"Hourly clocktower bell is currently **{status}**.")
    else:
        await ctx.send("Unknown option. Use `on`, `off`, or `status`.")


# ---------- EVENTS ----------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")

    # Simple check that our audio file exists
    if not os.path.isfile(BELL_FILE):
        print(f"WARNING: Bell file not found at {BELL_FILE}. Make sure it exists.")

    # Start the hourly task if not already running
    if not hourly_bell_task.is_running():
        hourly_bell_task.start()
        print("Hourly bell task has been started.")


# ---------- MAIN ENTRY POINT ----------

if __name__ == "__main__":
    bot.run(TOKEN)