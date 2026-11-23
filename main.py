"""LOGIN BOT - sign in to a Telegram account, four ways in (dashboard on /start):
    1) Phone + OTP        : the bot's own API ID / Hash (environment) -> code -> (2-step password)
    2) API ID / API Hash  : your own key typed in the chat -> phone -> code -> (2-step password)
    3) Generate Session   : like 1), but the result is only a session string
    4) Paste Session      : paste a ready session string (made elsewhere): checked, no code needed

Screens carry a rotating photo from assets/photos above the text. The photo is handed to
Pyrogram as a fresh io.BytesIO (a raw bytes object is rejected by Pyrogram's uploader),
then cached as a file_id. If photos are off, missing or rejected, the same screen goes out
as text - the login flow never breaks.

Environment variables (Render: Environment tab / Railway: Variables):
    BOT_TOKEN   token of a bot made only for this (revoke it afterwards)
    API_ID, API_HASH   from my.telegram.org
    PORT        set by Render / Railway by itself (a tiny health page answers on it)
    PHOTOS      on (default) / off - turn the rotating photos off
    PHOTOS_DIR  optional folder that replaces assets/photos
    ROTATION_STATE_FILE  optional path for the rotation state (default data/rotation.json)
Start command: python main.py
"""
import asyncio
import functools
import io
import json
import logging
import os
import platform
import random
import re
import time
from html import escape as html_escape
from importlib import metadata

from pyrogram import Client, errors as tg_errors, filters, idle
from pyrogram.types import BotCommand, InputMediaPhoto
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("loginbot")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"Missing environment variable: {name}")
    return v


BOT_TOKEN = _need("BOT_TOKEN")
API_ID = int(_need("API_ID"))
API_HASH = _need("API_HASH")
PORT = int(os.environ.get("PORT", "0") or 0)

PHOTO_DIR = os.environ.get("PHOTOS_DIR") or os.path.join(BASE_DIR, "assets", "photos")
PHOTOS_ENABLED = os.environ.get("PHOTOS", "on").strip().lower() not in ("off", "0", "false", "no")
ROTATION_FILE = os.environ.get("ROTATION_STATE_FILE", os.path.join(BASE_DIR, "data", "rotation.json"))
CAPTION_LIMIT = 1024              # Telegram limit for a photo caption
TEXT_LIMIT = 4096                 # Telegram limit for one text message
ROTATE_ON_KEYPAD = False          # keypad digits only edit the caption and keep the current photo
LOGIN_TTL = 30 * 60               # an unfinished login is dropped after 30 minutes
MAX_ROTATION_USERS = 2000         # cap on the persisted rotation state


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

try:
    from PIL import Image as PILImage
    HAS_PIL = True
except Exception:
    PILImage = None
    HAS_PIL = False


# ======================================================================
# PHOTO STORE - read, validate and normalise every picture once at start
# ======================================================================
class Photo:
    __slots__ = ("path", "rel", "kind", "data", "category", "file_id", "failed")

    def __init__(self, path, rel, kind, data, category):
        self.path = path
        self.rel = rel                     # relative path, used by the persisted rotation state
        self.kind = kind                   # "jpeg" or "png" - what Telegram accepts as a photo
        self.data = data
        self.category = category
        self.file_id = None                # cached after the first successful upload
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
    """Convert anything Pillow can open to JPEG; alpha is flattened on white, huge pictures shrink."""
    im = PILImage.open(io.BytesIO(data))
    im.load()
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = PILImage.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    if len(data) > 8 * 1024 * 1024 or max(im.size) > 2560:
        im.thumbnail((2560, 2560))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


PHOTOS = []                     # all usable pictures, loaded once at start
PHOTO_BLACKLIST = set()         # indices removed from rotation after a send failure
BAGS = {}                       # uid -> {"bag": [indices], "last": index}
ROTATION = {}                   # uid (str) -> relative photo path, persisted across restarts
ROTATION_INDEX = {}             # relative photo path -> index in PHOTOS


def load_photos():
    """Read every file in PHOTO_DIR (recursively) at start. Never crashes."""
    global PHOTOS, ROTATION_INDEX
    PHOTOS, ROTATION_INDEX = [], {}
    if not PHOTOS_ENABLED:
        log.info("photos: PHOTOS=off - screens are sent as plain text")
        return
    if not os.path.isdir(PHOTO_DIR):
        log.info("photos: folder not found (%s) - screens are sent as plain text", PHOTO_DIR)
        return
    skipped = []
    for root, dirs, files in os.walk(PHOTO_DIR):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in (".txt", ".md"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, PHOTO_DIR).replace(os.sep, "/")
            try:
                size = os.path.getsize(path)
                if size == 0 or size > 64 * 1024 * 1024:
                    log.warning("photos: skipped %s (size %s bytes)", rel, size)
                    continue
                with open(path, "rb") as f:
                    data = f.read()
                kind = sniff_format(data[:16])
                category = os.path.basename(root).lower()
                if kind in ("jpeg", "png"):
                    if not _plausible(data, kind):
                        log.warning("photos: corrupt file skipped: %s", rel)
                        continue
                    if HAS_PIL:                     # integrity check; corrupt files are skipped
                        try:
                            with PILImage.open(io.BytesIO(data)) as im:
                                im.verify()
                        except Exception:
                            log.warning("photos: corrupt file skipped: %s", rel)
                            continue
                    PHOTOS.append(Photo(path, rel, kind, data, category))
                elif HAS_PIL:
                    # Telegram photos are JPEG / PNG: webp, gif, bmp and tiff are transcoded here.
                    try:
                        PHOTOS.append(Photo(path, rel, "jpeg", pil_to_jpeg(data), category))
                    except Exception:
                        log.warning("photos: unreadable file skipped: %s", rel)
                else:
                    skipped.append(rel)
            except Exception as e:
                log.warning("photos: could not read %s (%s)", rel, type(e).__name__)
    if skipped:
        log.warning("photos: %d file(s) skipped because Pillow is not installed (%s). "
                    "Add 'pillow' to requirements.txt to enable webp / gif / bmp / tiff.",
                    len(skipped), ", ".join(sorted(skipped)[:8]))
    for i, photo in enumerate(PHOTOS):
        ROTATION_INDEX[photo.rel] = i
    n_login = sum(1 for p in PHOTOS if p.category == "login")
    log.info("photos: %d ready (%d login, %d other)", len(PHOTOS), n_login, len(PHOTOS) - n_login)


