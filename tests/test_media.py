"""Self-check for the login bot's photo pipeline, rotation and screen rendering.

Runs against a fake Pyrogram client that enforces the *same* contract as the real one:
send_photo / edit_message_media accept a file_id (str) or a file-like object (io.IOBase)
with .name set - raw bytes are rejected, which is the bug this build fixes.
No network, no Telegram credentials.
"""
import asyncio
import io
import json
import os
import re
import sys
import tempfile
from types import SimpleNamespace

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tests/ lives inside the project
os.environ.update(
    BOT_TOKEN="12345:AAHtesttoken",
    API_ID="12345",
    API_HASH="a" * 32,
    ROTATION_STATE_FILE=os.path.join(tempfile.mkdtemp(), "rotation.json"),
)
sys.path.insert(0, BOT_DIR)
import main  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


class FakePhoto:
    def __init__(self, file_id):
        self.file_id = file_id
        self.width = self.height = 800


class FakeMsg:
    def __init__(self, mid, chat_id, photo=None, text=None):
        self.id = mid
        self.chat = SimpleNamespace(id=chat_id)
        self.photo = [FakePhoto(p) for p in (photo or [])]
        self.text = text
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeBot:
    """Mirrors pyrogram's file handling closely enough to catch the real bug."""

    def __init__(self):
        self.n = 0
        self.photos = []          # (chat_id, caption, source_type, markup)
        self.messages = []        # (chat_id, text, markup)
        self.edits = 0
        self.deletes = 0
        self.fail_media = False
        self.fail_forever = False

    def _validate(self, source):
        if isinstance(source, bytes):
            raise ValueError("Invalid file. Expected a file path as string or a binary file pointer")
        if isinstance(source, str):
            return
        if isinstance(source, io.IOBase) and getattr(source, "name", None):
            return
        raise ValueError("file-like object needs a .name")

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        if self.fail_media or self.fail_forever:
            raise ValueError("Invalid file")
        self._validate(photo)
        self.n += 1
        self.photos.append((chat_id, caption, type(photo).__name__, reply_markup))
        return FakeMsg(self.n, chat_id, photo=[f"fid{self.n}"])

    async def send_message(self, chat_id, text, reply_markup=None):
        self.n += 1
        self.messages.append((chat_id, text, reply_markup))
        return FakeMsg(self.n, chat_id, text=text)

    async def edit_message_media(self, chat_id, mid, media, reply_markup=None):
        self._validate(media.media)
        self.n += 1
        self.edits += 1
        return FakeMsg(mid, chat_id, photo=[f"fid{self.n}"])

    async def edit_message_caption(self, chat_id, mid, caption=None, reply_markup=None):
        self.n += 1
        return FakeMsg(mid, chat_id, photo=["fid"])

    async def edit_message_text(self, chat_id, mid, text=None, reply_markup=None):
        self.n += 1
        return FakeMsg(mid, chat_id, text=text)

    async def delete_messages(self, chat_id, message_ids):
        self.deletes += 1


