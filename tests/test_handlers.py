"""Handler-level QA for the login bot: commands, callbacks, flows, dead-button audit.

Drives the real handler functions with fake Telegram updates. No network: Pyrogram clients
and the code request are replaced with fakes, so the whole state machine runs end to end.
"""
import asyncio
import os
import sys
import tempfile
import time
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
from pyrogram import errors as tg_errors  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"   [{detail}]" if detail else ""))


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.first_name = "Tester"
        self.username = "tester"


class FakeMessage:
    def __init__(self, uid, text=None, photo=False, mid=1):
        self.from_user = FakeUser(uid)
        self.chat = SimpleNamespace(id=uid)
        self.text = text
        self.id = mid
        self.command = None
        self.deleted = False
        self.photo = [SimpleNamespace(file_id="fid", width=800, height=800)] if photo else []

    async def delete(self):
        self.deleted = True


class FakeCallback:
    def __init__(self, uid, data, message):
        self.from_user = FakeUser(uid)
        self.data = data
        self.message = message
        self.answered = 0

    async def answer(self, *a, **k):
        self.answered += 1


class FakeBot:
    def __init__(self):
        self.n = 0
        self.events = []            # (kind, text)

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        self.n += 1
        self.events.append(("photo", caption or ""))
        return FakeMessage(chat_id, photo=True, mid=self.n)

    async def send_message(self, chat_id, text, reply_markup=None):
        self.n += 1
        self.events.append(("text", text or ""))
        return FakeMessage(chat_id, text=text, mid=self.n)

    async def edit_message_media(self, chat_id, mid, media, reply_markup=None):
        self.n += 1
        self.events.append(("edit_media", media.caption or ""))
        return FakeMessage(chat_id, photo=True, mid=mid)

    async def edit_message_caption(self, chat_id, mid, caption=None, reply_markup=None):
        self.n += 1
        self.events.append(("edit_caption", caption or ""))
        return FakeMessage(chat_id, photo=True, mid=mid)

    async def edit_message_text(self, chat_id, mid, text=None, reply_markup=None):
        self.n += 1
        self.events.append(("edit_text", text or ""))
        return FakeMessage(chat_id, text=text, mid=mid)

    async def delete_messages(self, chat_id, ids):
        self.events.append(("delete", str(ids)))

    def screens(self):
        return [t for _, t in self.events if t]

    def last(self):
        return self.screens()[-1] if self.events else ""

    def joined(self):
        return "\n".join(self.screens())


class FakeLoginClient:
    """Stands in for the user client: code 12345 works, 99999 does not."""
    need_password = False

    def __init__(self, **kw):
        self.kw = kw

    async def connect(self):
        pass

    async def send_code(self, phone):
        return SimpleNamespace(phone_code_hash="hash1", type=SimpleNamespace(name="APP"),
                               next_type=SimpleNamespace(name="SMS"), timeout=60)

    async def resend_code(self, phone, h):
        return await self.send_code(phone)

    async def sign_in(self, phone, h, code):
        if self.need_password:
            raise tg_errors.SessionPasswordNeeded()
        if code != "12345":
            raise tg_errors.PhoneCodeInvalid()
        return SimpleNamespace(id=777)

    async def check_password(self, pwd):
        if pwd != "secret":
            raise tg_errors.PasswordHashInvalid()
        return SimpleNamespace(id=777)

    async def get_me(self):
        return SimpleNamespace(id=777, first_name="Tester", username="tester", is_bot=False)

    async def export_session_string(self):
        return "SESSION" + "x" * 300

    async def disconnect(self):
        pass


class FakeUserClient(FakeLoginClient):
    """Stands in for the client built from a pasted session string."""
    is_bot = False

    async def start(self):
        pass

    async def stop(self):
        pass

    async def get_me(self):
        return SimpleNamespace(id=778, first_name="Pasted", username="pasted", is_bot=self.is_bot)


def fresh(uid):
    main.STATE.pop(uid, None)
    main.LOGIN.pop(uid, None)


async def cmd(fn, uid, command, text=None):
    m = FakeMessage(uid, text=text)
    m.command = [command]
    await fn(None, m)
    return m


