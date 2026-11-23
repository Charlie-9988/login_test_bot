"""LOGIN BOT - sign in to a Telegram account, four ways in (menu on /start):
    1) Phone + OTP            : the bot's own API ID / Hash (environment) -> code -> (2-step password)
    2) API ID / API Hash      : your own key typed in the chat -> phone -> code -> (2-step password)
    3) Generate Session       : like 1), but the result is only a session string
    4) Paste Session          : paste a ready session string (made elsewhere): checked, no code needed

Every screen shows a rotating photo (assets/photos) above the text. If photos are off,
missing or rejected, the same screen is sent as plain text - the login flow never breaks.
Every step is written to the log, including how Telegram delivers the code (delivery=).

Environment variables (Render: Environment tab / Railway: Variables):
    BOT_TOKEN   token of a bot made only for this (revoke it afterwards)
    API_ID, API_HASH   from my.telegram.org
    PORT        set by Render / Railway by itself (a tiny health page answers on it)
    PHOTOS      on (default) / off - turn the rotating photos off
Start command: python main.py
"""
import asyncio
import io
import logging
import os
import platform
import random
import re
import time
from importlib import metadata

from pyrogram import Client, filters, idle
from pyrogram import errors as tg_errors
from pyrogram.types import BotCommand, InputMediaPhoto
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("loginbot")


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
bot = Client("login_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

# ======================================================================
# PHOTO ROTATION - constants
# ======================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTO_DIR = os.path.join(BASE_DIR, "assets", "photos")
PHOTOS_ENABLED = os.environ.get("PHOTOS", "on").strip().lower() not in ("off", "0", "false", "no")
ROTATE_ON_KEYPAD = False          # keypad digits only edit the caption and keep the current photo
CAPTION_LIMIT = 1024              # Telegram limit for a photo caption

try:
    from PIL import Image as PILImage
    HAS_PIL = True
except Exception:
    PILImage = None
    HAS_PIL = False


class Photo:
    __slots__ = ("path", "kind", "data", "category", "file_id", "failed")

    def __init__(self, path, kind, data, category):
        self.path = path
        self.kind = kind
        self.data = data
        self.category = category
        self.file_id = None          # cached after the first successful upload
        self.failed = False


def sniff_format(head):
    """Format from the first bytes, never from the file extension."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"                                   # JPG / JPEG / JFIF / EXIF
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:2] == b"BM":
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    return None


def _plausible(data, kind):
    """Cheap end-marker sanity check, so a truncated file is caught even without Pillow."""
    tail = data.rstrip(b"\x00")
    if kind == "jpeg" and not tail.endswith(b"\xff\xd9"):
        return False                                    # JPEG files end with FFD9
    if kind == "png" and not tail.endswith(b"\xaeB`\x82"):
        return False                                    # IEND chunk CRC
    if kind == "gif" and not tail.endswith(b"\x3b"):
        return False
    return True


def pil_to_jpeg(data):
    """Convert anything Pillow can open to JPEG; downsizes very large pictures."""
    im = PILImage.open(io.BytesIO(data))
    im.load()
    if im.mode != "RGB":
        im = im.convert("RGB")
    if im.width + im.height > 10000 or len(data) > 10 * 1024 * 1024:
        im.thumbnail((2048, 2048))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue()


PHOTOS = []                     # all usable pictures, loaded once at start
PHOTO_BLACKLIST = set()         # indices removed from rotation after a send failure
BAGS = {}                       # uid -> {"bag": [indices], "last": index}


def load_photos():
    """Read every file in PHOTO_DIR (recursively) at start. Never crashes."""
    global PHOTOS
    PHOTOS = []
    if not PHOTOS_ENABLED:
        log.info("photos: PHOTOS=off - screens are sent as plain text")
        return
    if not os.path.isdir(PHOTO_DIR):
        log.info("photos: folder not found (%s) - screens are sent as plain text", PHOTO_DIR)
        return
    skipped = []                # files that only Pillow could open, but Pillow is missing
    for root, dirs, files in os.walk(PHOTO_DIR):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in (".txt", ".md"):
                continue
            path = os.path.join(root, name)
            try:
                size = os.path.getsize(path)
                if size == 0 or size > 64 * 1024 * 1024:
                    continue
                with open(path, "rb") as f:
                    data = f.read()
                kind = sniff_format(data[:16])
                category = os.path.basename(root).lower()
                if kind in ("jpeg", "png", "webp"):
                    if not _plausible(data, kind):
                        log.warning("photos: corrupt file skipped: %s", name)
                        continue
                    if HAS_PIL:                     # integrity check; corrupt files are skipped
                        try:
                            with PILImage.open(io.BytesIO(data)) as im:
                                im.verify()
                        except Exception:
                            log.warning("photos: corrupt file skipped: %s", name)
                            continue
                    PHOTOS.append(Photo(path, kind, data, category))
                elif HAS_PIL:
                    try:
                        PHOTOS.append(Photo(path, "jpeg", pil_to_jpeg(data), category))
                    except Exception:
                        log.warning("photos: unreadable file skipped: %s", name)
                else:
                    skipped.append(name)
            except Exception as e:
                log.warning("photos: could not read %s (%s)", name, type(e).__name__)
    if skipped:
        log.warning("photos: %d file(s) skipped because Pillow is not installed (%s). "
                    "Add 'pillow' to requirements.txt to enable GIF / BMP / TIFF.",
                    len(skipped), ", ".join(sorted(skipped)[:8]))
    n_login = sum(1 for p in PHOTOS if p.category == "login")
    log.info("photos: %d ready (%d login, %d nature)", len(PHOTOS), n_login, len(PHOTOS) - n_login)


def _usable_photos():
    return [i for i in range(len(PHOTOS)) if i not in PHOTO_BLACKLIST]


def _refill(indices, last):
    """One round of the shuffle bag: every picture once, login / nature mixed."""
    login = [i for i in indices if PHOTOS[i].category == "login"]
    other = [i for i in indices if PHOTOS[i].category != "login"]
    random.shuffle(login)
    random.shuffle(other)
    out, a, b = [], 0, 0
    while a < len(login) or b < len(other):
        if a < len(login):
            out.append(login[a])
            a += 1
        if b < len(other):
            out.append(other[b])
            b += 1
    if last is not None and len(out) > 1 and out[0] == last:
        k = random.randrange(1, len(out))
        out[0], out[k] = out[k], out[0]
    return out


def next_photo(uid):
    """The next picture for this user: every picture once before any repeats, never twice in a row."""
    if not PHOTOS_ENABLED or not PHOTOS:
        return None
    if len(BAGS) > 500:                       # tiny cleanup of stale bags
        for k in [k for k in BAGS if k != uid and k not in STATE and k not in LOGIN][:200]:
            BAGS.pop(k, None)
    st = BAGS.setdefault(uid, {"bag": [], "last": None})
    for _ in range(2):
        while st["bag"]:
            i = st["bag"].pop(0)
            if i in PHOTO_BLACKLIST:
                continue
            if i == st["last"] and len(_usable_photos()) > 1:
                continue
            st["last"] = i
            return PHOTOS[i]
        usable = _usable_photos()
        if not usable:
            return None
        st["bag"] = _refill(usable, st["last"])
    return None


def blacklist_photo(photo, reason):
    """A picture Telegram rejected never comes back this run. Logged once."""
    if photo.failed:
        return
    photo.failed = True
    try:
        PHOTO_BLACKLIST.add(PHOTOS.index(photo))
    except ValueError:
        pass
    log.warning("photos: removed %s from rotation (%s)", os.path.basename(photo.path), reason)


# ======================================================================
# SENDING HELPERS - every screen goes through these
# ======================================================================
def clip_caption(text):
    """Photo captions are limited to 1024 characters (text messages to 4096)."""
    if len(text) <= CAPTION_LIMIT:
        return text
    cut = text[:CAPTION_LIMIT - 1]
    nl = cut.rfind("\n")
    if nl > 400:
        cut = cut[:nl]
    return cut + "…"


def _largest_file_id(msg):
    ph = getattr(msg, "photo", None)
    if ph:
        try:
            return max(ph, key=lambda p: (getattr(p, "width", 0) or 0) * (getattr(p, "height", 0) or 0)).file_id
        except Exception:
            return None
    return None


async def _send_photo_msg(uid, photo, caption, markup=None):
    source = photo.file_id or photo.data       # reuse the cached file_id when we have one
    sent = await bot.send_photo(uid, source, caption=clip_caption(caption), reply_markup=markup)
    fid = _largest_file_id(sent)
    if fid:
        photo.file_id = fid
    return sent


async def send_screen(uid, text, markup=None):
    """A screen with a fresh rotated photo on top; falls back to plain text, same text and buttons."""
    for _ in range(3):
        photo = next_photo(uid)
        if photo is None:
            return await bot.send_message(uid, text, reply_markup=markup)
        try:
            return await _send_photo_msg(uid, photo, text, markup)
        except Exception as e:
            blacklist_photo(photo, type(e).__name__)
    log.warning("photos: all pictures failed to send - falling back to text")
    return await bot.send_message(uid, text, reply_markup=markup)


async def swap_screen(msg, uid, text, markup=None):
    """Inline-button reply: replace picture AND caption in place (edit_message_media).
    If the message is old, is plain text, or the edit fails: delete it and send a fresh screen."""
    photo = next_photo(uid)
    if msg is not None and photo is not None and getattr(msg, "photo", None):
        # the message really carries a picture - replace picture AND caption in place
        try:
            media = InputMediaPhoto(photo.file_id or photo.data, caption=clip_caption(text))
            m2 = await bot.edit_message_media(msg.chat.id, msg.id, media, reply_markup=markup)
            fid = _largest_file_id(m2)
            if fid:
                photo.file_id = fid
            return m2
        except tg_errors.MessageNotModified:
            return msg
        except Exception:
            pass
    if msg is not None:
        try:
            await msg.delete()
        except Exception:
            pass
    return await send_screen(uid, text, markup)


async def edit_code_screen(uid, text, markup):
    """Caption-only update for the code screen (keypad digits keep the current picture)."""
    st = STATE.get(uid)
    if st and st.get("kp"):
        try:
            if st.get("kp_photo"):
                await bot.edit_message_caption(uid, st["kp"], caption=text, reply_markup=markup)
            else:
                await bot.edit_message_text(uid, st["kp"], text, reply_markup=markup)
            return
        except tg_errors.MessageNotModified:
            return
        except Exception:
            pass
        try:
            await bot.delete_messages(uid, st["kp"])
        except Exception:
            pass
        st["kp"] = None
    sent = await send_screen(uid, text, markup)
    if sent is not None:
        st = STATE.get(uid)
        if st is not None:
            st["kp"] = sent.id
            st["kp_photo"] = bool(_largest_file_id(sent))


# ======================================================================
# SCREEN TEXTS - screenshot style: bar heading, ▸ blocks, 💡 /cancel line
# ======================================================================
def _menu_blocks():
    return (
        "▸ <b>📱 Phone + OTP</b>\n"
        "The quickest way. Send your number, then the code Telegram gives you.\n\n"
        "▸ <b>🔑 API ID / API Hash</b>\n"
        "Uses your own key from my.telegram.org. Lower ban risk, and the best choice when the code never arrives.\n\n"
        "▸ <b>🧬 Generate Session</b>\n"
        "Log in once and the bot makes a session string for you.\n\n"
        "▸ <b>📋 Paste Session</b>\n"
        "Already have a session string? Paste it and I only check that it works. No code needed.\n\n"
        "💡 Tip: if the code never arrives, choose 🔑 API ID / API Hash."
    )


def txt_welcome():
    return ("🔐 <b>━━━ 🔐 LOGIN BOT ━━━</b>\n\n"
            "Sign in to a Telegram account and see exactly how the login code is delivered.\n\n"
            "Choose how to log in:\n\n"
            f"{_menu_blocks()}\n\n"
            "⚙️ <i>Powered by ESBG</i>")


def txt_login_menu():
    return ("🔐 <b>━━━ 🔐 CONNECT YOUR TELEGRAM ACCOUNT ━━━</b>\n\n"
            "Choose how to log in:\n\n"
            f"{_menu_blocks()}")


def txt_phone_intro(mode="login"):
    if mode == "generate":
        return ("🧬 <b>━━━ 🧬 GENERATE SESSION ━━━</b>\n\n"
                "I will log in once and give you a session string to paste back.\n\n"
                "▸ <b>Your phone number</b>\n"
                "Send it with the country code. Example: <code>+919876543210</code>\n\n"
                "💡 <i>/cancel</i> to stop.")
    return ("📱 <b>━━━ 📱 PHONE + OTP LOGIN ━━━</b>\n\n"
            "Sign in with your phone number and the one-time code Telegram gives you.\n\n"
            "▸ <b>Your phone number</b>\n"
            "Send it with the country code. Example: <code>+919876543210</code>\n\n"
            "💡 <i>/cancel</i> to stop.")


def txt_own_intro():
    return ("🔑 <b>━━━ 🔑 YOUR OWN API KEY ━━━</b>\n\n"
            "Get it free at my.telegram.org → <i>API development tools</i>.\n"
            "It is only used for this login and is not stored.\n\n"
            "▸ <b>Step 1 of 3 - API ID</b>\n"
            "Send your API ID (numbers).\n\n"
            "💡 <i>/cancel</i> to stop.")


def txt_own_step2():
    return ("🔑 <b>━━━ 🔑 YOUR OWN API KEY ━━━</b>\n\n"
            "▸ <b>Step 2 of 3 - API Hash</b>\n"
            "Send your API Hash (32 letters and digits). I delete your message at once.\n\n"
            "💡 <i>/cancel</i> to stop.")


def txt_own_step3():
    return ("🔑 <b>━━━ 🔑 YOUR OWN API KEY ━━━</b>\n\n"
            "✅ API credentials saved for this login.\n\n"
            "▸ <b>Step 3 of 3 - Phone number</b>\n"
            "Send your phone number with the country code. Example: <code>+919876543210</code>\n\n"
            "💡 <i>/cancel</i> to stop.")


def txt_paste_intro():
    return ("📋 <b>━━━ 📋 PASTE SESSION ━━━</b>\n\n"
            "Paste a ready session string and I will check that it works.\n\n"
            "▸ <b>Your session string</b>\n"
            "Send the whole string as one message. I delete your message at once.\n\n"
            "▸ <b>Keep it secret</b>\n"
            "A session string is the key to your account. Never share it.\n\n"
            "💡 <i>/cancel</i> to stop.")


def txt_code_screen(lg, digits, note=""):
    t = ("🔢 <b>━━━ 🔢 ENTER THE CODE ━━━</b>\n\n"
         f"{lg['where']}\n\n"
         "🔢 Enter the code with the buttons below.\n"
         "Typing the code in the chat can make Telegram cancel it. If you type it, put a space between the digits.\n\n"
         "<b>Code:</b> <code>" + (" ".join(digits) if digits else "-") + "</code>")
    if note:
        t += "\n\n" + note
    t += ("\n\n<i>Not arriving? Tap 🔁 below, wait a minute, or start /login again "
          "and choose 🔑 API ID / API Hash.</i>\n\n"
          "💡 <i>/cancel</i> to stop.")
    return t


def txt_password():
    return ("🔒 <b>━━━ 🔒 TWO-STEP VERIFICATION ━━━</b>\n\n"
            "Your account is protected with a password.\n\n"
            "▸ <b>Your password</b>\n"
            "Send it as one message. I delete your message at once and never store it.\n\n"
            "💡 <i>/cancel</i> to stop.")


def txt_invalid_phone():
    return ("❌ <b>━━━ ❌ INVALID PHONE ━━━</b>\n\n"
            "That does not look like a phone number.\n\n"
            "▸ <b>Try again</b>\n"
            "Send it with the country code. Example: <code>+919876543210</code>\n\n"
            "💡 <i>/cancel</i> to stop.")


def txt_invalid_api_id():
    return ("❌ <b>━━━ ❌ INVALID API ID ━━━</b>\n\n"
            "▸ <b>Try again</b>\n"
            "Your API ID is a number of about 7-8 digits. Send it again.\n\n"
            "💡 <i>/cancel</i> to stop.")


def txt_invalid_api_hash():
    return ("❌ <b>━━━ ❌ INVALID API HASH ━━━</b>\n\n"
            "▸ <b>Try again</b>\n"
            "The API Hash has 32 letters and digits. Send it again.\n\n"
            "💡 <i>/cancel</i> to stop.")


def txt_paste_invalid(reason):
    return ("📋 <b>━━━ ❌ INVALID SESSION ━━━</b>\n\n"
            f"{reason}\n\n"
            "Paste it again, or <i>/cancel</i>.")


def txt_paste_too_many():
    return ("📋 <b>━━━ ❌ TOO MANY ATTEMPTS ━━━</b>\n\n"
            "Five tries are used up.\n\nSend <i>/start</i> to try again.")


def txt_paste_ok(me):
    return ("📋 <b>━━━ ✅ SESSION VERIFIED ━━━</b>\n\n"
            f"<b>Library:</b> {LIB}\n"
            f"<b>Account:</b> {getattr(me, 'first_name', '-')} "
            f"(@{getattr(me, 'username', None) or '-'})\n\n"
            "The session works. No code was needed.\n\n"
            "▸ <b>Keep it secret</b>\n"
            "A session string is the key to your account. Never share it.")


def txt_result(me, string, mode, own_key):
    head = ("🧬 <b>━━━ 🧬 SESSION GENERATED ━━━</b>" if mode == "generate"
            else "✅ <b>━━━ ✅ LOGIN WORKED ━━━</b>")
    key_line = ("▸ <b>API key</b>\nThis login used your own API ID / API Hash from my.telegram.org."
                if own_key else
                "▸ <b>API key</b>\nThis login used the bot's own API ID / API Hash.")
    return (f"{head}\n\n"
            f"<b>Account:</b> {getattr(me, 'first_name', '-')}\n"
            f"<b>Library:</b> {LIB}\n\n"
            f"{key_line}\n\n"
            "<b>Your session string</b>\n"
            f"<code>{string}</code>\n\n"
            "▸ <b>Keep it secret</b>\n"
            "This string gives full access to your account. Save it somewhere safe and delete this message.\n\n"
            "You can check it any time with 📋 Paste Session.")


def txt_cancelled():
    return ("✋ <b>━━━ ✋ CANCELLED ━━━</b>\n\n"
            "The current action was stopped. Nothing was saved.\n\n"
            "Send <i>/start</i> to choose a login method.")


def txt_expired():
    return ("⌛ <b>━━━ ⌛ LOGIN EXPIRED ━━━</b>\n\n"
            "This login took too long or was reset.\n\n"
            "Send <i>/start</i> to begin again.")


def txt_help_caption():
    return ("❓ <b>━━━ ❓ HELP ━━━</b>\n\n"
            "How the bot works, step by step. The full guide is in the next message.")


def txt_help():
    return (
        "❓ <b>━━━ ❓ HELP ━━━</b>\n\n"
        "This bot signs you in to a Telegram account and shows exactly how the login code is delivered. Four ways in:\n\n"
        "▸ <b>📱 Phone + OTP</b>\n"
        "The quickest way. Send your number, then the code Telegram gives you.\n\n"
        "▸ <b>🔑 API ID / API Hash</b>\n"
        "Uses your own key from my.telegram.org. Lower ban risk, and the best choice when the code never arrives.\n\n"
        "▸ <b>🧬 Generate Session</b>\n"
        "Log in once and the bot makes a session string for you.\n\n"
        "▸ <b>📋 Paste Session</b>\n"
        "Already have a session string? Paste it and I only check that it works. No code needed.\n\n"
        "▸ <b>Steps</b>\n"
        "Phone number → code → 2-step password (only if enabled) → result.\n\n"
        "▸ <b>The code keypad</b>\n"
        "Tap the digits, ⌫ deletes, ✅ Submit checks the code, 🔁 New code asks Telegram for a fresh one. "
        "Typing the code in the chat can make Telegram cancel it - if you type it, put a space between the digits.\n\n"
        "▸ <b>2-step password</b>\n"
        "If your account has one, the bot asks for it after the code. Your message is deleted at once.\n\n"
        "▸ <b>Commands</b>\n"
        "/start - main menu\n/login - choose a login method\n/generatesession - generate a session string\n"
        "/pastesession - check a ready session string\n/help - this guide\n/cancel - cancel the current action\n\n"
        "▸ <b>If the code does not arrive</b>\n"
        "Check the delivery line on the code screen: APP means the code is inside the Telegram app, "
        "in the chat named Telegram (blue tick) - it is not an SMS. Tap 🔁, wait a minute, "
        "or start /login again and choose 🔑 API ID / API Hash.\n\n"
        "▸ <b>Safety</b>\n"
        "A session string is the key to your account - never share it. Secrets you send (code, password, "
        "API Hash, session string) are deleted from the chat at once. Use your own accounts only.\n\n"
        "⚙️ <i>Powered by ESBG</i>"
    )


def where_text(sent):
    kind = enum_name(getattr(sent, "type", None))
    text = {
        "APP": "📲 <b>The code was sent inside Telegram:</b> open the chat named <b>Telegram</b> (blue tick) "
               "on a phone or computer where this account is already logged in. It is not an SMS.",
        "SMS": "💬 <b>The code was sent by SMS</b> to your phone.",
        "CALL": "📞 <b>Telegram will call you</b> and read the code aloud.",
        "FLASH_CALL": "📞 <b>Missed call:</b> the code is the last digits of the calling number.",
        "MISSED_CALL": "📞 <b>Missed call:</b> the code is the last digits of the calling number.",
        "EMAIL_CODE": "📧 <b>The code was sent by email</b> to your registered address.",
        "FRAGMENT_SMS": "🧩 <b>The code was sent through Fragment.</b>",
    }.get(kind, f"🔑 <b>The code was sent</b> ({kind.title() if kind else 'unknown method'}). Check your phone.")
    if kind == "APP" and not enum_name(getattr(sent, "next_type", None)):
        text += "\n\n⚠️ <b>Note:</b> Telegram does not offer an alternative delivery method (SMS / call) for this request."
    return text


def err_text(e):
    if isinstance(e, tg_errors.FloodWait):
        return f"⏳ <b>Too many attempts.</b> Please wait {e.value} seconds and try again."
    if isinstance(e, asyncio.TimeoutError):
        return "⚠️ <b>Timeout.</b> Telegram did not respond in time."
    if isinstance(e, OSError):
        return "⚠️ <b>Network error.</b> Unable to reach Telegram."
    return f"❌ <b>Error:</b> <code>{type(e).__name__}</code> ({getattr(e, 'ID', '-')})\n{str(e)[:200]}"


def log_err(step, uid, e):
    log.warning("login: %s failed uid=%s error=%s id=%s detail=%s", step, uid, type(e).__name__,
                getattr(e, "ID", "-"), str(e)[:300] or "-", exc_info=True)


def err_text_api(e, api):
    """err_text, with a clear line when a typed API ID / Hash is the reason."""
    if type(e).__name__ == "ApiIdInvalid":
        return "❌ <b>Invalid API credentials</b> (ApiIdInvalid).\nPlease verify your API ID and API Hash at my.telegram.org."
    if type(e).__name__ == "ApiIdPublishedFlood":
        return "❌ <b>API ID blocked</b> (ApiIdPublishedFlood).\nPlease create your own API key at my.telegram.org."
    return err_text(e)


def enum_name(v):
    return "" if v is None else str(getattr(v, "name", v)).upper()


# ======================================================================
# STATE
# ======================================================================
STATE = {}      # uid -> {"step", "digits", "kp", "kp_photo", "mode", "api_id", "api_hash", "tries", "ts"}
LOGIN = {}      # uid -> {"client", "phone", "hash", "next", "where", "tries", "rs_ts", "mode", "api"}
LOCKS = {}


def lock(uid):
    return LOCKS.setdefault(uid, asyncio.Lock())


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
    return Markup([
        [Btn("📱 Phone + OTP", "m|ph")],
        [Btn("🔑 API ID / API Hash", "m|own")],
        [Btn("🧬 Generate Session", "m|gen")],
        [Btn("📋 Paste Session", "m|paste")],
        [Btn("❓ Help", "help")],
        [Btn("❌ Close", "close")],
    ])


def close_markup():
    return Markup([[Btn("❌ Close", "close")]])


def keypad(lg):
    rows = [[Btn(str(n), f"d|{n}") for n in (1, 2, 3)], [Btn(str(n), f"d|{n}") for n in (4, 5, 6)],
            [Btn(str(n), f"d|{n}") for n in (7, 8, 9)],
            [Btn("⌫", "d|del"), Btn("0", "d|0"), Btn("✅ Submit", "d|ok")],
            [Btn("🔁 New code", "rs"), Btn("❌ Cancel", "cx")]]
    return Markup(rows)


# ======================================================================
# SHARED BEGINNERS - used by both the menu buttons and the /commands
# ======================================================================
async def begin_phone_login(uid):
    await reset(uid)
    STATE[uid] = {"step": "phone", "mode": "login", "ts": time.time()}
    return txt_phone_intro("login")


async def begin_generate(uid):
    await reset(uid)
    STATE[uid] = {"step": "phone", "mode": "generate", "ts": time.time()}
    return txt_phone_intro("generate")


async def begin_own_key(uid):
    await reset(uid)
    STATE[uid] = {"step": "api_id", "ts": time.time()}
    return txt_own_intro()


async def begin_paste(uid):
    await reset(uid)
    STATE[uid] = {"step": "paste", "tries": 0, "ts": time.time()}
    return txt_paste_intro()


async def send_help(uid):
    """Photo with the heading as caption, then the full guide as a second message
    (photo captions are limited to 1024 characters)."""
    photo = next_photo(uid)
    if photo is not None:
        try:
            await _send_photo_msg(uid, photo, txt_help_caption())
        except Exception as e:
            blacklist_photo(photo, type(e).__name__)
    await bot.send_message(uid, txt_help(), reply_markup=close_markup())


# ======================================================================
# LOGIN FLOWS (unchanged logic, restyled screens)
# ======================================================================
async def open_login(phone, api=None):
    """api = (api_id, api_hash) typed by the user, or None for the bot's own key."""
    last = None
    aid, ahash = api if api else (API_ID, API_HASH)
    for attempt in (1, 2):
        lc = Client(f"lb_{int(time.time() * 1000)}", api_id=aid, api_hash=ahash, in_memory=True)
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
        return await send_screen(uid, txt_invalid_phone())
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
            return await send_screen(uid, err_text_api(e, api))
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


async def finish(uid, lg):
    c = lg["client"]
    me = await c.get_me()
    string = await c.export_session_string()
    log.info("login: SUCCESS uid=%s account=%s mode=%s", uid, getattr(me, "id", "?"), lg.get("mode"))
    kp = (STATE.get(uid) or {}).get("kp")
    await reset(uid)
    if kp:
        try:
            await bot.delete_messages(uid, kp)
        except Exception:
            pass
    text = txt_result(me, string, lg.get("mode"), bool(lg.get("api")))
    await send_screen(uid, text)


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
            return await send_screen(uid, txt_paste_too_many())
        return await send_screen(uid, txt_paste_invalid(
            "That does not look like a session string. Send the whole string as one message."))
    async with lock(uid):
        status = await bot.send_message(uid, "⏳ Verifying the session…")
        chk = Client(f"chk_{uid}_{int(time.time())}", api_id=API_ID, api_hash=API_HASH, session_string=txt, in_memory=True)
        try:
            await asyncio.wait_for(chk.start(), CONNECT_TIMEOUT)
            me = await asyncio.wait_for(chk.get_me(), STEP_TIMEOUT)
        except Exception as e:
            log_err("paste_session", uid, e)
            return await send_screen(uid, txt_paste_invalid(err_text(e)))
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
            return await send_screen(uid, txt_paste_invalid(
                "This is a bot session. Paste the session string of a user account."))
        log.info("login: SESSION PASTE OK uid=%s account=%s library=%s", uid, getattr(me, "id", "?"), LIB)
        STATE.pop(uid, None)
        await send_screen(uid, txt_paste_ok(me))


async def do_api_id(m, uid):
    txt = (m.text or "").strip()
    try:
        await m.delete()
    except Exception:
        pass
    if not txt.isdigit() or not 4 <= len(txt) <= 12:
        return await send_screen(uid, txt_invalid_api_id())
    STATE[uid] = {"step": "api_hash", "api_id": int(txt), "ts": time.time()}
    await send_screen(uid, txt_own_step2())


async def do_api_hash(m, uid):
    txt = (m.text or "").strip()
    try:
        await m.delete()                                   # keep the hash out of the chat
    except Exception:
        pass
    if not re.fullmatch(r"[0-9a-fA-F]{32}", txt):
        return await send_screen(uid, txt_invalid_api_hash())
    st = STATE.get(uid) or {}
    STATE[uid] = {"step": "phone", "api_id": st.get("api_id"), "api_hash": txt.lower(), "mode": "login", "ts": time.time()}
    await send_screen(uid, txt_own_step3())


async def show_code_screen(uid, note="", rotate=False):
    st, lg = STATE.get(uid), LOGIN.get(uid)
    if not st or not lg:
        return
    text = txt_code_screen(lg, st.get("digits", ""), note)
    if rotate:
        photo = next_photo(uid)
        if st.get("kp") and st.get("kp_photo") and photo is not None:
            try:
                media = InputMediaPhoto(photo.file_id or photo.data, caption=clip_caption(text))
                m2 = await bot.edit_message_media(uid, st["kp"], media, reply_markup=keypad(lg))
                fid = _largest_file_id(m2)
                if fid:
                    photo.file_id = fid
                return
            except tg_errors.MessageNotModified:
                return
            except Exception:
                pass
        if st.get("kp"):
            try:
                await bot.delete_messages(uid, st["kp"])
            except Exception:
                pass
            st["kp"] = None
        sent = await send_screen(uid, text, keypad(lg))
        if sent is not None:
            st = STATE.get(uid)
            if st is not None:
                st["kp"] = sent.id
                st["kp_photo"] = bool(_largest_file_id(sent))
        return
    await edit_code_screen(uid, text, keypad(lg))


async def submit_code(uid, code, rotate=False):
    lg, st = LOGIN.get(uid), STATE.get(uid)
    if not lg or not st:
        return await send_screen(uid, txt_expired())
    async with lock(uid):
        try:
            res = await asyncio.wait_for(lg["client"].sign_in(lg["phone"], lg["hash"], code), STEP_TIMEOUT)
        except tg_errors.SessionPasswordNeeded:
            STATE[uid] = {"step": "password", "ts": time.time()}
            log.info("login: 2-step password needed uid=%s", uid)
            return await send_screen(uid, txt_password())
        except (tg_errors.PhoneCodeInvalid, getattr(tg_errors, "PhoneCodeEmpty", tg_errors.PhoneCodeInvalid)):
            lg["tries"] += 1
            st["digits"] = ""
            return await show_code_screen(uid, "❌ <b>Incorrect code.</b> Please try again.", rotate=rotate)
        except tg_errors.PhoneCodeExpired:
            try:
                sent = await asyncio.wait_for(lg["client"].send_code(lg["phone"]), STEP_TIMEOUT)
            except Exception as e:
                log_err("send_code (after expiry)", uid, e)
                await reset(uid)
                return await send_screen(uid, txt_expired() + "\n\n" + err_text(e))
            lg.update(hash=sent.phone_code_hash, next=enum_name(getattr(sent, "next_type", None)),
                      where=where_text(sent), tries=0, rs_ts=time.time())
            st["digits"] = ""
            return await show_code_screen(uid, "⌛ <b>Code expired.</b> A new one has been requested.", rotate=rotate)
        except (asyncio.TimeoutError, OSError) as e:
            return await show_code_screen(uid, err_text(e) + "\nPress ✅ Submit to retry.", rotate=rotate)
        except Exception as e:
            log_err("sign_in", uid, e)
            await reset(uid)
            return await send_screen(uid, err_text(e))
        if not hasattr(res, "id"):
            await reset(uid)
            return await send_screen(uid, txt_expired())
        await finish(uid, lg)


async def submit_password(m, uid):
    pwd = m.text or ""
    try:
        await m.delete()
    except Exception:
        pass
    lg = LOGIN.get(uid)
    if not lg:
        return await send_screen(uid, txt_expired())
    async with lock(uid):
        try:
            await asyncio.wait_for(lg["client"].check_password(pwd), STEP_TIMEOUT)
        except tg_errors.PasswordHashInvalid:
            return await send_screen(uid, "❌ <b>━━━ ❌ WRONG PASSWORD ━━━</b>\n\n"
                                          "▸ <b>Try again</b>\nSend the password again.\n\n"
                                          "💡 <i>/cancel</i> to stop.")
        except Exception as e:
            log_err("check_password", uid, e)
            await reset(uid)
            return await send_screen(uid, err_text(e))
        await finish(uid, lg)


# ======================================================================
# HANDLERS
# ======================================================================
@bot.on_message(filters.private & filters.command(["start", "login"]))
async def start_cmd(c, m):
    uid = m.from_user.id
    await reset(uid)
    cmd = (getattr(m, "command", None) or ["start"])[0]
    log.info("login bot: /%s uid=%s library=%s python=%s", cmd, uid, LIB, platform.python_version())
    text = txt_login_menu() if cmd == "login" else txt_welcome()
    await send_screen(uid, text, menu_markup())


@bot.on_message(filters.private & filters.command("help"))
async def help_cmd(c, m):
    """Works in any state and never touches a running login."""
    log.info("login bot: /help uid=%s", m.from_user.id)
    await send_help(m.from_user.id)


@bot.on_message(filters.private & filters.command("cancel"))
async def cancel_cmd(c, m):
    uid = m.from_user.id
    await reset(uid)
    log.info("login bot: /cancel uid=%s", uid)
    await send_screen(uid, txt_cancelled(), close_markup())


@bot.on_message(filters.private & filters.command("generatesession"))
async def generatesession_cmd(c, m):
    """Same as pressing the 🧬 Generate Session button."""
    uid = m.from_user.id
    text = await begin_generate(uid)
    log.info("login bot: /generatesession uid=%s", uid)
    await send_screen(uid, text)


@bot.on_message(filters.private & filters.command("pastesession"))
async def pastesession_cmd(c, m):
    """Same as pressing the 📋 Paste Session button."""
    uid = m.from_user.id
    text = await begin_paste(uid)
    log.info("login bot: /pastesession uid=%s", uid)
    await send_screen(uid, text)


@bot.on_message(filters.private & filters.text
                & ~filters.command(["start", "login", "generatesession", "pastesession", "help", "cancel"]))
async def text_router(c, m):
    uid = m.from_user.id
    st = STATE.get(uid)
    if not st or "step" not in st:
        return await send_screen(uid, txt_welcome(), menu_markup())
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
    if data == "close":                                    # ❌ Close deletes the message
        try:
            await q.message.delete()
        except Exception:
            pass
        return
    if data == "help":                                     # ❓ Help: fresh screen, any state
        try:
            await q.message.delete()
        except Exception:
            pass
        return await send_help(uid)
    if data == "cx":
        await reset(uid)
        return await swap_screen(q.message, uid, txt_cancelled(), close_markup())
    if data.startswith("m|"):                              # the four ways in
        way = data[2:]
        if way == "ph":
            text = await begin_phone_login(uid)
        elif way == "gen":
            text = await begin_generate(uid)
        elif way == "own":
            text = await begin_own_key(uid)
        elif way == "paste":
            text = await begin_paste(uid)
        else:
            return
        log.info("login bot: method=%s uid=%s", way, uid)
        return await swap_screen(q.message, uid, text)
    if not st or not lg or st.get("step") != "code":
        return await q.message.reply_text("⌛ This screen is outdated. Send /start to begin again.")
    if data.startswith("d|"):
        key, digits = data[2:], st.get("digits", "")
        if key == "ok":
            if len(digits) < 4:
                return await show_code_screen(uid, "⚠️ Please enter the complete code first.", rotate=True)
            return await submit_code(uid, digits, rotate=True)
        st["digits"] = digits[:-1] if key == "del" else (digits + key if key.isdigit() and len(digits) < 8 else digits)
        return await show_code_screen(uid, rotate=ROTATE_ON_KEYPAD)   # caption edit, keeps the picture
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
            await show_code_screen(uid, "🔁 <b>New code requested.</b> Only the latest code is valid.", rotate=True)


# ======================================================================
# STARTUP
# ======================================================================
async def health_server():
    """A tiny page on $PORT so Render (web service) sees the app as alive. Railway worker: no PORT, no server."""
    if not PORT:
        return None

    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = f"login bot ok - {LIB}".encode()
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
    load_photos()
    await bot.start()
    await register_commands()
    health = await health_server()
    me = await bot.get_me()
    log.info("Login bot started: @%s | library=%s | python=%s | photos=%d | %s",
             me.username, LIB, platform.python_version(), len(PHOTOS), platform.platform())
    await idle()
    if health:
        health.close()
    await bot.stop()


if __name__ == "__main__":
    bot.run(main())
