import os
import asyncio
import re
import json
import shutil
from datetime import datetime, timedelta

import disnake as discord
from disnake.ext import commands, tasks

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
COUNTDOWN_STATE_FILE = os.path.join("countdowns.json")
# Whether anyone can use !bell (True) or only allowed users/roles (False)
bell_public_enabled = BELL_PUBLIC_ENABLED_DEFAULT

# Simple manual help text for commands we expose
COMMAND_DESCRIPTIONS = {
    "bell": "Ring the bell once in the configured voice channel (respects public/restricted).",
    "bellpublic": "Toggle whether !bell is public or restricted to allowed users/roles.",
    "clock": "Hourly clock control: on/off/status.",
    "time": "Show Discord timestamp formats (auto-localized for each viewer).",
    "timestamp": "Build custom Discord timestamp code. Example: !timestamp tmrw at 0918 am",
    "timer": "Start a live countdown message. Example: !timer 15 or !timer 1h 30m",
    "timerstop": "Stop the active timer in this channel.",
    "commands": "List available commands and what they do.",
}

# Global state for the hourly clock
clocktower_enabled = True  # !clock on/off changes this
last_rung_hour = None  # Keeps track of the last hour we rang so we don't double-ring
active_countdowns = {}  # channel_id -> asyncio.Task
countdown_recovery_done = False
bell_play_lock = asyncio.Lock()  # Prevent overlapping voice joins/plays
last_bell_attempt_monotonic = 0.0
BELL_MIN_GAP_SECONDS = 8.0
voice_connect_blocked_reason = ""  # Set when Discord rejects non-DAVE voice connects (code 4017)

# ---------- INTENTS SETUP ----------

intents = discord.Intents.default()
intents.message_content = True  # So we can read !bell and !clock commands
intents.members = True  # So we can see which users are in the voice channel
intents.voice_states = True  # So we can see voice channel states

# Bot with "!" as prefix
bot = commands.Bot(command_prefix="!", intents=intents)


def get_voice_stack_status() -> str:
    try:
        import disnake.voice_client as voice_client_mod

        has_dave = bool(getattr(voice_client_mod, "has_dave", False))
        return f"Voice stack: disnake {discord.__version__}, dave.py loaded={has_dave}"
    except Exception as e:
        return f"Voice stack check failed: {e}"


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


