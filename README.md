<div align="center">

<img src="assets/banner.svg" alt="Login Test Bot" width="100%"/>

<br/>

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-MTProto-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)
![Railway](https://img.shields.io/badge/Deploy-Railway-0B0D0E?style=for-the-badge&logo=railway&logoColor=white)
![Render](https://img.shields.io/badge/Deploy-Render-46E3B7?style=for-the-badge&logo=render&logoColor=black)
![Status](https://img.shields.io/badge/Status-Active-22D3A6?style=for-the-badge)

**A focused diagnostic bot for Telegram user-account login.**
It does one thing only: log in, and show exactly how Telegram delivers the code.

</div>

---

## Table of Contents

- [Overview](#overview)
- [Login Methods](#login-methods)
- [Quick Start](#quick-start)
- [Environment Variables](#environment-variables)
- [Deployment](#deployment)
- [Choosing the Library](#choosing-the-library)
- [Reading the Logs](#reading-the-logs)
- [Troubleshooting](#troubleshooting)
- [Security Notes](#security-notes)
- [Project Structure](#project-structure)

---

## Overview

**Login Test Bot** is built to find out *why a Telegram login code does not arrive*.
It contains no other features, so the MTProto library can be swapped with a single line in
`requirements.txt` and the results compared under identical conditions (same phone number, same API ID).

On `/start` the bot shows the active **library version** and **Python version**, and every
login step is written to the log together with the delivery type Telegram chose (app, SMS, call, ...).

## Login Methods

| # | Method | What you provide | Result |
|---|--------|------------------|--------|
| 1 | **Phone + OTP** | Phone number, OTP code, 2-step password (if enabled) | Login check |
| 2 | **API ID / API Hash** | Your own API ID and Hash, phone number, OTP code | Login check with your own key |
| 3 | **Generate Session** | Same as method 1 | A session string |
| 4 | **Paste Session** | An existing session string | Validity check, no code needed |

Messages that contain secrets (API Hash, session string, password, code) are **deleted from the chat immediately**.

## Quick Start

1. **Create a test bot** with [@BotFather](https://t.me/BotFather) and copy the token
   (revoke it once testing is finished).
2. **Get API credentials** at [my.telegram.org](https://my.telegram.org) under *API development tools*.
3. **Deploy** the project (see [Deployment](#deployment)) and set the environment variables.
4. Open the bot in Telegram, send `/start`, choose a method, and follow the prompts.

## Environment Variables

| Variable | Required | Description |
|----------|:--------:|-------------|
| `BOT_TOKEN` | Yes | Token of the test bot from @BotFather |
| `API_ID` | Yes | Numeric API ID from my.telegram.org |
| `API_HASH` | Yes | API Hash from my.telegram.org |
| `PORT` | No | Set automatically by Render / Railway for the health page |

## Deployment

### Railway

1. **New Project** -> **Deploy from GitHub repo** -> select this repository.
2. Open **Variables** and add `BOT_TOKEN`, `API_ID`, `API_HASH`.
3. The start command is `python main.py` (provided by the included `Procfile`).
4. Python 3.12 or newer works (Railway default is fine).

### Render

1. **New** -> **Web Service** -> select this repository. Settings are read from `render.yaml`.
2. Add `BOT_TOKEN`, `API_ID`, `API_HASH` under **Environment**.
3. After changing the library, use **Clear build cache & deploy**.

## Choosing the Library

The active library is the **single line** in `requirements.txt`. Test them one after another:

| Library | Line in `requirements.txt` | Notes |
|---------|----------------------------|-------|
| **Pyrofork** (default) | `pyrofork==2.3.69` | Newest release on PyPI, known to work |
| Pyrogram | `pyrogram==2.0.106` | Old, code did not arrive |
| Kurigram | `kurigram==2.2.26` | Code did not arrive |

To switch, replace the content of `requirements.txt` with the line you want (or copy one of the
`requirements_*.txt` files) and redeploy.

> **Important:** never install two of these libraries together.

## Reading the Logs

After requesting a code, the log contains a line like:

```text
login: code requested ... delivery=APP next=SMS
```

| Field | Meaning |
|-------|---------|
| `delivery` | How Telegram sent the code: `APP`, `SMS`, `CALL`, `FLASH_CALL`, `EMAIL_CODE`, ... |
| `next` | The next method Telegram offers if the code does not arrive |

When reporting a problem, include this line **for each library tested** and whether the code actually arrived.

## Troubleshooting

| Problem | Cause / Fix |
|---------|-------------|
| `No matching distribution found for pyrofork==2.4.0` | That version does not exist on PyPI. Use `pyrofork==2.3.69`. |
| Code does not arrive | Check `delivery=` in the log. `APP` means the code is in the Telegram chat named **Telegram**, not an SMS. |
| `ApiIdInvalid` | API ID / Hash is wrong. Re-check at my.telegram.org. |
| `ApiIdPublishedFlood` | This API ID is blocked. Create your own key. |
| `FloodWait` | Too many attempts. Wait the number of seconds shown. |
| Crash at start | Make sure `BOT_TOKEN`, `API_ID`, `API_HASH` are set. Send the first error line from the log. |

## Security Notes

- A **session string is the key to the account.** Never share it and delete the message that shows it.
- Use a **separate test bot** and revoke its token afterwards.
- Do not commit `BOT_TOKEN`, `API_HASH`, or session strings to the repository.

## Project Structure

```text
login_test_bot/
├── assets/
│   └── banner.svg                  # animated README banner
├── main.py                         # the bot (login flows + health page)
├── requirements.txt                # active library (one line)
├── requirements_pyrofork_*.txt     # ready-made library choices
├── requirements_pyrogram_*.txt
├── requirements_kurigram_*.txt
├── Procfile                        # Railway / Heroku-style start command
├── render.yaml                     # Render service definition
└── README.md
```

---

<div align="center">
<sub>Built for Telegram login diagnostics. Use only with your own accounts.</sub>
</div>
