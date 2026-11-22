"""LOGIN TEST BOT - only the login. Four ways in (menu on /start):
    1) Phone + OTP            : the bot's own API ID / Hash (environment) -> code -> (2-step password)
    2) API ID / API Hash      : your own key typed in the chat -> phone -> code -> (2-step password)
    3) Generate Session       : like 1), but the result is only a session string
    4) Paste Session          : paste a ready session string (made elsewhere): checked, no code needed

Purpose: find out why the login code does not arrive. It does nothing else, so the library can be changed in
requirements.txt (one line) and the result compared. Every step is written to the log.

Environment variables (Render: Environment tab / Railway: Variables):
    BOT_TOKEN   token of a bot made only for this test (revoke it afterwards)
    API_ID, API_HASH   from my.telegram.org
    PORT        set by Render / Railway by itself (a tiny health page answers on it)
Start command: python main.py
"""
import asyncio
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

STATE = {}      # uid -> {"step": "phone" | "code" | "password", "digits": "", "kp": message id}
LOGIN = {}      # uid -> {"client", "phone", "hash", "next", "where", "tries", "rs_ts"}
LOCKS = {}


def lock(uid):
    return LOCKS.setdefault(uid, asyncio.Lock())


def enum_name(v):
    return "" if v is None else str(getattr(v, "name", v)).upper()


def where_text(sent):
    kind = enum_name(getattr(sent, "type", None))
    text = {
        "APP": "📲 The code was sent <b>inside Telegram</b> (the chat named <b>Telegram</b>). It is not an SMS.",
        "SMS": "💬 The code was sent by <b>SMS</b>.",
        "CALL": "📞 Telegram will <b>call</b> you and read the code out.",
        "FLASH_CALL": "📞 Telegram makes a <b>missed call</b>: the code is the last digits of the number.",
        "MISSED_CALL": "📞 Telegram makes a <b>missed call</b>: the code is the last digits of the number.",
        "EMAIL_CODE": "📧 The code was sent to your <b>email</b>.",
        "FRAGMENT_SMS": "🧩 The code was sent through <b>Fragment</b>.",
    }.get(kind, f"🔑 Telegram sent a code (type: {kind or 'unknown'}).")
    if kind == "APP" and not enum_name(getattr(sent, "next_type", None)):
        text += "\n\n⚠️ Telegram offers NO other way (no SMS / call) for this request."
    return text


def err_text(e):
    if isinstance(e, tg_errors.FloodWait):
        return f"⏳ FloodWait: wait {e.value}s."
    if isinstance(e, asyncio.TimeoutError):
        return "⚠️ Telegram did not answer in time."
    if isinstance(e, OSError):
        return "⚠️ Network problem while reaching Telegram."
    return f"❌ {type(e).__name__} ({getattr(e, 'ID', '-')}): {str(e)[:200]}"


def log_err(step, uid, e):
    log.warning("login: %s failed uid=%s error=%s id=%s detail=%s", step, uid, type(e).__name__,
                getattr(e, "ID", "-"), str(e)[:300] or "-", exc_info=True)


def err_text_api(e, api):
    """err_text, with a clear line when a typed API ID / Hash is the reason."""
    if type(e).__name__ == "ApiIdInvalid":
        return "❌ Telegram rejected the API ID / API Hash (ApiIdInvalid). Check them at my.telegram.org."
    if type(e).__name__ == "ApiIdPublishedFlood":
        return "❌ This API ID is blocked (ApiIdPublishedFlood). Make your own at my.telegram.org."
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
    text = (f"{lg['where']}\n\n🔢 Enter the code with the buttons below.\n\n"
            f"Code: <code>{' '.join(digits) if digits else '-'}</code>")
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
    return Markup([[Btn("📱 Phone + OTP", "m|ph")],
                   [Btn("🔑 API ID / API Hash", "m|own")],
                   [Btn("🧬 Generate Session", "m|gen")],
                   [Btn("📋 Paste Session", "m|paste")]])


@bot.on_message(filters.private & filters.command(["start", "login"]))
async def start_cmd(c, m):
    uid = m.from_user.id
    await reset(uid)
    log.info("login-test: /start uid=%s library=%s python=%s", uid, LIB, platform.python_version())
    await m.reply_text(
        "🧪 <b>LOGIN TEST</b>\n"
        f"Library: <code>{LIB}</code>\nPython: <code>{platform.python_version()}</code>\n\n"
        "Choose how to log in:\n\n"
        "📱 <b>Phone + OTP</b> - your number, then the code (the bot's API key).\n"
        "🔑 <b>API ID / API Hash</b> - your own key from my.telegram.org, then your number and the code.\n"
        "🧬 <b>Generate Session</b> - log in once and get a session string.\n"
        "📋 <b>Paste Session</b> - you already have a session string: paste it, no code needed.\n\n/cancel to stop.",
        reply_markup=menu_markup())