def parse_time_text(time_text: str) -> tuple[int, int] | tuple[None, None]:
    cleaned = time_text.strip().lower().replace(".", "")
    compact = re.sub(r"\s+", "", cleaned)

    # 12-hour examples: 9am, 9:18am, 0918am, 09:18 am
    m_12h = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(am|pm)", compact)
    if m_12h:
        hour = int(m_12h.group(1))
        minute = int(m_12h.group(2) or "0")
        ampm = m_12h.group(3)
        if not (1 <= hour <= 12 and 0 <= minute <= 59):
            return None, None
        if ampm == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        return hour, minute

    # 24-hour examples: 09:18
    m_24h_colon = re.fullmatch(r"(\d{1,2}):(\d{2})", compact)
    if m_24h_colon:
        hour = int(m_24h_colon.group(1))
        minute = int(m_24h_colon.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None, None

    # 24-hour compact examples: 918, 0918, 2118
    m_24h_compact = re.fullmatch(r"\d{3,4}", compact)
    if m_24h_compact:
        digits = compact
        if len(digits) == 3:
            hour = int(digits[0])
            minute = int(digits[1:])
        else:
            hour = int(digits[:2])
            minute = int(digits[2:])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute

    return None, None


def parse_duration_text(duration_text: str) -> timedelta | None:
    # Examples: "2h", "2h 30m", "3 days", "1 week 2 days"
    token_pattern = re.compile(
        r"(\d+)\s*(w|week|weeks|d|day|days|h|hr|hrs|hour|hours|m|min|mins|minute|minutes)",
        re.IGNORECASE,
    )

    cleaned = duration_text.replace(",", " ").strip()
    if not cleaned:
        return None

    total = timedelta(0)
    consumed_ranges = []

    for m in token_pattern.finditer(cleaned):
        value = int(m.group(1))
        unit = m.group(2).lower()
        consumed_ranges.append((m.start(), m.end()))

        if unit in {"w", "week", "weeks"}:
            total += timedelta(weeks=value)
        elif unit in {"d", "day", "days"}:
            total += timedelta(days=value)
        elif unit in {"h", "hr", "hrs", "hour", "hours"}:
            total += timedelta(hours=value)
        elif unit in {"m", "min", "mins", "minute", "minutes"}:
            total += timedelta(minutes=value)

    if not consumed_ranges:
        return None

    # Verify there's no unexpected content between parsed tokens
    cursor = 0
    for start, end in consumed_ranges:
        gap = cleaned[cursor:start].strip()
        if gap:
            return None
        cursor = end
    if cleaned[cursor:].strip():
        return None

    return total if total > timedelta(0) else None


def parse_when_input(when_text: str) -> tuple[datetime | None, str | None]:
    now = datetime.now().astimezone()
    raw = when_text.strip()
    lowered = raw.lower()

    # Relative-duration forms:
    # "in 2h", "in 2h 30m", "in 3 days at 9:18am"
    in_match = re.fullmatch(r"in\s+(.+?)(?:\s+at\s+(.+))?", lowered)
    if in_match:
        duration_text = in_match.group(1).strip()
        at_time_text = (in_match.group(2) or "").strip()

        delta = parse_duration_text(duration_text)
        if delta is None:
            return None, "Could not parse the duration part."

        dt = now + delta
        if at_time_text:
            hour, minute = parse_time_text(at_time_text)
            if hour is None:
                return None, "Could not parse the time part after `at`."
            dt = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)

        return dt, None

    # Relative-date forms: today/tomorrow/tmrw/tmr at <time>
    rel = re.fullmatch(r"(today|tomorrow|tmrw|tmr)\s*(?:at\s*)?(.+)", lowered)
    if rel:
        day_token = rel.group(1)
        time_token = rel.group(2).strip()
        hour, minute = parse_time_text(time_token)
        if hour is None:
            return None, "Could not parse the time part."

        date_value = now.date()
        if day_token in {"tomorrow", "tmrw", "tmr"}:
            date_value = date_value + timedelta(days=1)

        dt = datetime(
            date_value.year,
            date_value.month,
            date_value.day,
            hour,
            minute,
            tzinfo=now.tzinfo,
        )
        return dt, None

    # Explicit-date forms: YYYY-MM-DD 09:18 / MM/DD/YYYY 0918am
    for date_fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        parts = raw.split(maxsplit=1)
        if len(parts) != 2:
            continue
        date_token, time_token = parts[0], parts[1]
        try:
            date_value = datetime.strptime(date_token, date_fmt).date()
        except ValueError:
            continue

        hour, minute = parse_time_text(time_token)
        if hour is None:
            return None, "Could not parse the time part."

        dt = datetime(
            date_value.year,
            date_value.month,
            date_value.day,
            hour,
            minute,
            tzinfo=now.tzinfo,
        )
        return dt, None

    return None, "Could not parse date/time. Try `in 2h 30m`, `tmrw at 0918 am`, or `2026-03-02 09:18`."


# ---------- HELPER: PLAY THE BELL IN THE VOICE CHANNEL ----------