def media_source(photo):
    """What Pyrogram can actually upload: a cached file_id, or a *fresh* BytesIO with .name set.

    Pyrogram's save_file() only accepts a path (str / PurePath) or an io.IOBase; raw bytes
    raise ValueError, which is why every photo send used to fail and screens fell back to text.
    A new buffer per send is required: concurrent sends share nothing.
    """
    if photo.file_id:
        return photo.file_id
    buf = io.BytesIO(photo.data)
    buf.name = os.path.basename(photo.rel) or "photo.jpg"
    return buf


# ======================================================================
# ROTATION
# ======================================================================
def load_rotation():
    """Restore per-user rotation state (best effort - read-only hosts simply skip it)."""
    global ROTATION
    ROTATION = {}
    if not ROTATION_FILE:
        return
    try:
        with open(ROTATION_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            ROTATION = {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
            log.info("photos: rotation state restored for %d user(s)", len(ROTATION))
    except FileNotFoundError:
        return
    except Exception as e:
        log.warning("photos: rotation state ignored (%s)", type(e).__name__)


_save_task = None
_save_disabled = False


def _write_rotation_blocking(snapshot):
    folder = os.path.dirname(ROTATION_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = f"{ROTATION_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f)
    os.replace(tmp, ROTATION_FILE)


async def _write_rotation():
    global _save_disabled, _save_task
    await asyncio.sleep(1.0)                       # debounce bursts of screens
    if _save_disabled:
        return
    snapshot = dict(list(ROTATION.items())[-MAX_ROTATION_USERS:])
    try:
        await asyncio.to_thread(_write_rotation_blocking, snapshot)
    except Exception as e:
        _save_disabled = True                      # ephemeral disk (Render free dyno): stop trying
        log.warning("photos: rotation state is not persisted (%s) - continuing in memory",
                    type(e).__name__)


def save_rotation_soon():
    """Write the rotation state in the background, at most one pending write at a time."""
    global _save_task
    if not ROTATION_FILE or _save_disabled:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _save_task is None or _save_task.done():
        _save_task = loop.create_task(_write_rotation())


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
    """The next picture for this user: every picture once before any repeats, never twice in a row.

    The last picture is remembered on disk, so a restart does not show the same image again.
    """
    if not PHOTOS_ENABLED or not PHOTOS:
        return None
    if len(BAGS) > 500:                       # tiny cleanup of stale bags
        for k in [k for k in BAGS if k != uid and k not in STATE and k not in LOGIN][:200]:
            BAGS.pop(k, None)
    st = BAGS.setdefault(uid, {"bag": [], "last": None})
    if st["last"] is None:
        restored = ROTATION_INDEX.get(ROTATION.get(str(uid), ""))
        if restored is not None:
            st["last"] = restored
    for _ in range(2):
        while st["bag"]:
            i = st["bag"].pop(0)
            if i in PHOTO_BLACKLIST:
                continue
            if i == st["last"] and len(_usable_photos()) > 1:
                continue
            _remember(uid, st, i)
            return PHOTOS[i]
        usable = _usable_photos()
        if not usable:
            return None
        st["bag"] = _refill(usable, st["last"])
    return None


def _remember(uid, st, index):
    st["last"] = index
    ROTATION[str(uid)] = PHOTOS[index].rel
    save_rotation_soon()


def blacklist_photo(photo, reason):
    """A picture Telegram rejected never comes back this run. Logged once."""
    if photo.failed:
        return
    photo.failed = True
    try:
        PHOTO_BLACKLIST.add(PHOTOS.index(photo))
    except ValueError:
        pass
    log.warning("photos: removed %s from rotation (%s)", photo.rel, reason)


# ======================================================================
# SENDING HELPERS - every screen goes through these
# ======================================================================
_TAG_RE = re.compile(r"<[^>]+>")
_CODE_TAG_RE = re.compile(r"</?code[^>]*>", re.IGNORECASE)


def _code_depth(text, upto):
    depth = 0
    for m in _CODE_TAG_RE.finditer(text[:upto]):
        depth += -1 if m.group(0).startswith("</") else 1
    return depth


def split_html(text, limit=CAPTION_LIMIT):
    """Split text into (caption, rest) without ever cutting a <code> block in half.

    A session string lives inside <code>, so a plain clip would destroy it.
    """
    text = text or ""
    if len(text) <= limit:
        return text, ""
    window = text[:limit]
    best = -1
    for m in re.finditer(r"\n\n", window):
        if _code_depth(text, m.end()) == 0:
            best = m.end()
    if best < 200:
        for m in re.finditer(r"\n", window):
            if m.end() >= 200 and _code_depth(text, m.end()) == 0:
                best = m.end()
    if best <= 0:
        best = len(window) - 1
    return text[:best].rstrip(), text[best:].lstrip("\n")


def clip_caption(text):
    """Photo captions are limited to 1024 characters (text messages to 4096)."""
    return split_html(text, CAPTION_LIMIT)[0]


def _chunks(text, limit=TEXT_LIMIT):
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


def _largest_file_id(msg):
    ph = getattr(msg, "photo", None)
    if ph:
        try:
            return max(ph, key=lambda p: (getattr(p, "width", 0) or 0) * (getattr(p, "height", 0) or 0)).file_id
        except Exception:
            return None
    return None


async def _send_photo_msg(uid, photo, caption, markup=None):
    sent = await bot.send_photo(uid, media_source(photo), caption=caption, reply_markup=markup)
    fid = _largest_file_id(sent)
    if fid:
        photo.file_id = fid
    return sent


async def _send_rest(uid, rest, markup=None):
    """Text that did not fit under the photo, split at Telegram's 4096 limit."""
    parts = _chunks(rest)
    last = None
    for i, part in enumerate(parts):
        last = await bot.send_message(uid, part, reply_markup=markup if i == len(parts) - 1 else None)
    return last


async def _send_text_only(uid, text, markup=None):
    parts = _chunks(text)
    last = None
    for i, part in enumerate(parts):
        last = await bot.send_message(uid, part, reply_markup=markup if i == len(parts) - 1 else None)
    return last


async def send_screen(uid, text, markup=None):
    """A screen with a fresh rotated photo on top; falls back to plain text, same text and buttons."""
    head, rest = split_html(text, CAPTION_LIMIT)
    for _ in range(3):
        photo = next_photo(uid)
        if photo is None:
            return await _send_text_only(uid, text, markup)
        try:
            msg = await _send_photo_msg(uid, photo, head, markup if not rest else None)
            if rest:
                await _send_rest(uid, rest, markup)
            return msg
        except tg_errors.FloodWait:
            break
        except Exception as e:
            blacklist_photo(photo, type(e).__name__)
    log.warning("photos: could not attach a picture - falling back to text")
    return await _send_text_only(uid, text, markup)


async def swap_screen(msg, uid, text, markup=None):
    """Inline-button reply: replace picture AND caption in place (edit_message_media).
    If the message is old, is plain text, or the text does not fit a caption: delete and resend."""
    head, rest = split_html(text, CAPTION_LIMIT)
    if msg is not None and not rest and getattr(msg, "photo", None):
        photo = next_photo(uid)
        if photo is not None:
            try:
                media = InputMediaPhoto(media_source(photo), caption=head)
                m2 = await bot.edit_message_media(msg.chat.id, msg.id, media, reply_markup=markup)
                fid = _largest_file_id(m2)
                if fid:
                    photo.file_id = fid
                return m2
            except tg_errors.MessageNotModified:
                return msg
            except Exception as e:
                log.info("photos: in-place edit failed (%s) - resending the screen", type(e).__name__)
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
                await bot.edit_message_caption(uid, st["kp"], caption=clip_caption(text), reply_markup=markup)
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
# SCREEN TEXTS - one house style: title, rule, short blocks, footer hint
# ======================================================================
RULE = "──────────────"


def _name_of(user):
    if user is None:
        return "there"
    raw = getattr(user, "first_name", None) or getattr(user, "username", None) or "there"
    raw = str(raw).strip() or "there"
    return html_escape(raw[:32])


def _options_block():
    return (
        "▸ 📱 <b>Phone + OTP</b> — the fastest way in\n"
        "▸ 🔑 <b>API ID / Hash</b> — your own key, lower risk\n"
        "▸ 🧬 <b>Generate Session</b> — get a reusable session string\n"
        "▸ 📋 <b>Paste Session</b> — verify a session you already have"
    )


def txt_home(user):
    return (
        "🔐 <b>LOGIN BOT</b>\n"
        "<i>Telegram account access, explained step by step</i>\n"
        f"{RULE}\n\n"
        f"👋 Welcome, <b>{_name_of(user)}</b>.\n\n"
        "Sign in to a Telegram account and see exactly how Telegram delivers your code.\n\n"
        "<b>Available options</b>\n"
        f"{_options_block()}\n\n"
        "<b>Privacy</b>\n"
        "Codes, passwords, API hashes and session strings are deleted the moment you send them, "
        "and are never stored.\n\n"
        f"{RULE}\n"
        "Choose an option below."
    )


def txt_login_menu():
    return (
        "🔐 <b>CONNECT YOUR TELEGRAM ACCOUNT</b>\n"
        "<i>Pick the method that fits you</i>\n"
        f"{RULE}\n\n"
        f"{_options_block()}\n\n"
        "<b>Not sure?</b>\n"
        "Start with 📱 Phone + OTP. If the code never arrives, use 🔑 API ID / Hash.\n\n"
        f"{RULE}\n"
        "Tap a method to continue."
    )


def txt_phone_intro(mode="login"):
    if mode == "generate":
        return (
            "🧬 <b>GENERATE SESSION</b>\n"
            "<i>Sign in once, keep a session string</i>\n"
            f"{RULE}\n\n"
            "<b>What this does</b>\n"
            "Logs in with the bot's own API key and returns a session string you can paste back later.\n\n"
            "<b>You'll need</b>\n"
            "▸ Your phone number with country code — <code>+919876543210</code>\n"
            "▸ The code Telegram sends\n"
            "▸ Your 2-step password, if the account has one\n\n"
            "<b>Limits</b>\n"
            "▸ A session string is full account access: keep it secret\n"
            "▸ Wrong codes are rejected; request a new one with 🔁\n\n"
            f"{RULE}\n"
            "Send your phone number to continue.\n"
            "💡 /cancel stops at any time."
        )
    return (
        "📱 <b>PHONE + OTP LOGIN</b>\n"
        "<i>Fastest sign-in with a one-time code</i>\n"
        f"{RULE}\n\n"
        "<b>How it works</b>\n"
        "1. Send your phone number\n"
        "2. Telegram sends you a code\n"
        "3. Enter it on the keypad\n"
        "4. Enter your 2-step password, if you have one\n\n"
        "<b>You'll need</b>\n"
        "▸ Your number with country code — <code>+919876543210</code>\n"
        "▸ Access to the code: in the Telegram app, by SMS, call or email\n\n"
        "<b>Limits</b>\n"
        "▸ Typing the code as chat text can make Telegram cancel it — use the keypad\n\n"
        f"{RULE}\n"
        "Send your phone number to continue.\n"
        "💡 /cancel stops at any time."
    )


def txt_own_intro():
    return (
        "🔑 <b>YOUR OWN API KEY</b>\n"
        "<i>Lower ban risk, best when the code never arrives</i>\n"
        f"{RULE}\n\n"
        "<b>What this does</b>\n"
        "Logs in with your own API ID and API Hash instead of the bot's shared key.\n\n"
        "<b>Where to get it</b>\n"
        "my.telegram.org → <i>API development tools</i> → create an app.\n\n"
        "<b>You'll need</b>\n"
        "▸ API ID (numbers)\n"
        "▸ API Hash (32 letters and digits)\n"
        "▸ Your phone number\n\n"
        "<b>Privacy</b>\n"
        "The key is used for this login only, is never stored, and your messages are deleted.\n\n"
        f"{RULE}\n"
        "<b>Step 1 of 3</b> — send your API ID.\n"
        "💡 /cancel stops at any time."
    )


def txt_own_step2():
    return (
        "🔑 <b>YOUR OWN API KEY</b>\n"
        "<i>Step 2 of 3</i>\n"
        f"{RULE}\n\n"
        "✅ API ID accepted.\n\n"
        "<b>Send your API Hash</b>\n"
        "It is the 32-character string next to your API ID at my.telegram.org.\n\n"
        "Your message is deleted immediately and the hash is not stored.\n\n"
        f"{RULE}\n"
        "💡 /cancel stops at any time."
    )


def txt_own_step3():
    return (
        "🔑 <b>YOUR OWN API KEY</b>\n"
        "<i>Step 3 of 3</i>\n"
        f"{RULE}\n\n"
        "✅ API credentials saved for this login.\n\n"
        "<b>Send your phone number</b>\n"
        "Include the country code — <code>+919876543210</code>\n\n"
        f"{RULE}\n"
        "💡 /cancel stops at any time."
    )


def txt_paste_intro():
    return (
        "📋 <b>PASTE SESSION</b>\n"
        "<i>Check a session string you already have</i>\n"
        f"{RULE}\n\n"
        "<b>What this does</b>\n"
        "Connects with the session string, reads the account behind it and closes it again. "
        "No phone number and no code.\n\n"
        "<b>You'll need</b>\n"
        "▸ A complete session string, sent as one message\n\n"
        "<b>Privacy</b>\n"
        "Your message is deleted at once and the string is not stored.\n\n"
        "<b>Limits</b>\n"
        "▸ Five invalid attempts end the session — then /start again\n"
        "▸ Bot sessions are not accepted, only user accounts\n\n"
        f"{RULE}\n"
        "Send the session string to continue.\n"
        "💡 /cancel stops at any time."
    )


def txt_code_screen(lg, digits, note=""):
    shown = " ".join(digits) if digits else "—"
    t = (
        "🔢 <b>ENTER THE CODE</b>\n"
        "<i>Step 2 of 3</i>\n"
        f"{RULE}\n\n"
        f"{lg['where']}\n\n"
        "Tap the digits below, then ✅ Submit.\n"
        "If you type it instead, separate the digits with spaces so Telegram does not cancel the code.\n\n"
        f"<b>Code:</b> <code>{shown}</code>"
    )
    if note:
        t += f"\n\n{note}"
    t += (
        "\n\n<i>Not arriving? Tap 🔁 New code, wait a minute, or start /login again and choose "
        "🔑 API ID / Hash.</i>\n\n"
        f"{RULE}\n"
        "💡 /cancel stops the login."
    )
    return t


def txt_password():
    return (
        "🔒 <b>TWO-STEP VERIFICATION</b>\n"
        "<i>Last step</i>\n"
        f"{RULE}\n\n"
        "Your account is protected with a cloud password.\n\n"
        "<b>Send your password</b>\n"
        "One message, nothing else. It is deleted immediately and never stored.\n\n"
        "<b>If you don't know it</b>\n"
        "Recover it in Telegram: Settings → Privacy and Security → Two-Step Verification.\n\n"
        f"{RULE}\n"
        "💡 /cancel stops the login."
    )


def txt_invalid_phone():
    return (
        "❌ <b>INVALID PHONE NUMBER</b>\n"
        f"{RULE}\n\n"
        "That does not look like a phone number.\n\n"
        "<b>Correct format</b>\n"
        "Country code first, then the number — <code>+919876543210</code>\n\n"
        "Send it again, or /cancel to stop."
    )


def txt_invalid_api_id():
    return (
        "❌ <b>INVALID API ID</b>\n"
        f"{RULE}\n\n"
        "The API ID is a number, usually 7–8 digits.\n\n"
        "Send it again, or /cancel to stop."
    )


def txt_invalid_api_hash():
    return (
        "❌ <b>INVALID API HASH</b>\n"
        f"{RULE}\n\n"
        "The API Hash is exactly 32 letters and digits.\n\n"
        "Send it again, or /cancel to stop."
    )


def txt_paste_invalid(reason):
    return (
        "📋 <b>SESSION NOT ACCEPTED</b>\n"
        f"{RULE}\n\n"
        f"{reason}\n\n"
        "Paste the whole string again, or /cancel to stop."
    )


def txt_paste_too_many():
    return (
        "⛔ <b>TOO MANY ATTEMPTS</b>\n"
        f"{RULE}\n\n"
        "Five invalid session strings were sent, so this attempt is over.\n\n"
        "Send /start to begin again."
    )


def txt_paste_ok(me):
    return (
        "📋 <b>SESSION VERIFIED</b>\n"
        f"{RULE}\n\n"
        "✅ The session works. No code was needed.\n\n"
        f"<b>Account:</b> {html_escape(str(getattr(me, 'first_name', '-'))[:40])} "
        f"(@{html_escape(str(getattr(me, 'username', None) or '-'))[:32]})\n"
        f"<b>Library:</b> {LIB}\n\n"
        "<b>Keep it secret</b>\n"
        "A session string is the key to your account — never share it."
    )


def txt_result_head(me, mode, own_key):
    title = "🧬 <b>SESSION GENERATED</b>" if mode == "generate" else "✅ <b>LOGIN WORKED</b>"
    key_line = ("your own API ID / Hash from my.telegram.org" if own_key
                else "the bot's own API ID / Hash")
    return (
        f"{title}\n"
        f"{RULE}\n\n"
        f"<b>Account:</b> {html_escape(str(getattr(me, 'first_name', '-'))[:40])}\n"
        f"<b>Library:</b> {LIB}\n"
        f"<b>API key:</b> {key_line}\n\n"
        "Your session string is in the next message."
    )


def txt_result_session(string):
    return (
        "<b>🔑 YOUR SESSION STRING</b>\n"
        f"{RULE}\n\n"
        f"<code>{string}</code>\n\n"
        "<b>Keep it secret</b>\n"
        "This string gives full access to the account. Save it somewhere safe and delete this message.\n\n"
        "You can verify it any time with 📋 Paste Session."
    )


def txt_cancelled():
    return (
        "✋ <b>CANCELLED</b>\n"
        f"{RULE}\n\n"
        "The current action was stopped. Nothing was saved.\n\n"
        "Open the menu to start again."
    )


def txt_expired():
    return (
        "⌛ <b>SESSION EXPIRED</b>\n"
        f"{RULE}\n\n"
        "This login took too long or was reset.\n\n"
        "Open the menu to begin again."
    )


def txt_unknown():
    return (
        "🤔 <b>UNKNOWN COMMAND</b>\n"
        f"{RULE}\n\n"
        "That command does not exist.\n\n"
        "<b>Available</b>\n"
        "/start · /login · /generatesession · /pastesession · /guide · /help · /cancel"
    )


def txt_error(e):
    """User-facing failure text: no traceback, no internals."""
    if isinstance(e, tg_errors.FloodWait):
        return (f"⏳ <b>SLOW DOWN</b>\n{RULE}\n\nTelegram asks us to wait {e.value} seconds.\n\n"
                "Try again after that.")
    if isinstance(e, tg_errors.RPCError):
        code = getattr(e, "ID", "-")
        return (f"⚠️ <b>TELEGRAM REFUSED THE REQUEST</b>\n{RULE}\n\n"
                f"Error code: <code>{html_escape(str(code))}</code>\n\n"
                "Try again in a moment. If it keeps happening, send /start and choose "
                "🔑 API ID / Hash.")
    if isinstance(e, (asyncio.TimeoutError, OSError)):
        return ("⚠️ <b>CONNECTION PROBLEM</b>\n"
                f"{RULE}\n\n"
                "Telegram did not answer in time. Check the connection and try again.")
    return ("⚠️ <b>SOMETHING WENT WRONG</b>\n"
            f"{RULE}\n\n"
            "The action could not be completed.\n\n"
            "Try again, or send /start for a fresh start.")


def where_text(sent):
    kind = enum_name(getattr(sent, "type", None))
    text = {
        "APP": "📲 <b>The code was sent inside Telegram:</b> open the chat named <b>Telegram</b> (blue tick) "
               "on a device where this account is already signed in. It is not an SMS.",
        "SMS": "💬 <b>The code was sent by SMS</b> to your phone.",
        "CALL": "📞 <b>Telegram will call you</b> and read the code aloud.",
        "FLASH_CALL": "📞 <b>Missed call:</b> the code is the last digits of the calling number.",
        "MISSED_CALL": "📞 <b>Missed call:</b> the code is the last digits of the calling number.",
        "EMAIL_CODE": "📧 <b>The code was sent by email</b> to your registered address.",
        "FRAGMENT_SMS": "🧩 <b>The code was sent through Fragment.</b>",
    }.get(kind, f"🔑 <b>The code was sent</b> ({kind.title() if kind else 'unknown method'}). Check your phone.")
    if kind == "APP" and not enum_name(getattr(sent, "next_type", None)):
        text += "\n\n⚠️ <b>Note:</b> Telegram offers no alternative delivery (SMS / call) for this request."
    return text


def err_text(e):
    if isinstance(e, tg_errors.FloodWait):
        return f"⏳ <b>Too many attempts.</b> Please wait {e.value} seconds and try again."
    if isinstance(e, asyncio.TimeoutError):
        return "⚠️ <b>Timeout.</b> Telegram did not respond in time."
    if isinstance(e, OSError):
        return "⚠️ <b>Network error.</b> Unable to reach Telegram."
    return f"❌ <b>Error:</b> <code>{type(e).__name__}</code> ({getattr(e, 'ID', '-')})"


def err_text_api(e, api):
    """err_text, with a clear line when a typed API ID / Hash is the reason."""
    if type(e).__name__ == "ApiIdInvalid":
        return ("❌ <b>INVALID API CREDENTIALS</b>\n"
                "Check your API ID and API Hash at my.telegram.org, then try again.")
    if type(e).__name__ == "ApiIdPublishedFlood":
        return ("❌ <b>API KEY BLOCKED</b>\n"
                "This API ID is used too widely. Create your own at my.telegram.org.")
    return err_text(e)


def log_err(step, uid, e):
    # Never log message contents: only step, user, error type and ids.
    log.warning("login: %s failed uid=%s error=%s id=%s detail=%s", step, uid, type(e).__name__,
                getattr(e, "ID", "-"), str(e)[:300] or "-", exc_info=True)


def enum_name(v):
    return "" if v is None else str(getattr(v, "name", v)).upper()


# ======================================================================
# NAVIGATION
# ======================================================================
def home_markup():
    return Markup([
        [Btn("📱 Phone + OTP", "m|ph"), Btn("🔑 API ID / Hash", "m|own")],
        [Btn("🧬 Generate Session", "m|gen"), Btn("📋 Paste Session", "m|paste")],
        [Btn("📘 Guide", "nav|guide"), Btn("ℹ️ About", "g|about")],
        [Btn("✖ Close", "close")],
    ])


def login_markup():
    return Markup([
        [Btn("📱 Phone + OTP", "m|ph"), Btn("🔑 API ID / Hash", "m|own")],
        [Btn("🧬 Generate Session", "m|gen"), Btn("📋 Paste Session", "m|paste")],
        [Btn("🏠 Home", "nav|home"), Btn("✖ Close", "close")],
    ])


def guide_markup():
    return Markup([
        [Btn("⚙️ How it works", "g|how"), Btn("🔢 Code & keypad", "g|code")],
        [Btn("🛠 Troubleshooting", "g|trouble"), Btn("🛡 Safety", "g|safety")],
        [Btn("ℹ️ About the bot", "g|about")],
        [Btn("🏠 Home", "nav|home"), Btn("✖ Close", "close")],
    ])


def topic_markup():
    return Markup([
        [Btn("‹ Guide", "nav|guide"), Btn("🏠 Home", "nav|home")],
        [Btn("✖ Close", "close")],
    ])


def nav_markup():
    return Markup([[Btn("🏠 Home", "nav|home"), Btn("✖ Close", "close")]])


def method_markup():
    return Markup([[Btn("✖ Cancel", "cx"), Btn("🏠 Home", "nav|home")]])


def keypad(lg):
    rows = [[Btn(str(n), f"d|{n}") for n in (1, 2, 3)], [Btn(str(n), f"d|{n}") for n in (4, 5, 6)],
            [Btn(str(n), f"d|{n}") for n in (7, 8, 9)],
            [Btn("⌫", "d|del"), Btn("0", "d|0"), Btn("✅ Submit", "d|ok")],
            [Btn("🔁 New code", "rs"), Btn("✖ Cancel", "cx")]]
    return Markup(rows)


# ======================================================================
# GUIDE PAGES
# ======================================================================
def txt_guide_menu():
    return (
        "📘 <b>GUIDE</b>\n"
        "<i>Everything this bot does, in short</i>\n"
        f"{RULE}\n\n"
        "▸ <b>How it works</b> — the four login methods\n"
        "▸ <b>Code & keypad</b> — entering the one-time code\n"
        "▸ <b>Troubleshooting</b> — when the code does not arrive\n"
        "▸ <b>Safety</b> — how your secrets are handled\n"
        "▸ <b>About</b> — version, library, commands\n\n"
        f"{RULE}\n"
        "Pick a topic."
    )


def txt_topic_how():
    return (
        "⚙️ <b>HOW IT WORKS</b>\n"
        f"{RULE}\n\n"
        "▸ 📱 <b>Phone + OTP</b>\n"
        "The bot's own API key, your number, the code. The quickest route.\n\n"
        "▸ 🔑 <b>API ID / Hash</b>\n"
        "Your own key from my.telegram.org. Lower ban risk and the best choice when the code "
        "does not arrive.\n\n"
        "▸ 🧬 <b>Generate Session</b>\n"
        "Same flow as Phone + OTP, but the result is a session string instead of a live login.\n\n"
        "▸ 📋 <b>Paste Session</b>\n"
        "A session string made elsewhere is checked and closed again. No code needed.\n\n"
        "Every method ends with a clear result screen, or an explanation of what went wrong."
    )


def txt_topic_code():
    return (
        "🔢 <b>CODE & KEYPAD</b>\n"
        f"{RULE}\n\n"
        "▸ Tap the digits, then <b>✅ Submit</b>\n"
        "▸ <b>⌫</b> deletes the last digit\n"
        "▸ <b>🔁 New code</b> asks Telegram for a fresh code (one request every 15 seconds)\n"
        "▸ <b>✖ Cancel</b> drops the login\n\n"
        "<b>Why the keypad?</b>\n"
        "Typing a login code as plain chat text can make Telegram cancel it. If you must type it, "
        "put a space between the digits: <code>1 2 3 4 5</code>\n\n"
        "Only the latest code is valid after a resend."
    )


def txt_topic_trouble():
    return (
        "🛠 <b>TROUBLESHOOTING</b>\n"
        f"{RULE}\n\n"
        "▸ <b>The code never arrives</b>\n"
        "Read the delivery line on the code screen. <b>APP</b> means the code is inside Telegram, "
        "in the chat named Telegram (blue tick) — not an SMS. Tap 🔁, wait a minute, or restart with "
        "🔑 API ID / Hash.\n\n"
        "▸ <b>Incorrect code</b>\n"
        "Request a new one; older codes stop working.\n\n"
        "▸ <b>Two-step password</b>\n"
        "Use the cloud password of the account, not the SMS code.\n\n"
        "▸ <b>Flood wait</b>\n"
        "Telegram is rate-limiting. Wait the number of seconds shown, then retry.\n\n"
        "▸ <b>Frozen screen</b>\n"
        "Send /start. An unfinished login is dropped after 30 minutes anyway."
    )


def txt_topic_safety():
    return (
        "🛡 <b>SAFETY</b>\n"
        f"{RULE}\n\n"
        "▸ <b>Deleted immediately</b>\n"
        "Codes, 2-step passwords, API hashes and session strings are removed from the chat as soon "
        "as they are read.\n\n"
        "▸ <b>Not stored</b>\n"
        "None of those values are written to a database or a log file.\n\n"
        "▸ <b>A session string is a password</b>\n"
        "It grants full access to the account. Never share it, and delete the message that contains it.\n\n"
        "▸ <b>Use your own account</b>\n"
        "Only sign in to accounts you own."
    )


def txt_topic_about():
    photos = f"{len(PHOTOS)} pictures in rotation" if PHOTOS_ENABLED and PHOTOS else "photos off"
    return (
        "ℹ️ <b>ABOUT</b>\n"
        f"{RULE}\n\n"
        "<b>Login Bot</b> — a focused Telegram sign-in tester.\n\n"
        f"<b>Library:</b> {LIB}\n"
        f"<b>Python:</b> {platform.python_version()}\n"
        f"<b>Media:</b> {photos}\n\n"
        "<b>Commands</b>\n"
        "/start — dashboard\n"
        "/login — choose a method\n"
        "/generatesession — session string\n"
        "/pastesession — verify a session\n"
        "/guide — this guide\n"
        "/help — full manual\n"
        "/cancel — stop the current action\n\n"
        f"{RULE}\n"
        "⚙️ <i>Powered by ESBG</i>"
    )


def _topic(key):
    return {
        "how": txt_topic_how,
        "code": txt_topic_code,
        "trouble": txt_topic_trouble,
        "safety": txt_topic_safety,
        "about": txt_topic_about,
    }.get(key)


def txt_help_caption():
    return (
        "❓ <b>HELP</b>\n"
        f"{RULE}\n\n"
        "How the bot works, step by step. The full manual is in the next message."
    )


def txt_help():
    return (
        "❓ <b>FULL MANUAL</b>\n"
        f"{RULE}\n\n"
        "This bot signs you in to a Telegram account and shows exactly how the login code is delivered. "
        "Four ways in:\n\n"
        "▸ <b>📱 Phone + OTP</b>\n"
        "The quickest way. Send your number, then the code Telegram gives you.\n\n"
        "▸ <b>🔑 API ID / API Hash</b>\n"
        "Your own key from my.telegram.org. Lower ban risk, and the best choice when the code never arrives.\n\n"
        "▸ <b>🧬 Generate Session</b>\n"
        "Log in once and the bot makes a session string for you.\n\n"
        "▸ <b>📋 Paste Session</b>\n"
        "Already have a session string? Paste it and the bot only checks that it works.\n\n"
        "▸ <b>Steps</b>\n"
        "Phone number → code → 2-step password (only if enabled) → result.\n\n"
        "▸ <b>The code keypad</b>\n"
        "Tap the digits, ⌫ deletes, ✅ Submit checks the code, 🔁 New code asks Telegram for a fresh one. "
        "Typing the code as chat text can make Telegram cancel it — if you type it, use spaces between "
        "the digits.\n\n"
        "▸ <b>2-step password</b>\n"
        "If the account has one, the bot asks for it after the code. Your message is deleted at once.\n\n"
        "▸ <b>Commands</b>\n"
        "/start — dashboard\n/login — choose a login method\n/generatesession — generate a session string\n"
        "/pastesession — check a ready session string\n/guide — short topic guide\n/help — this manual\n"
        "/cancel — cancel the current action\n\n"
        "▸ <b>If the code does not arrive</b>\n"
        "Check the delivery line: APP means the code is inside the Telegram app, in the chat named "
        "Telegram (blue tick) — not an SMS. Tap 🔁, wait a minute, or restart with 🔑 API ID / Hash.\n\n"
        "▸ <b>Safety</b>\n"
        "A session string is the key to your account — never share it. Secrets you send are deleted from "
        "the chat at once. Use your own accounts only.\n\n"
        f"{RULE}\n"
        "⚙️ <i>Powered by ESBG</i>"
    )


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
    """Photo with the heading as caption, then the full manual as a text message (caption limit)."""
    await send_screen(uid, txt_help_caption())
    await _send_text_only(uid, txt_help(), nav_markup())


async def send_result(uid, head, string):
    """Photo screen (header) + a separate message holding the session string, so nothing is clipped."""
    await send_screen(uid, head)
    await _send_text_only(uid, txt_result_session(string), home_markup())


# ======================================================================
# LOGIN FLOWS (logic unchanged, screens restyled)
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
        return await send_screen(uid, txt_invalid_phone(), method_markup())
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
            return await send_screen(uid, err_text_api(e, api), method_markup())
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
    await send_result(uid, txt_result_head(me, lg.get("mode"), bool(lg.get("api"))), string)


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
            return await send_screen(uid, txt_paste_too_many(), nav_markup())
        return await send_screen(uid, txt_paste_invalid(
            "That does not look like a session string. Send the whole string as one message."), method_markup())
    async with lock(uid):
        status = await bot.send_message(uid, "⏳ Verifying the session…")
        chk = Client(f"chk_{uid}_{int(time.time())}", api_id=API_ID, api_hash=API_HASH, session_string=txt, in_memory=True)
        try:
            await asyncio.wait_for(chk.start(), CONNECT_TIMEOUT)
            me = await asyncio.wait_for(chk.get_me(), STEP_TIMEOUT)
        except Exception as e:
            log_err("paste_session", uid, e)
            return await send_screen(uid, txt_paste_invalid(err_text(e)), method_markup())
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
                "This is a bot session. Paste the session string of a user account."), method_markup())
        log.info("login: SESSION PASTE OK uid=%s account=%s library=%s", uid, getattr(me, "id", "?"), LIB)
        STATE.pop(uid, None)
        await send_screen(uid, txt_paste_ok(me), home_markup())


async def do_api_id(m, uid):
    txt = (m.text or "").strip()
    try:
        await m.delete()
    except Exception:
        pass
    if not txt.isdigit() or not 4 <= len(txt) <= 12:
        return await send_screen(uid, txt_invalid_api_id(), method_markup())
    STATE[uid] = {"step": "api_hash", "api_id": int(txt), "ts": time.time()}
    await send_screen(uid, txt_own_step2(), method_markup())


async def do_api_hash(m, uid):
    txt = (m.text or "").strip()
    try:
        await m.delete()                                   # keep the hash out of the chat
    except Exception:
        pass
    if not re.fullmatch(r"[0-9a-fA-F]{32}", txt):
        return await send_screen(uid, txt_invalid_api_hash(), method_markup())
    st = STATE.get(uid) or {}
    STATE[uid] = {"step": "phone", "api_id": st.get("api_id"), "api_hash": txt.lower(), "mode": "login",
                  "ts": time.time()}
    await send_screen(uid, txt_own_step3(), method_markup())


async def show_code_screen(uid, note="", rotate=False):
    st, lg = STATE.get(uid), LOGIN.get(uid)
    if not st or not lg:
        return
    text = txt_code_screen(lg, st.get("digits", ""), note)
    if rotate:
        photo = next_photo(uid)
        if st.get("kp") and st.get("kp_photo") and photo is not None:
            try:
                media = InputMediaPhoto(media_source(photo), caption=clip_caption(text))
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
        return await send_screen(uid, txt_expired(), nav_markup())
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
                return await send_screen(uid, txt_expired() + "\n\n" + err_text(e), nav_markup())
            lg.update(hash=sent.phone_code_hash, next=enum_name(getattr(sent, "next_type", None)),
                      where=where_text(sent), tries=0, rs_ts=time.time())
            st["digits"] = ""
            return await show_code_screen(uid, "⌛ <b>Code expired.</b> A new one has been requested.", rotate=rotate)
        except (asyncio.TimeoutError, OSError) as e:
            return await show_code_screen(uid, err_text(e) + "\nPress ✅ Submit to retry.", rotate=rotate)
        except tg_errors.FloodWait:
            raise
        except Exception as e:
            log_err("sign_in", uid, e)
            await reset(uid)
            return await send_screen(uid, err_text(e), nav_markup())
        if not hasattr(res, "id"):
            await reset(uid)
            return await send_screen(uid, txt_expired(), nav_markup())
        await finish(uid, lg)


async def submit_password(m, uid):
    pwd = m.text or ""
    try:
        await m.delete()
    except Exception:
        pass
    lg = LOGIN.get(uid)
    if not lg:
        return await send_screen(uid, txt_expired(), nav_markup())
    async with lock(uid):
        try:
            await asyncio.wait_for(lg["client"].check_password(pwd), STEP_TIMEOUT)
        except tg_errors.PasswordHashInvalid:
            return await send_screen(uid, "❌ <b>WRONG PASSWORD</b>\n" + RULE + "\n\n"
                                          "Send the password again, or /cancel to stop.")
        except Exception as e:
            log_err("check_password", uid, e)
            await reset(uid)
            return await send_screen(uid, err_text(e), nav_markup())
        await finish(uid, lg)


# ======================================================================
# HANDLERS
# ======================================================================
def _target(upd):
    try:
        if getattr(upd, "from_user", None):
            return upd.from_user.id
        chat = getattr(upd, "chat", None)
        return getattr(chat, "id", None)
    except Exception:
        return None


def guarded(step):
    """Last-resort protection: log the traceback, tell the user something useful."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(c, upd, *a, **kw):
            try:
                return await fn(c, upd, *a, **kw)
            except Exception as e:
                log.error("%s failed: %s: %s", step, type(e).__name__, str(e)[:300], exc_info=True)
                uid = _target(upd)
                if uid:
                    try:
                        await send_screen(uid, txt_error(e), nav_markup())
                    except Exception:
                        pass
                if hasattr(upd, "answer"):
                    try:
                        await upd.answer()
                    except Exception:
                        pass
        return wrapper
    return deco


def is_stale(st):
    return bool(st) and st.get("step") in ("phone", "api_id", "api_hash", "paste", "password", "code") \
        and time.time() - st.get("ts", 0) > LOGIN_TTL


@bot.on_message(filters.private & filters.command(["start", "login"]))
@guarded("start")
async def start_cmd(c, m):
    uid = m.from_user.id
    await reset(uid)
    cmd = (getattr(m, "command", None) or ["start"])[0]
    log.info("login bot: /%s uid=%s library=%s python=%s", cmd, uid, LIB, platform.python_version())
    if cmd == "login":
        return await send_screen(uid, txt_login_menu(), login_markup())
    await send_screen(uid, txt_home(m.from_user), home_markup())


@bot.on_message(filters.private & filters.command("guide"))
@guarded("guide")
async def guide_cmd(c, m):
    uid = m.from_user.id
    await reset(uid)
    log.info("login bot: /guide uid=%s", uid)
    await send_screen(uid, txt_guide_menu(), guide_markup())


@bot.on_message(filters.private & filters.command("help"))
@guarded("help")
async def help_cmd(c, m):
    """Works in any state and never touches a running login."""
    log.info("login bot: /help uid=%s", m.from_user.id)
    await send_help(m.from_user.id)


@bot.on_message(filters.private & filters.command("cancel"))
@guarded("cancel")
async def cancel_cmd(c, m):
    uid = m.from_user.id
    await reset(uid)
    log.info("login bot: /cancel uid=%s", uid)
    await send_screen(uid, txt_cancelled(), nav_markup())


@bot.on_message(filters.private & filters.command("generatesession"))
@guarded("generatesession")
async def generatesession_cmd(c, m):
    """Same as pressing the 🧬 Generate Session button."""
    uid = m.from_user.id
    text = await begin_generate(uid)
    log.info("login bot: /generatesession uid=%s", uid)
    await send_screen(uid, text, method_markup())


@bot.on_message(filters.private & filters.command("pastesession"))
@guarded("pastesession")
async def pastesession_cmd(c, m):
    """Same as pressing the 📋 Paste Session button."""
    uid = m.from_user.id
    text = await begin_paste(uid)
    log.info("login bot: /pastesession uid=%s", uid)
    await send_screen(uid, text, method_markup())


@bot.on_message(filters.private & filters.text
                & ~filters.command(["start", "login", "generatesession", "pastesession", "guide", "help", "cancel"]))
@guarded("text")
async def text_router(c, m):
    uid = m.from_user.id
    st = STATE.get(uid)
    if is_stale(st):
        await reset(uid)
        return await send_screen(uid, txt_expired(), nav_markup())
    if not st or "step" not in st:
        if (m.text or "").startswith("/"):
            return await send_screen(uid, txt_unknown(), home_markup())
        return await send_screen(uid, txt_home(m.from_user), home_markup())
    step = st["step"]
    if step == "phone":
        return await do_phone(m, uid)
    if step == "api_id":
        return await do_api_id(m, uid)
    if step == "api_hash":
        return await do_api_hash(m, uid)
    if step == "paste":
        return await do_paste(m, uid)
    if step == "password":
        return await submit_password(m, uid)
    if step == "code":
        digits = re.sub(r"\D", "", m.text or "")
        try:
            await m.delete()
        except Exception:
            pass
        if digits:
            st["digits"] = digits[:8]
            return await submit_code(uid, st["digits"])


@bot.on_callback_query()
@guarded("callback")
async def callbacks(c, q):
    uid, data = q.from_user.id, q.data or ""
    st, lg = STATE.get(uid), LOGIN.get(uid)
    try:
        await q.answer()
    except Exception:
        pass

    if data == "close":                                    # ✖ Close deletes the message
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    if data == "help":                                     # ❓ legacy Help button
        try:
            await q.message.delete()
        except Exception:
            pass
        return await send_help(uid)

    if data == "cx":                                       # ✖ Cancel drops the login
        await reset(uid)
        return await swap_screen(q.message, uid, txt_cancelled(), nav_markup())

    if data.startswith("nav|") or data.startswith("g|"):
        page = data.split("|", 1)[1]
        if page == "home":
            await reset(uid)
            return await swap_screen(q.message, uid, txt_home(q.from_user), home_markup())
        if page == "guide":
            return await swap_screen(q.message, uid, txt_guide_menu(), guide_markup())
        builder = _topic(page)
        if builder:
            return await swap_screen(q.message, uid, builder(), topic_markup())
        return await swap_screen(q.message, uid, txt_guide_menu(), guide_markup())

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
        return await swap_screen(q.message, uid, text, method_markup())

    if not st or not lg or st.get("step") != "code":
        try:
            await q.answer("This screen is out of date", show_alert=False)
        except Exception:
            pass
        return await swap_screen(q.message, uid, txt_expired(), nav_markup())

    if data.startswith("d|") and st.get("step") == "code":
        key, digits = data[2:], st.get("digits", "")
        if key == "ok":
            if len(digits) < 4:
                return await show_code_screen(uid, "⚠️ Enter the complete code first.", rotate=True)
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
            await reader.read(2048)
            body = f"login bot ok - {LIB}".encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                         b"Cache-Control: no-store\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    log.info("health page on port %s", PORT)
    return server


MENU_COMMANDS = [
    ("start", "Open the dashboard"),
    ("login", "Choose a login method"),
    ("generatesession", "Generate a session string"),
    ("pastesession", "Check a ready session string"),
    ("guide", "Short topic guide"),
    ("help", "Full manual"),
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
    load_rotation()
    await bot.start()
    await register_commands()
    health = await health_server()
    me = await bot.get_me()
    log.info("Login bot started: @%s | library=%s | python=%s | photos=%d | %s",
             me.username, LIB, platform.python_version(), len(PHOTOS), platform.platform())
    await idle()
    if health:
        health.close()
        try:
            await health.wait_closed()
        except Exception:
            pass
    await bot.stop()


if __name__ == "__main__":
    bot.run(main())