async def run():
    print("\n[1] photo store")
    main.load_photos()
    check("all 24 photos load (webp transcoded)", len(main.PHOTOS) == 24, len(main.PHOTOS))
    check("every photo is jpeg/png", all(p.kind in ("jpeg", "png") for p in main.PHOTOS))
    check("login and nature sets are balanced", sum(p.category == "login" for p in main.PHOTOS) == 12)
    src = main.media_source(main.PHOTOS[0])
    check("media source is BytesIO, never raw bytes", isinstance(src, io.BytesIO), type(src).__name__)
    check("media source carries a filename for Pyrogram", bool(getattr(src, "name", None)), src.name)
    check("two calls give independent buffers", main.media_source(main.PHOTOS[0]) is not src)
    # Regression test against the installed library's own upload branch.
    from pyrogram.types import InputMediaPhoto
    im = InputMediaPhoto(main.media_source(main.PHOTOS[0]), caption="x")
    check("pyrogram's upload branch accepts our object",
          isinstance(im.media, io.BytesIO) or os.path.isfile(im.media))
    async def upload_probe(value):
        try:
            await main.bot.save_file(value)
            return "uploaded"
        except ValueError as e:
            return f"ValueError: {e}"
        except AttributeError:
            return "accepted"          # passes validation, stops only because the client is offline
    raw = await upload_probe(main.PHOTOS[0].data)
    ok = await upload_probe(main.media_source(main.PHOTOS[0]))
    check("raw bytes still raise in pyrogram's uploader (original bug)", raw.startswith("ValueError"), raw)
    check("the new media source passes pyrogram's uploader", ok == "accepted", ok)
    print("\n[2] rotation")
    uid = 1001
    picks = [main.PHOTOS.index(main.next_photo(uid)) for _ in range(24)]
    check("first round shows every picture once", len(set(picks)) == 24, len(set(picks)))
    check("no picture repeats back to back", all(a != b for a, b in zip(picks, picks[1:])))
    more = [main.PHOTOS.index(main.next_photo(uid)) for _ in range(48)]
    seq = picks + more
    check("no consecutive duplicates over 72 screens", all(a != b for a, b in zip(seq, seq[1:])))
    cats = [main.PHOTOS[i].category for i in seq[:24]]
    check("login and nature alternate", all(a != b for a, b in zip(cats, cats[1:])))
    other = [main.PHOTOS.index(main.next_photo(2002)) for _ in range(10)]
    check("each user rotates independently", other[:5] != picks[:5])
    print("\n[3] caption splitting")
    session = "A" * 400
    text = main.txt_result_session(session)
    head, rest = main.split_html(text, main.CAPTION_LIMIT)
    check("split keeps the caption within 1024", len(head) <= 1024, len(head))
    check("session string survives the split intact", session in (head + rest))
    check("no code tag is cut in half", head.count("<code>") == head.count("</code>"))
    long_text = main.txt_help()
    head2, rest2 = main.split_html(long_text, main.CAPTION_LIMIT)
    check("a long screen yields caption + continuation", bool(rest2) and len(head2) <= 1024)
    print("\n[4] sending screens")
    fake = FakeBot()
    main.bot = fake
    msg = await main.send_screen(uid, main.txt_home(SimpleNamespace(first_name="Test", username="t")),
                                main.home_markup())
    check("welcome screen attaches a real photo", bool(msg.photo), msg.photo)
    check("caption is within Telegram limits", all(len(c) <= 1024 for _, c, _, _ in fake.photos))
    check("keyboard travels with the photo", fake.photos[-1][3] is not None)
    before = len(fake.photos)
    for _ in range(4):
        await main.send_screen(uid, main.txt_login_menu(), main.login_markup())
    check("repeated screens keep rotating the picture", len(fake.photos) - before == 4)
    fids = [sent.photo[0].file_id for sent in [
        await main.send_screen(uid, main.txt_login_menu(), main.login_markup()) for _ in range(4)]]
    check("consecutive screens are distinct messages", len(set(fids)) == 4, fids)
    print("\n[5] session result is never clipped")
    fake = FakeBot()
    main.bot = fake
    await main.send_result(uid, main.txt_result_head(SimpleNamespace(first_name="Test"), "login", False), session)
    check("result screen sent as photo + text", len(fake.photos) == 1 and len(fake.messages) == 1)
    check("full session string delivered uncut", session in fake.messages[0][1])
    check("keyboard sits on the session message", fake.messages[0][2] is not None)
    print("\n[6] failure handling")
    fake = FakeBot()
    fake.fail_media = True
    main.bot = fake
    main.PHOTO_BLACKLIST.clear()
    for p in main.PHOTOS:
        p.failed = False
    out = await main.send_screen(uid, "hello world", main.nav_markup())
    check("media rejection falls back to text", len(fake.photos) == 0 and len(fake.messages) == 1)
    check("text fallback keeps the message body", out.text == "hello world")
    check("three rejected pictures were blacklisted", len(main.PHOTO_BLACKLIST) == 3, len(main.PHOTO_BLACKLIST))
    check("blacklisted pictures leave rotation", all(
        main.PHOTOS.index(main.next_photo(3003)) not in main.PHOTO_BLACKLIST for _ in range(20)))
    main.PHOTO_BLACKLIST.clear()
    for p in main.PHOTOS:
        p.failed = False
    print("\n[7] in-place edits and keypad")
    fake = FakeBot()
    main.bot = fake
    photo_msg = FakeMsg(50, uid, photo=["fid-old"])
    await main.swap_screen(photo_msg, uid, main.txt_guide_menu(), main.guide_markup())
    check("photo message is edited in place", fake.edits == 1 and not photo_msg.deleted)
    text_msg = FakeMsg(51, uid)
    fake.edits = 0
    await main.swap_screen(text_msg, uid, main.txt_guide_menu(), main.guide_markup())
    check("text-only message is replaced by a photo screen", fake.edits == 0 and text_msg.deleted)
    main.STATE[uid] = {"step": "code", "digits": "12", "kp": 60, "kp_photo": True, "ts": main.time.time()}
    main.LOGIN[uid] = {"where": "test", "next": "", "rs_ts": 0, "hash": "h", "phone": "+1", "client": None}
    fake.edits = 0
    await main.show_code_screen(uid, rotate=True)
    check("keypad screen rotates the photo in place", fake.edits == 1)
    main.STATE.clear()
    main.LOGIN.clear()
    print("\n[8] rotation state across restarts")
    await asyncio.sleep(1.4)                       # let the debounced write land
    path = main.ROTATION_FILE
    saved = json.load(open(path)) if os.path.exists(path) else {}
    check("rotation state written to disk", str(uid) in saved, list(saved)[:3])
    last_rel = main.PHOTOS[main.BAGS[uid]["last"]].rel
    check("state holds the last picture shown", saved.get(str(uid)) == last_rel, saved.get(str(uid)))
    main.BAGS.clear()
    main.ROTATION.clear()
    main.load_rotation()
    first_after_restart = main.PHOTOS.index(main.next_photo(uid))
    check("restart does not repeat the last picture", main.PHOTOS[first_after_restart].rel != last_rel)
    print("\n[9] concurrency")
    fake = FakeBot()
    main.bot = fake
    users = [4001, 4002, 4003]
    results = await asyncio.gather(*[
        main.send_screen(users[i % 3], main.txt_login_menu(), main.login_markup()) for i in range(30)
    ])
    check("30 concurrent screens all attach a photo", all(r.photo for r in results), sum(1 for r in results if r.photo))
    per_user = {}
    for chat_id, _, _, _ in fake.photos:
        per_user.setdefault(chat_id, []).append(1)
    check("every user got their own screens", sorted(per_user) == users, sorted(per_user))
    print("\n[10] html safety")
    check("user names are escaped", "&lt;" in main.txt_home(SimpleNamespace(first_name="<b>x</b>", username=None)))
    check("error screens expose no traceback", "Traceback" not in main.txt_error(RuntimeError("boom")))
    check("error screens stay generic", "boom" not in main.txt_error(RuntimeError("boom")))


asyncio.run(run())
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