async def play_bell_once(trigger: str = "unknown") -> bool:
    """Connects to the configured voice channel, plays the bell sound once, then disconnects."""
    global last_bell_attempt_monotonic
    global voice_connect_blocked_reason
    global clocktower_enabled

    async with bell_play_lock:
        now_mono = asyncio.get_running_loop().time()
        since_last = now_mono - last_bell_attempt_monotonic
        if since_last < BELL_MIN_GAP_SECONDS:
            print(
                f"Skipping bell trigger {trigger!r}: only {since_last:.2f}s since previous bell attempt."
            )
            return False
        last_bell_attempt_monotonic = now_mono

        print(f"Bell trigger received: {trigger!r}")

        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            print("Guild not found. Check GUILD_ID.")
            return False

        channel = guild.get_channel(VOICE_CHANNEL_ID)
        if channel is None or not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            print("Voice channel not found or is not a voice channel. Check VOICE_CHANNEL_ID.")
            return False

        if voice_connect_blocked_reason and isinstance(channel, discord.VoiceChannel):
            print(f"Bell blocked: {voice_connect_blocked_reason}")
            return False

        # Check that the sound file exists
        if not os.path.isfile(BELL_FILE):
            print(f"Bell file not found at: {BELL_FILE}")
            return False

        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            print("ffmpeg was not found on PATH. Install ffmpeg and restart the bot shell.")
            return False

        me = guild.me or guild.get_member(bot.user.id if bot.user else 0)
        if me is None:
            print("Could not resolve bot member object in guild.")
            return False

        perms = channel.permissions_for(me)
        if not perms.connect:
            print(f"Bot lacks CONNECT permission in voice channel ID: {channel.id}")
            return False
        if not perms.speak:
            print(f"Bot lacks SPEAK permission in voice channel ID: {channel.id}")
            return False

        voice_client = guild.voice_client
        success = False

        try:
            if voice_client and voice_client.is_connected():
                # Move to correct channel if already connected somewhere in this guild
                if voice_client.channel.id != VOICE_CHANNEL_ID:
                    await voice_client.move_to(channel)
            else:
                # Not connected yet, connect now
                voice_client = await channel.connect(reconnect=False, timeout=20.0)

            # Create FFmpeg audio source
            audio_source = discord.FFmpegPCMAudio(BELL_FILE, executable=ffmpeg_path)

            # If something is already playing, stop it
            if voice_client.is_playing():
                voice_client.stop()

            playback_done = asyncio.Event()
            playback_error = None

            def on_playback_done(error):
                nonlocal playback_error
                playback_error = error
                bot.loop.call_soon_threadsafe(playback_done.set)

            voice_client.play(audio_source, after=on_playback_done)
            print(f"Bell playback started in channel ID {channel.id}.")

            # Wait for ffmpeg/voice playback thread to complete or error out
            await asyncio.wait_for(playback_done.wait(), timeout=120)

            if playback_error:
                print(f"Bell playback error: {playback_error}")
            else:
                print("Bell finished playing.")
                success = True

        except asyncio.TimeoutError:
            print("Timed out while waiting for bell playback to complete.")
            if voice_client and voice_client.is_connected() and voice_client.is_playing():
                voice_client.stop()
        except discord.ConnectionClosed as e:
            if getattr(e, "code", None) == 4017:
                try:
                    import disnake.voice_client as voice_client_mod

                    has_dave = bool(getattr(voice_client_mod, "has_dave", False))
                except Exception:
                    has_dave = False

                if not has_dave:
                    voice_connect_blocked_reason = (
                        "Discord rejected non-stage voice connect with code 4017 "
                        "(DAVE/E2EE required for regular voice channels)."
                    )
                    clocktower_enabled = False
                    print(
                        "Voice connect failed with code 4017 and dave.py is not active. "
                        "Hourly bell has been auto-disabled. Use a Stage channel or install dave.py."
                    )
                else:
                    print(
                        "Voice connect failed with code 4017 even though dave.py is active. "
                        "This may be a Discord-side voice routing issue; retry shortly."
                    )
            else:
                print(f"Voice connection closed during bell playback setup: {e}")
        except Exception as e:
            print(f"Error while playing bell: {e}")

        finally:
            # Disconnect after playing
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect()
                print("Disconnected from voice channel.")

        return success


def load_countdown_state() -> dict:
    if not os.path.isfile(COUNTDOWN_STATE_FILE):
        return {}
    try:
        with open(COUNTDOWN_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"Failed to load countdown state: {e}")
        return {}


def save_countdown_state(state: dict):
    tmp_path = f"{COUNTDOWN_STATE_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, COUNTDOWN_STATE_FILE)


def remember_countdown(channel_id: int, end_unix: int, message_id: int):
    state = load_countdown_state()
    state[str(channel_id)] = {
        "end_unix": int(end_unix),
        "message_id": int(message_id),
    }
    save_countdown_state(state)


def forget_countdown(channel_id: int | str) -> bool:
    state = load_countdown_state()
    removed = state.pop(str(channel_id), None)
    if removed is not None:
        save_countdown_state(state)
    return removed is not None


def register_countdown_task(channel_id: int, task: asyncio.Task):
    active_countdowns[channel_id] = task

    def _countdown_done(t: asyncio.Task):
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Countdown task exception in channel {channel_id}: {e}")

    task.add_done_callback(_countdown_done)