@bot.on_message(filters.private & filters.command("cancel"))
async def cancel_cmd(c, m):
    await reset(m.from_user.id)
    await m.reply_text("✅ Cancelled. /start to begin again.")


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
        return await bot.send_message(uid, "❌ Send a valid number with country code, e.g. <code>+919876543210</code>.")
    st0 = STATE.get(uid) or {}
    api = (st0["api_id"], st0["api_hash"]) if st0.get("api_hash") else None
    mode = st0.get("mode", "login")
    async with lock(uid):
        wait = await bot.send_message(uid, "⏳ Asking Telegram for your code…")
        log.info("login: asking Telegram for a code uid=%s library=%s key=%s mode=%s", uid, LIB,
                 "own" if api else "bot", mode)
        try:
            lc, sent = await open_login(phone, api)
        except Exception as e:
            log_err("send_code", uid, e)
            return await bot.send_message(uid, err_text_api(e, api))
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
    head = "🧬 <b>SESSION GENERATED</b>" if lg.get("mode") == "generate" else "✅ <b>LOGIN WORKED</b>"
    await bot.send_message(
        uid, f"{head} ({LIB})\nAccount: {me.first_name}\n\n"
             f"Session string (the key to your account - keep it secret, delete this message):\n\n<code>{string}</code>\n\n"
             "You can test it with 📋 Paste Session in /start.")


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
            return await bot.send_message(uid, "❌ Too many tries. /start again.")
        return await bot.send_message(uid, "❌ That does not look like a session string. Paste the whole string, or /cancel.")
    async with lock(uid):
        status = await bot.send_message(uid, "⏳ Checking the session…")
        chk = Client(f"chk_{uid}_{int(time.time())}", api_id=API_ID, api_hash=API_HASH, session_string=txt, in_memory=True)
        try:
            await asyncio.wait_for(chk.start(), CONNECT_TIMEOUT)
            me = await asyncio.wait_for(chk.get_me(), STEP_TIMEOUT)
        except Exception as e:
            log_err("paste_session", uid, e)
            return await bot.send_message(uid, "❌ This string could not be used.\n" + err_text(e) +
                                          "\n\nPaste it again, or /cancel.")
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
            return await bot.send_message(uid, "❌ That is a bot session. Paste the session of your own account.")
        log.info("login: SESSION PASTE OK uid=%s account=%s library=%s", uid, getattr(me, "id", "?"), LIB)
        STATE.pop(uid, None)
        await bot.send_message(uid, f"✅ <b>SESSION WORKS</b> ({LIB})\nAccount: {me.first_name} "
                                    f"(@{getattr(me, 'username', None) or '-'})\n\nYou can delete the message that showed the string.")


async def do_api_id(m, uid):
    txt = (m.text or "").strip()
    try:
        await m.delete()
    except Exception:
        pass
    if not txt.isdigit() or not 4 <= len(txt) <= 12:
        return await bot.send_message(uid, "❌ The API ID is a number (about 7-8 digits). Send it again, or /cancel.")
    STATE[uid] = {"step": "api_hash", "api_id": int(txt), "ts": time.time()}
    await bot.send_message(uid, "🔑 Now send your <b>API Hash</b> (32 letters and digits).")


async def do_api_hash(m, uid):
    txt = (m.text or "").strip()
    try:
        await m.delete()                                   # keep the hash out of the chat
    except Exception:
        pass
    if not re.fullmatch(r"[0-9a-fA-F]{32}", txt):
        return await bot.send_message(uid, "❌ The API Hash is 32 letters and digits. Send it again, or /cancel.")
    st = STATE.get(uid) or {}
    STATE[uid] = {"step": "phone", "api_id": st.get("api_id"), "api_hash": txt.lower(), "mode": "login", "ts": time.time()}
    await bot.send_message(uid, "✅ Key saved for this login.\n\nNow send your phone number with the country code, "
                                "e.g. <code>+919876543210</code>.")


