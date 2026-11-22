"""ESBG LOGIN TEST BOT - only the login. Four ways in (menu on /start):
    1) Phone + OTP            : the bot's own API ID / Hash (environment) -> code -> (2-step password)
    2) API ID / API Hash      : your own key typed in the chat -> phone -> code -> (2-step password)
    3) Generate Session       : like 1), but the result is only a session string
    4) Paste Session          : paste a ready session string (made elsewhere): checked, no code needed

Purpose: find out why the login code does not arrive. It does nothing else, so the library can be changed in
requirements.txt (one line) and the result compared. Every step is written to the log.

Commands (registered in Telegram's "Menu" button at every start, failure is only logged):
    /start /login /generatesession /pastesession /help /cancel

Environment variables (Render: Environment tab / Railway: Variables):
    BOT_TOKEN   token of a bot made only for this test (revoke it afterwards)
    API_ID, API_HASH   from my.telegram.org
    PORT        set by Render / Railway by itself (a tiny health page answers on it)
Start command: python main.py
"""
import asyncio
import html
import inspect
import logging
import os
import platform
import re
import sys
import time
from importlib import metadata

from pyrogram import Client, filters, idle
from pyrogram import errors as tg_errors
from pyrogram.types import BotCommand
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("logintest")


def _need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"Missing environment variable: {name}")
    return v


BOT_TOKEN = _need("BOT_TOKEN")
API_ID = int(_need("API_ID"))
API_HASH = _need("API_HASH")
PORT = int(os.environ.get("PORT", "0") or 0)


def lib_info():
    for dist in ("pyrofork", "kurigram", "pyrogram"):
        try:
            return f"{dist} {metadata.version(dist)}"
        except metadata.PackageNotFoundError:
            continue
    return "unknown library"