async def tap(uid, data, message=None):
    q = FakeCallback(uid, data, message or FakeMessage(uid, photo=True))
    await main.callbacks(None, q)
    return q


async def type_text(uid, text):
    m = FakeMessage(uid, text=text)
    await main.text_router(None, m)
    return m


def clean(bot):
    return "SOMETHING WENT WRONG" not in bot.joined() and "Traceback" not in bot.joined()


async def run():
    main.load_photos()
    main.load_rotation()
    bot = FakeBot()
    main.bot = bot

    print("\n[1] command registration")
    handlers = {"start": main.start_cmd, "login": main.start_cmd, "guide": main.guide_cmd,
                "help": main.help_cmd, "cancel": main.cancel_cmd,
                "generatesession": main.generatesession_cmd, "pastesession": main.pastesession_cmd}
    missing = [n for n, _ in main.MENU_COMMANDS if n not in handlers]
    check("every registered command has a handler", not missing, missing)
    check("no extra command in the menu", len(main.MENU_COMMANDS) == len(handlers))

    print("\n[2] dashboard and commands")
    uid = 7001
    fresh(uid)
    await cmd(main.start_cmd, uid, "start")
    check("/start sends a dashboard with a photo", bot.events[-1][0] == "photo")
    check("/start greets the user by name", "Tester" in bot.last())
    check("/start lists the four methods", all(k in bot.last() for k in
                                               ("Phone + OTP", "API ID", "Generate", "Paste")))
    uid = 7002
    fresh(uid)
    await cmd(main.start_cmd, uid, "login")
    check("/login opens the method list", "CONNECT YOUR TELEGRAM ACCOUNT" in bot.last())
    uid = 7003
    fresh(uid)
    await cmd(main.guide_cmd, uid, "guide")
    check("/guide opens the topic menu", "GUIDE" in bot.last() and "Troubleshooting" in bot.last())
    uid = 7004
    fresh(uid)
    await cmd(main.help_cmd, uid, "help")
    check("/help sends photo heading + manual", len(bot.events) >= 2 and "FULL MANUAL" in bot.last())
    uid = 7005
    fresh(uid)
    await cmd(main.cancel_cmd, uid, "cancel")
    check("/cancel confirms the cancellation", "CANCELLED" in bot.last())

    print("\n[3] guide topics (every button works)")
    for key, needle in (("how", "HOW IT WORKS"), ("code", "CODE & KEYPAD"), ("trouble", "TROUBLESHOOTING"),
                        ("safety", "SAFETY"), ("about", "ABOUT")):
        await tap(7003, f"g|{key}")
        check(f"guide topic {key} renders", needle in bot.last())
    await tap(7003, "nav|guide")
    check("guide '‹ Guide' returns to the topic list", "Everything this bot does" in bot.last())
    await tap(7003, "nav|home")
    check("guide 'Home' returns to the dashboard", "LOGIN BOT" in bot.last())
    check("every guide action stayed clean", clean(bot))

    print("\n[4] phone + OTP flow")
    uid = 7100
    fresh(uid)
    main.open_login = lambda phone, api=None: _fake_open(phone, api)
    await cmd(main.start_cmd, uid, "start")
    await tap(uid, "m|ph")
    check("method button opens the phone screen", "PHONE + OTP LOGIN" in bot.last())
    check("phone screen explains what is needed", "You'll need" in bot.last())
    await type_text(uid, "not-a-number")
    check("invalid phone is rejected with guidance", "INVALID PHONE NUMBER" in bot.last())
    await type_text(uid, "+15551234567")
    check("valid phone reaches the code screen", "ENTER THE CODE" in bot.last())
    check("code screen shows the delivery method", "inside Telegram" in bot.last())
    check("code screen carries the keypad", "Code:" in bot.last())
    for digit in "1234":
        await tap(uid, f"d|{digit}")
    check("keypad digits update the code line", "1 2 3 4" in bot.last())
    await tap(uid, "d|del")
    check("backspace removes a digit", "1 2 3" in bot.last())
    await tap(uid, "d|ok")
    check("short code is refused before submitting", "complete code" in bot.last())
    for digit in "45":
        await tap(uid, f"d|{digit}")
    await tap(uid, "d|ok")
    check("correct code completes the login", "LOGIN WORKED" in bot.joined())
    check("session string delivered in its own message", "SESSION" in bot.joined())
    check("phone flow stayed clean", clean(bot))

    print("\n[5] wrong code and resend")
    uid = 7200
    fresh(uid)
    await cmd(main.start_cmd, uid, "start")
    await tap(uid, "m|ph")
    await type_text(uid, "+15551234567")
    for digit in "99999":
        await tap(uid, f"d|{digit}")
    await tap(uid, "d|ok")
    check("wrong code reported, login kept alive", "Incorrect code" in bot.last() and uid in main.LOGIN)
    await tap(uid, "rs")
    check("new code is requested and confirmed", "New code requested" in bot.last())
    await tap(uid, "rs")
    check("resend is throttled for 15 seconds", "wait a few seconds" in bot.last())
    check("resend flow stayed clean", clean(bot))

    print("\n[6] two-step password")
    uid = 7300
    fresh(uid)

    async def open_needing_password(phone, api=None):
        c = FakeLoginClient()
        c.need_password = True
        return _resolved(c)

    main.open_login = open_needing_password
    await cmd(main.start_cmd, uid, "start")
    await tap(uid, "m|ph")
    await type_text(uid, "+15551234567")
    for digit in "12345":
        await tap(uid, f"d|{digit}")
    await tap(uid, "d|ok")
    check("2-step account asks for the password", "TWO-STEP VERIFICATION" in bot.last())
    await type_text(uid, "wrong")
    check("wrong password is reported", "WRONG PASSWORD" in bot.last())
    await type_text(uid, "secret")
    check("correct password finishes the login", "LOGIN WORKED" in bot.joined())
    check("password flow stayed clean", clean(bot))

    print("\n[7] own API key flow")
    uid = 7400
    fresh(uid)
    main.open_login = lambda phone, api=None: _fake_open(phone, api)
    await cmd(main.start_cmd, uid, "start")
    await tap(uid, "m|own")
    check("API method explains the key source", "my.telegram.org" in bot.last())
    await type_text(uid, "abc")
    check("non-numeric API ID rejected", "INVALID API ID" in bot.last())
    await type_text(uid, "1234567")
    check("API ID accepted, hash requested", "API Hash" in bot.last())
    await type_text(uid, "short")
    check("bad API hash rejected", "INVALID API HASH" in bot.last())
    await type_text(uid, "a" * 32)
    check("hash accepted, phone requested", "phone number" in bot.last().lower())
    await type_text(uid, "+15551234567")
    check("own-key login reaches the code screen", "ENTER THE CODE" in bot.last())
    check("own-key flow stayed clean", clean(bot))

    print("\n[8] paste session flow")
    uid = 7500
    fresh(uid)
    main.Client = lambda *a, **kw: FakeUserClient(**kw)
    await cmd(main.start_cmd, uid, "start")
    await tap(uid, "m|paste")
    check("paste intro explains the handling", "deleted at once" in bot.last())
    await type_text(uid, "too-short")
    check("short session string rejected", "SESSION NOT ACCEPTED" in bot.last())
    for _ in range(4):
        await type_text(uid, "still-short")
    check("five bad attempts end the flow", "TOO MANY ATTEMPTS" in bot.last())
    fresh(uid)
    await tap(uid, "m|paste")
    await type_text(uid, "A" * 200)
    check("valid session string verified", "SESSION VERIFIED" in bot.last())

    class BotSessionClient(FakeUserClient):
        is_bot = True

    main.Client = lambda *a, **kw: BotSessionClient(**kw)
    fresh(uid)
    await tap(uid, "m|paste")
    await type_text(uid, "B" * 200)
    check("bot session strings are refused", "bot session" in bot.last())
    check("paste flow stayed clean", clean(bot))

    print("\n[9] navigation, close and state hygiene")
    uid = 7600
    fresh(uid)
    await cmd(main.start_cmd, uid, "start")
    await tap(uid, "m|own")
    main.STATE[uid]["api_id"] = 1234567
    await tap(uid, "nav|home")
    check("Home clears the conversation state", uid not in main.STATE)
    msg = FakeMessage(uid, photo=True)
    q = FakeCallback(uid, "close", msg)
    await main.callbacks(None, q)
    check("Close deletes the message", msg.deleted)
    check("Close answers the callback", q.answered == 1)
    await cmd(main.start_cmd, uid, "start")
    await tap(uid, "cx")
    check("Cancel shows the cancelled screen", "CANCELLED" in bot.last())
    await type_text(uid, "/nonsense")
    check("unknown command handled politely", "UNKNOWN COMMAND" in bot.last())
    main.STATE[uid] = {"step": "phone", "mode": "login", "ts": time.time() - main.LOGIN_TTL - 5}
    await type_text(uid, "+15551234567")
    check("stale conversation expires instead of hanging", "SESSION EXPIRED" in bot.last() and uid not in main.STATE)
    check("navigation stayed clean", clean(bot))

    print("\n[10] dead-button audit")
    main.LOGIN[999] = {"where": "", "next": "", "rs_ts": 0, "hash": "h", "phone": "+1", "client": FakeLoginClient()}
    keys = []
    for mk in (main.home_markup(), main.login_markup(), main.guide_markup(), main.topic_markup(),
               main.nav_markup(), main.method_markup(), main.keypad(main.LOGIN[999])):
        for row in mk.inline_keyboard:
            for b in row:
                keys.append(b.callback_data)
    check("all markups expose callback data", all(isinstance(k, str) and k for k in keys), keys[:3])
    dups = []
    for name, mk in (("home", main.home_markup()), ("login", main.login_markup()),
                     ("guide", main.guide_markup()), ("topic", main.topic_markup()),
                     ("nav", main.nav_markup()), ("method", main.method_markup()),
                     ("keypad", main.keypad(main.LOGIN[999]))):
        data = [b.callback_data for row in mk.inline_keyboard for b in row]
        if len(set(data)) != len(data):
            dups.append(name)
    check("no duplicate callback data inside a markup", not dups, dups)
    known_prefix = ("m|", "nav|", "g|", "d|")
    check("every callback uses a known scheme",
          all(k in ("close", "cx", "rs") or k.startswith(known_prefix) for k in keys),
          [k for k in keys if not (k in ("close", "cx", "rs") or k.startswith(known_prefix))])
    main.LOGIN.pop(999, None)

    print("\n[11] every button, pressed for real")
    uid = 7700
    failed = []
    for data in sorted(set(keys)):
        fresh(uid)
        main.STATE[uid] = {"step": "code", "digits": "12345", "kp": 10, "kp_photo": True, "ts": time.time()}
        main.LOGIN[uid] = {"client": FakeLoginClient(), "phone": "+1", "hash": "h", "mode": "login",
                           "api": None, "next": "", "where": "test", "tries": 0, "rs_ts": 0}
        before = len(bot.events)
        msg = FakeMessage(uid, photo=True)
        try:
            q = FakeCallback(uid, data, msg)
            await main.callbacks(None, q)
            if data == "close":
                acted = msg.deleted          # close is meant to delete, not to send
            else:
                acted = len(bot.events) > before
            crashed = "SOMETHING WENT WRONG" in bot.joined()
            if not acted or crashed or q.answered != 1:
                failed.append(data)
        except Exception as e:
            failed.append(f"{data}:{type(e).__name__}")
    check("every button performs an action", not failed, failed)


async def _fake_open(phone, api=None):
    return FakeLoginClient(), SimpleNamespace(phone_code_hash="hash1", type=SimpleNamespace(name="APP"),
                                             next_type=SimpleNamespace(name="SMS"), timeout=60)


def _resolved(client):
    return client, SimpleNamespace(phone_code_hash="hash1", type=SimpleNamespace(name="APP"),
                                   next_type=SimpleNamespace(name="SMS"), timeout=60)


asyncio.run(run())
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