async def recover_countdowns():
    state = load_countdown_state()
    if not state:
        return

    for channel_id_text, entry in list(state.items()):
        try:
            channel_id = int(channel_id_text)
            end_unix = int(entry.get("end_unix"))
            message_id = int(entry.get("message_id"))
        except Exception:
            forget_countdown(channel_id_text)
            continue

        if channel_id in active_countdowns and not active_countdowns[channel_id].done():
            continue

        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                channel = None

        if not isinstance(channel, discord.TextChannel):
            forget_countdown(channel_id)
            continue

        status_message = None
        try:
            status_message = await channel.fetch_message(message_id)
        except Exception:
            status_message = None

        end_time = datetime.fromtimestamp(end_unix).astimezone()
        task = asyncio.create_task(run_countdown(channel, end_time, status_message=status_message))
        register_countdown_task(channel_id, task)
        print(
            f"Recovered countdown in channel {channel_id} "
            f"(message {message_id if status_message else 'missing'})."
        )


async def run_countdown(
    channel: discord.TextChannel,
    end_time: datetime,
    status_message: discord.Message | None = None,
):
    end_unix = int(end_time.timestamp())
    if status_message is None:
        status_message = await channel.send(
            f"Countdown started. Ends at <t:{end_unix}:F>.\n"
            f"Time remaining: <t:{end_unix}:R>"
        )
    remember_countdown(channel.id, end_unix, status_message.id)

    try:
        while True:
            now = datetime.now().astimezone()
            remaining_seconds = int((end_time - now).total_seconds())

            if remaining_seconds <= 0:
                await status_message.edit(
                    content=(
                        "###############################\n"
                        "#         TIMER OVER          #\n"
                        "#  The bell fades into night  #\n"
                        "#   and time lies in shadow   #\n"
                        "###############################"
                    )
                )
                return

            # Round up to whole minutes so users see a stable minute countdown.
            remaining_minutes = (remaining_seconds + 59) // 60
            await status_message.edit(
                content=(
                    f"Countdown: **{remaining_minutes} minute(s)** left.\n"
                    f"Ends at <t:{end_unix}:F> (<t:{end_unix}:R>)"
                )
            )

            # Short timers feel better with faster updates.
            await asyncio.sleep(10 if remaining_seconds <= 300 else 60)
    except asyncio.CancelledError:
        await status_message.edit(content="Countdown cancelled.")
        raise
    except Exception as e:
        print(f"Countdown failed in channel {channel.id}: {e}")
        try:
            await channel.send(f"Countdown failed: {e}")
        except Exception:
            pass
    finally:
        this_task = asyncio.current_task()
        if active_countdowns.get(channel.id) is this_task:
            active_countdowns.pop(channel.id, None)
            forget_countdown(channel.id)


# ---------- COMMAND: !bell ----------

@bot.command(name="bell")
async def bell_command(ctx: commands.Context):
    """Manually trigger the bell: bot joins vc, plays sound, leaves."""
    global voice_connect_blocked_reason

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
    ok = await play_bell_once(trigger=f"manual by {ctx.author} ({ctx.author.id})")
    if not ok:
        if voice_connect_blocked_reason:
            await ctx.send(
                "Bell failed: Discord now requires DAVE/E2EE for regular voice channels. "
                "Use a Stage channel for now."
            )
        else:
            await ctx.send("Bell failed to play. Check the bot terminal logs for the exact voice error.")


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


@bot.command(name="time")
async def time_command(ctx: commands.Context):
    """
    Discord renders <t:...> timestamps in each viewer's local timezone.
    """
    now = datetime.now()
    now_unix = int(now.timestamp())
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    next_hour_unix = int(next_hour.timestamp())

    await ctx.send(
        "Current time: "
        f"<t:{now_unix}:F>\n"
        "Relative: "
        f"<t:{now_unix}:R>\n"
        "Next top of hour: "
        f"<t:{next_hour_unix}:t> (<t:{next_hour_unix}:R>)"
    )


