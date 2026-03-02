# Clocktower Bot

Discord voice bell bot with:
- `!bell` for manual ring in the configured voice channel
- `!bellpublic` to toggle public/restricted bell access
- `!clock on|off|status` hourly bell control

## Setup (Windows PowerShell)

```powershell
cd "c:\Users\airex\OneDrive\Desktop\Clocktower Bot"
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Create `.env` from `.env.example` and fill values.

## Run

```powershell
python bot.py
```

## Notes

- `ffmpeg` must be installed and available on PATH.
- In Discord Developer Portal, enable Message Content Intent.
- Do not commit `.env`.