LIB = lib_info()
CONNECT_TIMEOUT, STEP_TIMEOUT = 30, 45
bot = Client("login_test_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

STATE = {}      # uid -> {"step": "phone" | "api_id" | "api_hash" | "paste" | "code" | "password", ...}
LOGIN = {}      # uid -> {"client", "phone", "hash", "next", "where", "tries", "rs_ts"}
LOCKS = {}


def lock(uid):
    return LOCKS.setdefault(uid, asyncio.Lock())


def enum_name(v):
    return "" if v is None else str(getattr(v, "name", v)).upper()


# ---------------------------------------------------------------- screen style helpers
# Reference style: a decorative bar around an emoji and a CAPITAL title, short plain intro,
# instruction blocks ("▸ bold label" + plain sentences, one blank line between blocks) and a
# last line "💡 /cancel to stop." with /cancel in italics.

TIP_CANCEL = "💡 <i>/cancel</i> to stop."
TIP_START = "💡 <i>/start</i> to begin again."
FOOTER = "⚡ Powered by <b>ESBG</b>"


def block(label, body):
    """One instruction block: a small arrow, a bold label, plain sentence(s) on the next line."""
    return f"▸ <b>{label}</b>\n{body}"


def screen(emoji, title, body="", tip=TIP_CANCEL):
    """Compose a screen: bar heading, optional body blocks, optional last tip line."""
    text = f"━━━ {emoji} {title} ━━━"
    if body:
        text += f"\n\n{body}"
    if tip:
        text += f"\n\n{tip}"
    return text


def esc(v):
    """Escape dynamic values before they go into an HTML message."""
    return html.escape(str(v), quote=False)


def account_text(me):
    name = esc(getattr(me, "first_name", None) or "Unknown")
    uname = getattr(me, "username", None)
    return f"<b>{name}</b>" + (f" (@{esc(uname)})" if uname else "")


def menu_text():
    body = ("Choose how to log in:\n\n"
            + block("📱 Phone + OTP", "Quickest: send your number, then the code Telegram gives you.") + "\n\n"
            + block("🔑 API ID / API Hash",
                    "Uses your own key from my.telegram.org. Lower ban risk, and the best choice when the code never arrives.") + "\n\n"
            + block("🧬 Generate Session", "I log you in once and make a session string for you.") + "\n\n"
            + block("📋 Paste Session",
                    "Already have a session string? Paste it and I only check that it works. No code needed."))
    return (screen("🔐", "CONNECT YOUR TELEGRAM ACCOUNT", body, tip=None) + "\n\n" + TIP_CANCEL
            + f"\n\n🔬 <b>Diagnostics:</b> <code>{LIB}</code> · Python <code>{platform.python_version()}</code>\n{FOOTER}")


HELP_TEXT = (
    screen("❓", "HOW THIS BOT WORKS",
           "This bot signs a Telegram account in and reports exactly how the login code was delivered. "
           "Nothing is stored.\n\n"
           + block("📱 Phone + OTP", "Quickest: send your number, then the code Telegram gives you.") + "\n\n"
           + block("🔑 API ID / API Hash",
                   "Uses your own key from my.telegram.org. Lower ban risk, and the best choice when the code never arrives.") + "\n\n"
           + block("🧬 Generate Session", "I log you in once and make a session string for you.") + "\n\n"
           + block("📋 Paste Session",
                   "Already have a session string? Paste it and I only check that it works. No code needed.") + "\n\n"
           + block("Step order",
                   "API ID and API Hash (method 🔑 only) → phone number → code → 2-step password (only if enabled) → result.") + "\n\n"
           + block("The code keypad",
                   "Tap the digits, then ✅ Submit. ⌫ deletes one digit. You can also type the code in the chat - "
                   "typing it as one plain message can make Telegram cancel the code, so separate the digits with spaces.") + "\n\n"
           + block("2-step password",
                   "If your account has 2-step verification, I ask for your password last. I delete that message at once.") + "\n\n"
           + block("All commands",
                   "/start - Open the main menu\n"
                   "/login - Choose a login method\n"
                   "/generatesession - Generate a session string\n"
                   "/pastesession - Check a ready session string\n"
                   "/help - How the bot works and guidelines\n"
                   "/cancel - Cancel the current action") + "\n\n"
           + block("Security",
                   "A session string is the key to your account: never share it. I delete every message that contains a "
                   "secret (API Hash, code, password, session string) at once. Use your own accounts only.") + "\n\n"
           + block("If the code does not arrive",
                   "Tap 🔁 New code on the code screen and wait a minute. Read the delivery line at the top: APP means the "
                   "code is inside Telegram, in the chat named Telegram (blue tick) - it is not an SMS. Still nothing? "
                   "Send /login again and choose 🔑 API ID / API Hash."),
           tip=None)
    + "\n\n" + FOOTER
)


def where_text(sent):
    kind = enum_name(getattr(sent, "type", None))
    text = {
        "APP": "📲 The code was sent inside Telegram: open the chat named <b>Telegram</b> (blue tick) on a phone "
               "or computer where this account is already logged in. It is not an SMS.",
        "SMS": "💬 The code was sent by SMS to your phone.",
        "CALL": "📞 Telegram will call you and read the code aloud.",
        "FLASH_CALL": "📞 You will get a missed call: the code is the last digits of the calling number.",
        "MISSED_CALL": "📞 You will get a missed call: the code is the last digits of the calling number.",
        "EMAIL_CODE": "📧 The code was sent to your registered email address.",
        "FRAGMENT_SMS": "🧩 The code was sent through Fragment.",
    }.get(kind, f"🔑 Telegram has sent you a login code ({kind.title() if kind else 'unknown delivery'}).")
    if kind == "APP" and not enum_name(getattr(sent, "next_type", None)):
        text += "\n\n⚠️ Note: Telegram does not offer an alternative delivery method (SMS / call) for this request."
    return text


def err_text(e):
    if isinstance(e, tg_errors.FloodWait):
        return f"⏳ <b>FloodWait</b> - too many attempts. Telegram asks for a wait of {e.value} seconds."
    if isinstance(e, asyncio.TimeoutError):
        return "⚠️ <b>Timeout</b> - Telegram did not respond in time."
    if isinstance(e, OSError):
        return "⚠️ <b>Network error</b> - Telegram could not be reached."
    return f"❌ <b>{esc(type(e).__name__)}</b> ({esc(getattr(e, 'ID', '-') or '-')}): {esc(str(e)[:200])}"


def log_err(step, uid, e):
    log.warning("login: %s failed uid=%s error=%s id=%s detail=%s", step, uid, type(e).__name__,
                getattr(e, "ID", "-"), str(e)[:300] or "-", exc_info=True)


def err_text_api(e, api):
    """err_text, with a clear line when a typed API ID / Hash is the reason."""
    if type(e).__name__ == "ApiIdInvalid":
        return ("❌ <b>ApiIdInvalid</b> - this API ID / API Hash pair is not valid. "
                "Check both values at my.telegram.org, then send your phone number again.")
    if type(e).__name__ == "ApiIdPublishedFlood":
        return ("❌ <b>ApiIdPublishedFlood</b> - this API ID is blocked for new logins. "
                "Create your own key at my.telegram.org and use 🔑 API ID / API Hash.")
    return err_text(e)


def keypad(lg):
    nxt = lg.get("next") or ""
    names = {"SMS": "Send by SMS", "CALL": "Call me", "FLASH_CALL": "Missed call", "MISSED_CALL": "Missed call",
             "APP": "Send in the app", "EMAIL_CODE": "Send by email"}
    rows = [[Btn(str(n), f"d|{n}") for n in (1, 2, 3)], [Btn(str(n), f"d|{n}") for n in (4, 5, 6)],
            [Btn(str(n), f"d|{n}") for n in (7, 8, 9)],
            [Btn("⌫", "d|del"), Btn("0", "d|0"), Btn("✅ Submit", "d|ok")],
            [Btn("🔁 " + names.get(nxt, "New code"), "rs"), Btn("❌ Cancel", "cx")]]
    return Markup(rows)


async def show_code_screen(uid, note=""):
    st, lg = STATE.get(uid), LOGIN.get(uid)
    if not st or not lg:
        return
    digits = st.get("digits", "")
    text = (f"{lg['where']}\n\n"
            "🔢 Enter the code with the buttons below.\n"
            "Typing the code in the chat can make Telegram cancel it. If you type it, separate the digits with spaces.\n\n"
            f"Code: <code>{' '.join(digits) if digits else '-'}</code>\n\n"
            "<i>Not arriving? Tap 🔁 below, wait a minute, or start /login again and choose 🔑 API ID / API Hash.</i>")
    if note:
        text += f"\n\n{note}"
    if st.get("kp"):
        try:
            await bot.edit_message_text(uid, st["kp"], text, reply_markup=keypad(lg))
            return
        except tg_errors.MessageNotModified:
            return
        except Exception:
            pass
    sent = await bot.send_message(uid, text, reply_markup=keypad(lg))
    st["kp"] = sent.id


async def quiet_disconnect(c):
    try:
        await asyncio.wait_for(c.disconnect(), 10)
    except Exception:
        pass


async def reset(uid):
    STATE.pop(uid, None)
    lg = LOGIN.pop(uid, None)
    if lg:
        await quiet_disconnect(lg["client"])


def menu_markup():
    """Full-width menu: one button per row, the four methods, Help, then a red ❌ Close."""
    return Markup([[Btn("📱 Phone + OTP", "m|ph")],
                   [Btn("🔑 API ID / API Hash", "m|own")],
                   [Btn("🧬 Generate Session", "m|gen")],
                   [Btn("📋 Paste Session", "m|paste")],
                   [Btn("❓ Help", "hp")],
                   [Btn("❌ Close", "close")]])


# ---------------------------------------------------------------- shared flow starters
# Both the menu buttons and the /generatesession, /pastesession commands call these helpers,
# so the state and the intro screens are exactly the same for buttons and commands.


async def begin_phone_login(uid, mode):
    """📱 Phone + OTP button, 🧬 Generate Session button and /generatesession: ask for the phone number."""
    await reset(uid)
    if mode == "generate":
        STATE[uid] = {"step": "phone", "mode": "generate", "ts": time.time()}
        body = ("I will log in once and give you a session string to paste back.\n\n"
                + block("Your phone number", "Send it with the country code. Example: <code>+919876543210</code>"))
        return await bot.send_message(uid, screen("🧬", "GENERATE SESSION", body))
    STATE[uid] = {"step": "phone", "mode": "login", "ts": time.time()}
    body = block("Your phone number", "Send it with the country code. Example: <code>+919876543210</code>")
    return await bot.send_message(uid, screen("📱", "PHONE + OTP LOGIN", body))


async def begin_own_api(uid):
    """🔑 API ID / API Hash button and its flow: three steps, own key from my.telegram.org."""
    await reset(uid)
    STATE[uid] = {"step": "api_id", "ts": time.time()}
    body = ("Get it free at my.telegram.org → API development tools. "
            "It is only used for this login and is not stored.\n\n"
            + block("Step 1 of 3 - API ID", "Send your API ID (numbers)."))
    return await bot.send_message(uid, screen("🔑", "YOUR OWN API KEY", body))


async def begin_paste(uid):
    """📋 Paste Session button and /pastesession: check a ready session string, no code needed."""
    await reset(uid)
    STATE[uid] = {"step": "paste", "tries": 0, "ts": time.time()}
    body = ("Paste a ready session string and I will check that it works.\n\n"
            + block("Your session string", "Send the whole string as one message. I delete your message at once.") + "\n\n"
            + block("Keep it secret", "A session string is the key to your account. Never share it."))
    return await bot.send_message(uid, screen("📋", "PASTE SESSION", body))


# ---------------------------------------------------------------- commands

@bot.on_message(filters.private & filters.command(["start", "login"]))
async def start_cmd(c, m):
    uid = m.from_user.id
    await reset(uid)
    log.info("login-test: /start uid=%s library=%s python=%s", uid, LIB, platform.python_version())
    await m.reply_text(menu_text(), reply_markup=menu_markup())


@bot.on_message(filters.private & filters.command("help"))
async def help_cmd(c, m):
    """Works in any state: it never resets or touches a running login."""
    await m.reply_text(HELP_TEXT)


@bot.on_message(filters.private & filters.command("generatesession"))
async def generatesession_cmd(c, m):
    """Same state, same intro screen and same flow as pressing 🧬 Generate Session."""
    await begin_phone_login(m.from_user.id, "generate")


@bot.on_message(filters.private & filters.command("pastesession"))
async def pastesession_cmd(c, m):
    """Same state, same intro screen and same flow as pressing 📋 Paste Session."""
    await begin_paste(m.from_user.id)


@bot.on_message(filters.private & filters.command("cancel"))
async def cancel_cmd(c, m):
    await reset(m.from_user.id)
    await m.reply_text(screen("🚫", "CANCELLED", "The current action was stopped.", tip=TIP_START))


# ---------------------------------------------------------------- login flow

async def open_login(phone, api=None):
    """api = (api_id, api_hash) typed by the user, or None for the bot's own key."""
    last = None
    aid, ahash = api if api else (API_ID, API_HASH)
    for attempt in (1, 2):
        lc = Client(f"lt_{int(time.time() * 1000)}", api_id=aid, api_hash=ahash, in_memory=True)
        try:
            await asyncio.wait_for(lc.connect(), CONNECT_TIMEOUT)
            sent = await asyncio.wait_for(lc.send_code(phone), STEP_TIMEOUT)
            return lc, sent
        except (asyncio.TimeoutError, OSError) as e:
            last = e
            log.warning("login: connect/send_code did not get through (%s) - attempt %s", type(e).__name__, attempt)
            await quiet_disconnect(lc)
            await asyncio.sleep(1.5)
        except BaseException:
            await quiet_disconnect(lc)
            raise
    raise last


async def do_phone(m, uid):
    phone = re.sub(r"[\s-]", "", (m.text or "").strip())
    try:
        await m.delete()
    except Exception:
        pass
    if not re.fullmatch(r"\+[0-9]{6,15}", phone):
        return await bot.send_message(
            uid, screen("❌", "INVALID PHONE",
                        block("Your phone number", "Send it with the country code. Example: <code>+919876543210</code>")))
    st0 = STATE.get(uid) or {}
    api = (st0["api_id"], st0["api_hash"]) if st0.get("api_hash") else None
    mode = st0.get("mode", "login")
    async with lock(uid):
        wait = await bot.send_message(uid, "⏳ Requesting your verification code…")
        log.info("login: asking Telegram for a code uid=%s library=%s key=%s mode=%s", uid, LIB,
                 "own" if api else "bot", mode)
        try:
            lc, sent = await open_login(phone, api)
        except Exception as e:
            log_err("send_code", uid, e)
            return await bot.send_message(uid, screen("❌", "CODE REQUEST FAILED", block("Reason", err_text_api(e, api))))
        finally:
            try:
                await wait.delete()
            except Exception:
                pass
        log.info("login: code requested uid=%s library=%s key=%s delivery=%s next=%s timeout=%s", uid, LIB,
                 "own" if api else "bot", enum_name(getattr(sent, "type", None)),
                 enum_name(getattr(sent, "next_type", None)), getattr(sent, "timeout", None))
        LOGIN[uid] = {"client": lc, "phone": phone, "hash": sent.phone_code_hash, "mode": mode,
                      "api": api, "next": enum_name(getattr(sent, "next_type", None)), "where": where_text(sent),
                      "tries": 0, "rs_ts": 0.0}
        STATE[uid] = {"step": "code", "digits": "", "ts": time.time()}
        await show_code_screen(uid)


async def finish(uid, lg, note_prefix=""):
    c = lg["client"]
    me = await c.get_me()
    string = await c.export_session_string()
    log.info("login: SUCCESS uid=%s account=%s mode=%s", uid, getattr(me, "id", "?"), lg.get("mode"))
    await reset(uid)
    head = ("🧬", "SESSION GENERATED") if lg.get("mode") == "generate" else ("✅", "LOGIN WORKED")
    body = (block("Library", f"<code>{LIB}</code>") + "\n\n"
            + block("Account", account_text(me)) + "\n\n"
            + block("Session string", f"<code>{string}</code>") + "\n\n"
            + "⚠️ This string gives full access to your account. Keep it private and delete this message after saving "
              "it. You can verify it with 📋 Paste Session in /start.")
    await bot.send_message(uid, screen(head[0], head[1], body, tip=TIP_START))


SESSION_RE = re.compile(r"[A-Za-z0-9_\-=]{120,}")      # a session string (URL-safe base64)


async def do_paste(m, uid):
    """📋 a ready session string: it is started (no code), asked who it is, and closed again."""
    txt = (m.text or "").strip()
    try:
        await m.delete()                                   # keep the string out of the chat
    except Exception:
        pass
    st = STATE.get(uid) or {}
    if not SESSION_RE.fullmatch(txt):
        st["tries"] = st.get("tries", 0) + 1
        if st["tries"] >= 5:
            STATE.pop(uid, None)
            return await bot.send_message(
                uid, screen("❌", "TOO MANY ATTEMPTS",
                            "Five tries without a valid session string.", tip=TIP_START))
        return await bot.send_message(
            uid, screen("❌", "INVALID SESSION",
                        block("Reason", "That is not a session string. A session string is one long line of letters, "
                                        "digits, <code>-</code> and <code>_</code> (about 350 characters)."),
                        tip="Paste it again, or <i>/cancel</i>."))
    async with lock(uid):
        status = await bot.send_message(uid, "⏳ Verifying the session…")
        chk = Client(f"chk_{uid}_{int(time.time())}", api_id=API_ID, api_hash=API_HASH, session_string=txt, in_memory=True)
        try:
            await asyncio.wait_for(chk.start(), CONNECT_TIMEOUT)
            me = await asyncio.wait_for(chk.get_me(), STEP_TIMEOUT)
        except Exception as e:
            log_err("paste_session", uid, e)
            return await bot.send_message(
                uid, screen("❌", "INVALID SESSION",
                            "Telegram could not start the session, so the string is not valid.\n\n"
                            + block("Reason", err_text(e)),
                            tip="Paste it again, or <i>/cancel</i>."))
        finally:
            try:
                await status.delete()
            except Exception:
                pass
            try:
                await chk.stop()
            except Exception:
                pass
        if getattr(me, "is_bot", False):
            return await bot.send_message(
                uid, screen("❌", "INVALID SESSION",
                            block("Reason", "This is a <b>bot</b> session. Paste the session string of a user account."),
                            tip="Paste it again, or <i>/cancel</i>."))
        log.info("login: SESSION PASTE OK uid=%s account=%s library=%s", uid, getattr(me, "id", "?"), LIB)
        STATE.pop(uid, None)
        await bot.send_message(
            uid, screen("✅", "SESSION VERIFIED",
                        "The session string works.\n\n"
                        + block("Library", f"<code>{LIB}</code>") + "\n\n"
                        + block("Account", account_text(me))
                        + "\n\nYour message with the string was deleted from this chat.",
                        tip=TIP_START))


async def do_api_id(m, uid):
    txt = (m.text or "").strip()
    try:
        await m.delete()
    except Exception:
        pass
    if not txt.isdigit() or not 4 <= len(txt) <= 12:
        return await bot.send_message(
            uid, screen("❌", "INVALID API ID",
                        "It is a number of about 7-8 digits.\n\n"
                        + block("Step 1 of 3 - API ID", "Send your API ID (numbers).")))
    STATE[uid] = {"step": "api_hash", "api_id": int(txt), "ts": time.time()}
    await bot.send_message(
        uid, screen("🔑", "YOUR OWN API KEY",
                    block("Step 2 of 3 - API Hash", "Send your API Hash (32 characters). I delete your message at once.")))


async def do_api_hash(m, uid):
    txt = (m.text or "").strip()
    try:
        await m.delete()                                   # keep the hash out of the chat
    except Exception:
        pass
    if not re.fullmatch(r"[0-9a-fA-F]{32}", txt):
        return await bot.send_message(
            uid, screen("❌", "INVALID API HASH",
                        "It contains 32 letters and digits.\n\n"
                        + block("Step 2 of 3 - API Hash", "Send your API Hash (32 characters).")))
    st = STATE.get(uid) or {}
    STATE[uid] = {"step": "phone", "api_id": st.get("api_id"), "api_hash": txt.lower(), "mode": "login", "ts": time.time()}
    await bot.send_message(
        uid, screen("🔑", "YOUR OWN API KEY",
                    "API credentials saved for this login.\n\n"
                    + block("Step 3 of 3 - Phone number",
                            "Send it with the country code. Example: <code>+919876543210</code>")))


async def submit_code(uid, code):
    lg, st = LOGIN.get(uid), STATE.get(uid)
    if not lg or not st:
        return await bot.send_message(
            uid, screen("⌛", "LOGIN EXPIRED", "This login ran out of time.", tip=TIP_START))
    async with lock(uid):
        try:
            res = await asyncio.wait_for(lg["client"].sign_in(lg["phone"], lg["hash"], code), STEP_TIMEOUT)
        except tg_errors.SessionPasswordNeeded:
            STATE[uid] = {"step": "password", "ts": time.time()}
            log.info("login: 2-step password needed uid=%s", uid)
            return await bot.send_message(
                uid, screen("🔐", "2-STEP PASSWORD",
                            "Your account has 2-step verification.\n\n"
                            + block("Your password", "Send it as one message. I delete it at once.")))
        except (tg_errors.PhoneCodeInvalid, getattr(tg_errors, "PhoneCodeEmpty", tg_errors.PhoneCodeInvalid)):
            lg["tries"] += 1
            st["digits"] = ""
            return await show_code_screen(uid, "❌ Incorrect code. Please try again.")
        except tg_errors.PhoneCodeExpired:
            try:
                sent = await asyncio.wait_for(lg["client"].send_code(lg["phone"]), STEP_TIMEOUT)
            except Exception as e:
                log_err("send_code (after expiry)", uid, e)
                await reset(uid)
                return await bot.send_message(
                    uid, screen("⌛", "CODE EXPIRED", block("Reason", err_text(e)), tip=TIP_START))
            lg.update(hash=sent.phone_code_hash, next=enum_name(getattr(sent, "next_type", None)),
                      where=where_text(sent), tries=0, rs_ts=time.time())
            st["digits"] = ""
            return await show_code_screen(uid, "⌛ Code expired. A new one has been requested.")
        except (asyncio.TimeoutError, OSError) as e:
            return await show_code_screen(uid, err_text(e) + "\nPress ✅ Submit to retry.")
        except Exception as e:
            log_err("sign_in", uid, e)
            await reset(uid)
            return await bot.send_message(
                uid, screen("❌", "LOGIN FAILED", block("Reason", err_text(e)), tip=TIP_START))
        if not hasattr(res, "id"):
            await reset(uid)
            return await bot.send_message(
                uid, screen("❌", "NO ACCOUNT FOUND",
                            "This number is not registered on Telegram.", tip=TIP_START))
        await finish(uid, lg)


async def submit_password(m, uid):
    pwd = m.text or ""
    try:
        await m.delete()
    except Exception:
        pass
    lg = LOGIN.get(uid)
    if not lg:
        return await bot.send_message(
            uid, screen("⌛", "LOGIN EXPIRED", "This login ran out of time.", tip=TIP_START))
    async with lock(uid):
        try:
            await asyncio.wait_for(lg["client"].check_password(pwd), STEP_TIMEOUT)
        except tg_errors.PasswordHashInvalid:
            return await bot.send_message(
                uid, screen("❌", "WRONG PASSWORD", "Send it again, or <i>/cancel</i>.", tip=None))
        except Exception as e:
            log_err("check_password", uid, e)
            await reset(uid)
            return await bot.send_message(
                uid, screen("❌", "LOGIN FAILED", block("Reason", err_text(e)), tip=TIP_START))
        await finish(uid, lg)


@bot.on_message(filters.private & filters.text
                & ~filters.command(["start", "login", "generatesession", "pastesession", "help", "cancel"]))
async def text_router(c, m):
    uid = m.from_user.id
    st = STATE.get(uid)
    if not st:
        return await m.reply_text("👋 Send /start to choose a login method.")
    if st["step"] == "phone":
        return await do_phone(m, uid)
    if st["step"] == "api_id":
        return await do_api_id(m, uid)
    if st["step"] == "api_hash":
        return await do_api_hash(m, uid)
    if st["step"] == "paste":
        return await do_paste(m, uid)
    if st["step"] == "password":
        return await submit_password(m, uid)
    if st["step"] == "code":
        digits = re.sub(r"\D", "", m.text or "")
        try:
            await m.delete()
        except Exception:
            pass
        if digits:
            st["digits"] = digits[:8]
            return await submit_code(uid, st["digits"])


@bot.on_callback_query()
async def callbacks(c, q):
    uid, data = q.from_user.id, q.data or ""
    st, lg = STATE.get(uid), LOGIN.get(uid)
    try:
        await q.answer()
    except Exception:
        pass
    if data == "close":                                    # ❌ Close deletes the menu message
        try:
            await q.message.delete()
        except Exception:
            pass
        return
    if data == "hp":                                       # ❓ Help never touches a running login
        return await bot.send_message(uid, HELP_TEXT)
    if data == "cx":
        await reset(uid)
        return await q.message.reply_text(screen("🚫", "CANCELLED", "The current action was stopped.", tip=TIP_START))
    if data.startswith("m|"):                               # the four ways in
        way = data[2:]
        if way == "ph":
            return await begin_phone_login(uid, "login")
        if way == "gen":
            return await begin_phone_login(uid, "generate")
        if way == "own":
            return await begin_own_api(uid)
        if way == "paste":
            return await begin_paste(uid)
        return
    if not st or not lg or st.get("step") != "code":
        return await q.message.reply_text(
            screen("⌛", "SCREEN EXPIRED", "This screen is outdated.", tip=TIP_START))
    if data.startswith("d|"):
        key, digits = data[2:], st.get("digits", "")
        if key == "ok":
            if len(digits) < 4:
                return await show_code_screen(uid, "⚠️ Please enter the complete code first.")
            return await submit_code(uid, digits)
        st["digits"] = digits[:-1] if key == "del" else (digits + key if key.isdigit() and len(digits) < 8 else digits)
        return await show_code_screen(uid)
    if data == "rs":
        if time.time() - lg["rs_ts"] < 15:
            return await show_code_screen(uid, "⏳ Please wait a few seconds before requesting another code.")
        async with lock(uid):
            lc = lg["client"]
            try:
                if lg.get("next") and hasattr(lc, "resend_code"):
                    sent = await asyncio.wait_for(lc.resend_code(lg["phone"], lg["hash"]), STEP_TIMEOUT)
                else:
                    sent = await asyncio.wait_for(lc.send_code(lg["phone"]), STEP_TIMEOUT)
            except Exception as e:
                log_err("resend_code", uid, e)
                return await show_code_screen(uid, err_text(e))
            log.info("login: resend uid=%s library=%s delivery=%s next=%s", uid, LIB,
                     enum_name(getattr(sent, "type", None)), enum_name(getattr(sent, "next_type", None)))
            lg.update(hash=sent.phone_code_hash, next=enum_name(getattr(sent, "next_type", None)),
                      where=where_text(sent), tries=0, rs_ts=time.time())
            st["digits"] = ""
            await show_code_screen(uid, "🔁 New code requested. Only the latest code is valid.")


async def health_server():
    """A tiny page on $PORT so Render (web service) sees the app as alive. Railway worker: no PORT, no server."""
    if not PORT:
        return None

    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = f"ESBG login test bot ok - {LIB}".encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    log.info("health page on port %s", PORT)
    return server


MENU_COMMANDS = [
    ("start", "Open the main menu"),
    ("login", "Choose a login method"),
    ("generatesession", "Generate a session string"),
    ("pastesession", "Check a ready session string"),
    ("help", "How the bot works and guidelines"),
    ("cancel", "Cancel the current action"),
]


async def register_commands():
    """Fill the bot's own command menu (the 'Menu' button in Telegram). Runs at every start; a failure is only logged."""
    try:
        await bot.set_bot_commands([BotCommand(name, desc) for name, desc in MENU_COMMANDS])
        log.info("bot command menu registered: %s", ", ".join("/" + n for n, _ in MENU_COMMANDS))
    except Exception as e:
        log.warning("could not register the command menu: %s: %s", type(e).__name__, str(e)[:200])


async def main():
    await bot.start()
    await register_commands()
    health = await health_server()
    me = await bot.get_me()
    log.info("ESBG login test bot started: @%s | library=%s | python=%s | %s", me.username, LIB,
             platform.python_version(), platform.platform())
    await idle()
    if health:
        health.close()
    await bot.stop()


if __name__ == "__main__":
    bot.run(main())
