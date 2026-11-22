LOGIN TEST BOT (only the login)
================================
Purpose: find out why the login code does not arrive. It does nothing else.
The bot shows the library and Python version in /start and writes every step to the log.

1) MAKE A NEW TEST BOT in @BotFather (revoke its token afterwards). Get API ID / API Hash from my.telegram.org.

2) DEPLOY (Render OR Railway, the same files)
   Render : New Web Service -> this repo -> it reads render.yaml. Environment: BOT_TOKEN, API_ID, API_HASH.
   Railway: New project -> this repo -> Variables: BOT_TOKEN, API_ID, API_HASH. Start command: python main.py (Procfile is included).

3) THE LIBRARY = ONE LINE in requirements.txt. Test them one after another, always the same number and API ID:
   A) pyrofork==2.3.69   (the one on the phone that received the code: Pyrogram 2.3.69, Python 3.12.6)   <- requirements.txt now
   B) pyrogram==2.0.106
   C) kurigram==2.2.26
   Copy the content of requirements_pyrofork_2.3.69.txt / requirements_pyrogram_2.0.106.txt / requirements_kurigram_2.2.26.txt
   into requirements.txt, then deploy (Render: 'Clear build cache & deploy'). Never install two of them together.
   Render: PYTHON_VERSION is 3.12.6 in render.yaml. Railway: use Python 3.12 if you can choose.

4) TEST: send /start, then your number. Look at the log line 'login: code requested ... delivery=?? next=??'
   and tell me it for each library (and whether the code really arrived).

NOT TESTED ON REAL TELEGRAM: pyrofork / pyrogram / kurigram could not be installed where this was made, so the bot was
only run against a fake library (16 checks, all pass). If it crashes at start, send me the first error line.