async def submit_code(uid, code):
    lg, st = LOGIN.get(uid), STATE.get(uid)
    if not lg or not st:
        return await bot.send_message(uid, "⌛ Expired. /start again.")
    async with lock(uid):
        try:
            res = await asyncio.wait_for(lg["client"].sign_in(lg["phone"], lg["hash"], code), STEP_TIMEOUT)
        except tg_errors.SessionPasswordNeeded:
            STATE[uid] = {"step": "password", "ts": time.time()}
            log.info("login: 2-step password needed uid=%s", uid)
            return await bot.send_message(uid, "🔐 2-Step Verification is on. Send your password (I delete the message "
                                               "at once).")
        except (tg_errors.PhoneCodeInvalid, getattr(tg_errors, "PhoneCodeEmpty", tg_errors.PhoneCodeInvalid)):
            lg["tries"] += 1
            st["digits"] = ""
            return await show_code_screen(uid, "❌ Wrong code. Try again.")
        except tg_errors.PhoneCodeExpired:
            try:
                sent = await asyncio.wait_for(lg["client"].send_code(lg["phone"]), STEP_TIMEOUT)
            except Exception as e:
                log_err("send_code (after expiry)", uid, e)
                await reset(uid)
                return await bot.send_message(uid, "⌛ Code expired. " + err_text(e))
            lg.update(hash=sent.phone_code_hash, next=enum_name(getattr(sent, "next_type", None)),
                      where=where_text(sent), tries=0, rs_ts=time.time())
            st["digits"] = ""
            return await show_code_screen(uid, "⌛ That code expired; a new one was requested.")
        except (asyncio.TimeoutError, OSError) as e:
            return await show_code_screen(uid, err_text(e) + " Press ✅ again.")
        except Exception as e:
            log_err("sign_in", uid, e)
            await reset(uid)
            return await bot.send_message(uid, err_text(e))
        if not hasattr(res, "id"):
            await reset(uid)
            return await bot.send_message(uid, "❌ This number has no Telegram account yet.")
        await finish(uid, lg)


async def submit_password(m, uid):
    pwd = m.text or ""
    try:
        await m.delete()
    except Exception:
        pass
    lg = LOGIN.get(uid)
    if not lg:
        return await bot.send_message(uid, "⌛ Expired. /start again.")
    async with lock(uid):
        try:
            await asyncio.wait_for(lg["client"].check_password(pwd), STEP_TIMEOUT)
        except tg_errors.PasswordHashInvalid:
            return await bot.send_message(uid, "❌ Wrong password. Send it again, or /cancel.")
        except Exception as e:
            log_err("check_password", uid, e)
            await reset(uid)
            return await bot.send_message(uid, err_text(e))
        await finish(uid, lg)


@bot.on_message(filters.private & filters.text & ~filters.command(["start", "login", "cancel"]))
async def text_router(c, m):
    uid = m.from_user.id
    st = STATE.get(uid)
    if not st:
        return await m.reply_text("Send /start to choose a login way.")
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
    if data == "cx":
        await reset(uid)
        return await q.message.reply_text("✅ Cancelled. /start to begin again.")
    if data.startswith("m|"):                               # the four ways in
        way = data[2:]
        await reset(uid)
        if way in ("ph", "gen"):
            STATE[uid] = {"step": "phone", "mode": "generate" if way == "gen" else "login", "ts": time.time()}
            return await q.message.reply_text(
                ("🧬 <b>GENERATE SESSION</b>\nI log in once and give you a session string.\n\n" if way == "gen"
                 else "📱 <b>PHONE + OTP</b>\n\n") +
                "Send your phone number with the country code, e.g. <code>+919876543210</code>.\n/cancel to stop.")
        if way == "own":
            STATE[uid] = {"step": "api_id", "ts": time.time()}
            return await q.message.reply_text(
                "🔑 <b>YOUR OWN API KEY</b>\nGet it free at my.telegram.org -> API development tools.\n\n"
                "Step 1 of 3 - send your <b>API ID</b> (numbers).\n/cancel to stop.")
        if way == "paste":
            STATE[uid] = {"step": "paste", "tries": 0, "ts": time.time()}
            return await q.message.reply_text(
                "📋 <b>PASTE SESSION</b>\nPaste the whole session string of your own account as one message. I delete your "
                "message at once and only check that it works.\n\n⚠️ It is the key to your account: never share it.\n"
                "/cancel to stop.")
        return
    if not st or not lg or st.get("step") != "code":
        return await q.message.reply_text("⌛ This screen is old. /start again.")
    if data.startswith("d|"):
        key, digits = data[2:], st.get("digits", "")
        if key == "ok":
            if len(digits) < 4:
                return await show_code_screen(uid, "⚠️ Enter the whole code first.")
            return await submit_code(uid, digits)
        st["digits"] = digits[:-1] if key == "del" else (digits + key if key.isdigit() and len(digits) < 8 else digits)
        return await show_code_screen(uid)
    if data == "rs":
        if time.time() - lg["rs_ts"] < 15:
            return await show_code_screen(uid, "⏳ Wait a few seconds before asking again.")
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
            await show_code_screen(uid, "🔁 A new code was requested. Only the newest code works.")


async def health_server():
    """A tiny page on $PORT so Render (web service) sees the app as alive. Railway worker: no PORT, no server."""
    if not PORT:
        return None

    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = f"login test bot ok - {LIB}".encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    log.info("health page on port %s", PORT)
    return server


async def main():
    await bot.start()
    health = await health_server()
    me = await bot.get_me()
    log.info("Login test bot started: @%s | library=%s | python=%s | %s", me.username, LIB, platform.python_version(),
             platform.platform())
    await idle()
    if health:
        health.close()
    await bot.stop()


if __name__ == "__main__":
    bot.run(main())