@bot.command(name="timestamp", aliases=["ts"])
async def timestamp_command(ctx: commands.Context, *, when: str = None):
    """
    Build raw Discord timestamp code from user input.
    Examples:
    - !timestamp tmrw at 0918 am
    - !timestamp in 2h 30m
    - !timestamp in 3 days at 9pm
    - !timestamp today 21:18
    - !timestamp 2026-03-02 09:18
    """
    if not when:
        await ctx.send(
            "Usage: `!timestamp <when>`\n"
            "Examples:\n"
            "`!timestamp tmrw at 0918 am`\n"
            "`!timestamp in 2h 30m`\n"
            "`!timestamp in 3 days at 9pm`\n"
            "`!timestamp today 21:18`\n"
            "`!timestamp 2026-03-02 09:18`"
        )
        return

    parsed_dt, error = parse_when_input(when)
    if error:
        await ctx.send(f"{error}\nExample: `!timestamp tmrw at 0918 am`")
        return

    now = datetime.now().astimezone()
    if parsed_dt <= now:
        await ctx.send(
            "That resolves to a past time. Use a future value like "
            "`!timestamp in 2h` or `!timestamp tmrw at 0918 am`."
        )
        return

    unix_ts = int(parsed_dt.timestamp())
    code_full = f"<t:{unix_ts}:F>"
    code_short = f"<t:{unix_ts}:t>"
    code_date = f"<t:{unix_ts}:d>"
    code_relative = f"<t:{unix_ts}:R>"

    await ctx.send(
        f"Input interpreted as: `{parsed_dt.isoformat()}`\n"
        f"Raw code (date+time): `{code_full}`\n"
        f"Raw code (time): `{code_short}`\n"
        f"Raw code (date): `{code_date}`\n"
        f"Raw code (relative): `{code_relative}`\n"
        f"Preview: {code_full}"
    )


@bot.command(name="timer", aliases=["countdown", "cd"])
async def timer_command(ctx: commands.Context, *, duration: str = None):
    """
    Start a live timer based on a duration.
    Examples:
    - !timer 15
    - !timer 15m
    - !timer 1h 30m
    """
    if not duration:
        await ctx.send(
            "Usage: `!timer <duration>`\n"
            "Examples:\n"
            "`!timer 15`\n"
            "`!timer 15m`\n"
            "`!timer 1h 30m`"
        )
        return

    text = duration.strip().lower()
    delta = None

    # Bare integer means minutes
    if re.fullmatch(r"\d+", text):
        delta = timedelta(minutes=int(text))
    else:
        delta = parse_duration_text(text)

    if delta is None or delta <= timedelta(0):
        await ctx.send("Could not parse duration. Try `!timer 15` or `!timer 1h 30m`.")
        return

    existing = active_countdowns.get(ctx.channel.id)
    if existing and not existing.done():
        existing.cancel()

    end_time = datetime.now().astimezone() + delta
    task = asyncio.create_task(run_countdown(ctx.channel, end_time))
    register_countdown_task(ctx.channel.id, task)
    await ctx.send(f"Starting countdown for `{duration}`.")


@bot.command(name="timerstop", aliases=["countdownstop", "cdstop"])
async def timer_stop_command(ctx: commands.Context):
    task = active_countdowns.get(ctx.channel.id)
    if task is None or task.done():
        if forget_countdown(ctx.channel.id):
            await ctx.send("No active in-memory timer, but cleared saved timer state.")
        else:
            await ctx.send("There is no active timer in this channel.")
        active_countdowns.pop(ctx.channel.id, None)
        return

    task.cancel()
    await ctx.send("Stopping timer...")


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
    if channel is None or not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
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
    await play_bell_once(trigger="hourly task")
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
    global countdown_recovery_done

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    print(get_voice_stack_status())

    # Simple check that our audio file exists
    if not os.path.isfile(BELL_FILE):
        print(f"WARNING: Bell file not found at {BELL_FILE}. Make sure it exists.")

    # Start the hourly task if not already running
    if not hourly_bell_task.is_running():
        hourly_bell_task.start()
        print("Hourly bell task has been started.")

    if not countdown_recovery_done:
        await recover_countdowns()
        countdown_recovery_done = True


# ---------- MAIN ENTRY POINT ----------

if __name__ == "__main__":
    bot.run(TOKEN)
