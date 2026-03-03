# Clocktower Bot

A small Discord bot that rings a clocktower bell in voice and includes a few utility commands.

Built for one server and kept intentionally simple.

## What It Does

- Rings the bell at the top of each hour when at least one non-bot user is in the configured voice channel
- Lets members trigger a manual bell with `!bell` (public or restricted mode)
- Lets admins toggle hourly ringing with `!clock on|off|status`
- Includes timestamp helpers (`!time`, `!timestamp`)
- Includes channel countdown timers (`!timer`, `!timerstop`) with basic restart recovery

## Quick Start (Windows PowerShell)

```powershell
cd "c:\Users\airex\OneDrive\Desktop\Clocktower Bot"
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with your Discord IDs/token, then run:

```powershell
python bot.py
```

Optional local launcher:

```powershell
clocktower.bat
```

## Environment Variables

| Variable | Required | Notes |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Bot token from Discord Developer Portal |
| `GUILD_ID` | Yes | Server ID |
| `VOICE_CHANNEL_ID` | Yes | Voice channel where bell audio plays |
| `OWNER_ID` | Yes | Your Discord user ID |
| `CLOCK_ADMIN_ROLE_ID` | No | Role allowed to run `!clock` (`0` disables role check) |
| `BELL_ALLOWED_ROLE_ID` | No | Role allowed to use `!bell` when restricted (`0` disables role check) |
| `BELL_PUBLIC_ENABLED` | No | `true/false` default for `!bell` public mode |

## Commands

| Command | Purpose |
|---|---|
| `!bell` | Ring bell once (in configured voice channel) |
| `!bellpublic on/off` | Toggle manual bell access mode |
| `!clock on/off/status` | Control hourly bell loop |
| `!time` | Show Discord timestamp display examples |
| `!timestamp <when>` | Build future Discord timestamp code |
| `!timer <duration>` | Start a live countdown in current text channel |
| `!timerstop` | Stop current channel countdown |
| `!commands` | Print command list from bot |

## Notes

- `ffmpeg` must be installed and available on `PATH`.
- In Discord Developer Portal, enable **Message Content Intent** for this bot.
- Keep `.env` private and never commit real tokens.
