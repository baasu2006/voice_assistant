"""
VoiceAI - Full Stack Voice Assistant
Features:
  - Telegram MTProto (Telethon) - read and send
  - Gmail IMAP/SMTP - read and send
  - Google OAuth login
  - OpenAI GPT-4o - summarise, suggest reply, voice commands
  - Admin dashboard - user management, logs, API usage, errors
  - 3 languages: English, Hindi, Telugu
  - Voice confirmation and voice PIN
"""
import streamlit as st
import pandas as pd
import numpy
import os, sys, hashlib, secrets, asyncio, threading
import imaplib, smtplib, json, re
import email as email_lib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from datetime import datetime
from functools import wraps
from collections import defaultdict

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from authlib.integrations.flask_client import OAuth
from telethon import TelegramClient
from telethon.sessions import StringSession
from openai import OpenAI

# Windows event loop fix for Telethon
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ── .env loader ────────────────────────────────────────────────────────────────
def _load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
                v = v[1:-1]
            if k:
                os.environ[k] = v

_load_env()

def _int(k, d=0):
    try:
        return int(os.environ.get(k, "").strip())
    except Exception:
        return d

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-please")

oauth  = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ── Config ─────────────────────────────────────────────────────────────────────
TG_API_ID      = _int("TELEGRAM_API_ID")
TG_API_HASH    = os.environ.get("TELEGRAM_API_HASH",  "").strip()
TG_SESSION_STR = os.environ.get("TELEGRAM_SESSION",   "").strip()
TG_PHONE       = os.environ.get("TELEGRAM_PHONE",     "").strip()
GMAIL_ADDRESS  = os.environ.get("GMAIL_ADDRESS",      "").strip()
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY",     "").strip()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME",     "admin").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD",     "admin123").strip()

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ── In-memory stores ───────────────────────────────────────────────────────────
USERS_DB     = {}
PENDING_PINS = {}
ACTION_LOGS  = []
API_USAGE    = defaultdict(int)
ERROR_LOGS   = []
LOGIN_EVENTS = []

# Create default admin
USERS_DB[ADMIN_USERNAME] = {
    "password_hash": hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest(),
    "email":         "admin@voiceai.local",
    "role":          "admin",
    "created_at":    datetime.now().isoformat(),
    "active":        True,
}

# ── Background event loop for Telethon ────────────────────────────────────────
_BG_LOOP   = asyncio.new_event_loop()
_BG_THREAD = threading.Thread(
    target=lambda: _BG_LOOP.run_forever(),
    daemon=True
)
_BG_THREAD.start()

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _BG_LOOP).result(timeout=90)

# ── Logging ────────────────────────────────────────────────────────────────────
def log_action(action, detail="", status="success", user=None):
    ACTION_LOGS.append({
        "id":        len(ACTION_LOGS) + 1,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user":      user or session.get("username", "anonymous"),
        "action":    action,
        "detail":    str(detail)[:200],
        "status":    status,
    })
    if len(ACTION_LOGS) > 500:
        ACTION_LOGS.pop(0)

def log_error(route, error, user=None):
    ERROR_LOGS.append({
        "id":        len(ERROR_LOGS) + 1,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user":      user or session.get("username", "anonymous"),
        "route":     route,
        "error":     str(error)[:300],
    })
    if len(ERROR_LOGS) > 200:
        ERROR_LOGS.pop(0)

def log_api(name):
    API_USAGE[name] += 1

def log_login(username, method, success):
    LOGIN_EVENTS.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "username":  username,
        "method":    method,
        "success":   success,
        "ip":        request.remote_addr,
    })
    if len(LOGIN_EVENTS) > 200:
        LOGIN_EVENTS.pop(0)

# ── Auth helpers ───────────────────────────────────────────────────────────────
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if "username" not in session:
            return redirect(url_for("login"))
        return f(*a, **kw)
    return dec

def admin_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if "username" not in session:
            return redirect(url_for("login"))
        if USERS_DB.get(session["username"], {}).get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return f(*a, **kw)
    return dec

# ── OpenAI ─────────────────────────────────────────────────────────────────────
def ai_chat(system, user_msg, max_tokens=300):
    if not openai_client:
        return "OpenAI API key not configured."
    log_api("openai_chat")
    try:
        r = openai_client.chat.completions.create(
            model="gpt-4o",
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user_msg},
            ]
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        log_error("/ai_chat", e)
        return "AI error: " + str(e)

def ai_summarise(text, context="message"):
    log_api("openai_summarise")
    return ai_chat("Summarise this " + context + " in 1-2 sentences.", text, 120)

def ai_suggest_reply(text):
    log_api("openai_suggest_reply")
    return ai_chat(
        "Draft a short polite reply. Reply with ONLY the reply text, no preamble.",
        text, 150
    )

def ai_voice_command(command):
    log_api("openai_voice_command")
    lower = command.lower().strip()
    words = lower.split()

    # Extract recipient dynamically - works for any name
    recipient = ""
    trigger_words = ["to", "for", "with", "ko", "ke liye", "ki", "ku"]
    stop_words = {
        "on", "via", "in", "using", "please", "the", "a", "an",
        "telegram", "gmail", "email", "message", "mail", "my", "me",
        "par", "mera", "meri", "mere",
        "lo", "ki", "ku"
    }
    for tw in trigger_words:
        if tw in words:
            idx = words.index(tw)
            parts = []
            for w in words[idx + 1:]:
                if w in stop_words:
                    break
                parts.append(w)
            if parts:
                recipient = " ".join(parts).strip()
                break

    # Detect intent from action keywords
    is_send  = any(w in lower for w in [
        "send", "message", "msg", "ping", "text", "write", "compose", "draft", "drop",
        "bhejo", "bhejna", "sandesh", "likho", "likhna", "bhej",
        "pampu", "pampinchu", "raayu"
    ])
    is_email = any(w in lower for w in ["email", "mail", "gmail"])
    is_read  = any(w in lower for w in [
        "read", "check", "show", "open", "get", "fetch", "load", "see", "view", "inbox",
        "padho", "dekho", "dikhao", "kholo", "laao", "padh", "dekh",
        "chadavu", "chadu", "chupinchu"
    ])
    is_sum   = any(w in lower for w in [
        "summarize", "summarise", "summary", "brief", "overview", "digest", "tldr",
        "saar", "sankshipt", "mukhtasar",
        "samkshepanam"
    ])

    if is_send and is_email:
        return {
            "action":    "compose_email",
            "response":  "Sure! Let us compose an email" + (" to " + recipient if recipient else "") + ".",
            "speak":     "Composing an email" + (" to " + recipient if recipient else "") + ".",
            "recipient": recipient
        }
    if is_send:
        return {
            "action":    "compose_telegram",
            "response":  "Sure! Let us send a Telegram message" + (" to " + recipient if recipient else "") + ".",
            "speak":     "Sending a message" + (" to " + recipient if recipient else "") + ".",
            "recipient": recipient
        }
    if is_sum and is_email:
        return {"action": "summarize_gmail",    "response": "Summarizing your emails...",            "speak": "Summarizing your emails.",            "recipient": ""}
    if is_sum:
        return {"action": "summarize_telegram", "response": "Summarizing your Telegram messages...", "speak": "Summarizing your messages.",          "recipient": ""}
    if is_read and is_email:
        return {"action": "read_gmail",          "response": "Loading your Gmail inbox...",           "speak": "Loading your emails.",               "recipient": ""}
    if is_read:
        return {"action": "read_telegram",       "response": "Loading your Telegram messages...",     "speak": "Loading your Telegram messages.",    "recipient": ""}
    if any(w in lower for w in ["google", "oauth"]):
        return {"action": "login_google",        "response": "Redirecting to Google login...",        "speak": "Opening Google login.",              "recipient": ""}
    if any(w in lower for w in ["help", "commands", "options"]):
        return {"action": "help",                "response": "Here is what I can do for you!",        "speak": "Here is what I can do.",             "recipient": ""}

    # OpenAI fallback
    if not openai_client:
        return {
            "action":   "help",
            "response": "I heard: <b>" + command + "</b>. Try: Send a message to [name], Read my Telegram, Send email to [name], Read my Gmail",
            "speak":    "Try saying send a message to someone.",
            "recipient": ""
        }

    system = """You are a multilingual voice assistant for Telegram and Gmail.
User speaks English, Hindi, or Telugu.
Return ONLY valid JSON with keys:
  action: read_telegram|compose_telegram|summarize_telegram|read_gmail|compose_email|summarize_gmail|login_google|help|unknown
  response: short friendly text (1 sentence, same language as user)
  speak: very short text max 8 words
  recipient: person name if mentioned else empty string

Rules:
- Sending/messaging anyone -> compose_telegram unless email mentioned
- Sending email -> compose_email
- Reading messages -> read_telegram
- Reading email -> read_gmail
- Summarizing messages -> summarize_telegram
- Summarizing email -> summarize_gmail
- Extract recipient name for ANY name in any language
- NEVER return unknown for a clear send or read command"""

    raw = ai_chat(system, command, 200)
    raw = re.sub(r"```[a-z]*", "", raw).strip().strip("`")
    try:
        result = json.loads(raw)
        if result.get("action") == "unknown":
            if any(w in lower for w in ["send", "message", "msg"]):
                result["action"]   = "compose_telegram"
                result["response"] = "Let us send a Telegram message."
                result["speak"]    = "Let us send your message."
            elif any(w in lower for w in ["email", "mail"]):
                result["action"]   = "compose_email"
                result["response"] = "Let us compose an email."
                result["speak"]    = "Let us write your email."
        return result
    except Exception:
        if any(w in lower for w in ["send", "message", "msg"]):
            return {
                "action":    "compose_telegram",
                "response":  "Let us send a Telegram message" + (" to " + recipient if recipient else "") + ".",
                "speak":     "Let us send your message.",
                "recipient": recipient
            }
        return {
            "action":    "help",
            "response":  "I heard: <b>" + command + "</b>. Try: send message to [name] or read my telegram.",
            "speak":     "Try saying send a message to someone.",
            "recipient": ""
        }

# ── Telegram helpers ───────────────────────────────────────────────────────────
def _tg():
    s = StringSession(TG_SESSION_STR) if TG_SESSION_STR else StringSession()
    return TelegramClient(s, TG_API_ID, TG_API_HASH)

async def _tg_fetch(limit=10, contact=None):
    msgs   = []
    client = _tg()
    await client.connect()
    try:
        if contact:
            entity = await client.get_entity(contact)
            name   = getattr(entity, "first_name", None) or getattr(entity, "title", None) or contact
            async for m in client.iter_messages(entity, limit=10):
                if m.text:
                    msgs.append({
                        "id":      m.id,
                        "chat_id": entity.id,
                        "from":    name,
                        "text":    m.text,
                        "time":    m.date.strftime("%b %d %H:%M") if m.date else "",
                        "chat":    contact,
                    })
        else:
            async for dlg in client.iter_dialogs(limit=limit):
                async for m in client.iter_messages(dlg.id, limit=1):
                    if m.text:
                        msgs.append({
                            "id":      m.id,
                            "chat_id": dlg.id,
                            "from":    dlg.name or "?",
                            "text":    m.text,
                            "time":    m.date.strftime("%b %d %H:%M") if m.date else "",
                            "chat":    dlg.name or "?",
                        })
    finally:
        await client.disconnect()
    return msgs

async def _tg_resolve(recipient):
    client = _tg()
    await client.connect()
    try:
        r = recipient.strip()
        if r.lstrip("-").isdigit():
            return int(r), r
        if r.startswith("+"):
            try:
                entity = await client.get_entity(r)
                name   = (getattr(entity, "first_name", "") or "") + " " + (getattr(entity, "last_name", "") or "")
                return entity.id, name.strip()
            except Exception:
                pass
        uname = r if r.startswith("@") else "@" + r
        try:
            entity = await client.get_entity(uname)
            name   = getattr(entity, "first_name", None) or getattr(entity, "title", None) or r
            return entity.id, name
        except Exception:
            pass
        try:
            from telethon.tl.functions.contacts import GetContactsRequest
            result = await client(GetContactsRequest(hash=0))
            r_lower = r.lower()
            for u in result.users:
                fn = (u.first_name or "").lower()
                ln = (u.last_name  or "").lower()
                full = (fn + " " + ln).strip()
                if r_lower in full or fn == r_lower:
                    return u.id, u.first_name or r
        except Exception:
            pass
        return None, None
    finally:
        await client.disconnect()

async def _tg_send(chat_id, text):
    client = _tg()
    await client.connect()
    try:
        await client.send_message(chat_id, text)
    finally:
        await client.disconnect()

# ── Gmail helpers ──────────────────────────────────────────────────────────────
def _decode_hdr(v):
    parts = decode_header(v or "")
    out   = []
    for p, cs in parts:
        if isinstance(p, bytes):
            out.append(p.decode(cs or "utf-8", errors="replace"))
        else:
            out.append(str(p))
    return "".join(out)

def gmail_fetch(max_count=5, sender=None):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASS):
        raise ValueError("Gmail not configured in .env")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
    mail.select("inbox")
    if sender:
        _, data = mail.search(None, 'FROM "' + sender + '"')
    else:
        _, data = mail.search(None, "ALL")
    ids = data[0].split()
    if not ids:
        mail.logout()
        return []
    ids = ids[-max_count:][::-1]
    emails = []
    for eid in ids:
        _, md  = mail.fetch(eid, "(RFC822)")
        msg    = email_lib.message_from_bytes(md[0][1])
        subj   = _decode_hdr(msg.get("Subject", "(no subject)"))
        sndr   = _decode_hdr(msg.get("From",    "unknown"))
        body   = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                    body = part.get_payload(decode=True).decode("utf-8", "replace")
                    break
        else:
            body = msg.get_payload(decode=True).decode("utf-8", "replace")
        emails.append({
            "id":      eid.decode(),
            "from":    sndr,
            "subject": subj,
            "preview": body.strip()[:300],
            "body":    body.strip()[:2000],
            "time":    msg.get("Date", ""),
        })
    mail.logout()
    return emails

def gmail_send(to, subject, body):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASS):
        raise ValueError("Gmail not configured in .env")
    msg = MIMEMultipart()
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        s.sendmail(GMAIL_ADDRESS, to, msg.as_string())

# ── Auth routes ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return redirect(url_for("dashboard") if "username" in session else url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", google_configured=bool(os.environ.get("GOOGLE_CLIENT_ID")))
    d = request.get_json()
    u = d.get("username", "").strip()
    p = d.get("password", "")
    user = USERS_DB.get(u)
    if user and user.get("active", True) and user.get("password_hash") == hash_pw(p):
        session["username"] = u
        session["role"]     = user.get("role", "user")
        log_login(u, "password", True)
        log_action("login", "via password", user=u)
        return jsonify({"success": True, "redirect": "/dashboard"})
    log_login(u, "password", False)
    return jsonify({"success": False, "message": "Invalid username or password"})

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html", google_configured=bool(os.environ.get("GOOGLE_CLIENT_ID")))
    d  = request.get_json()
    u  = d.get("username", "").strip()
    pw = d.get("password", "")
    em = d.get("email",    "").strip()
    if not u or not pw or not em:
        return jsonify({"success": False, "message": "All fields are required"})
    if u in USERS_DB:
        return jsonify({"success": False, "message": "Username already taken"})
    if len(pw) < 6:
        return jsonify({"success": False, "message": "Password must be at least 6 characters"})
    USERS_DB[u] = {
        "password_hash": hash_pw(pw),
        "email":         em,
        "role":          "user",
        "created_at":    datetime.now().isoformat(),
        "active":        True,
    }
    session["username"] = u
    session["role"]     = "user"
    log_login(u, "signup", True)
    log_action("signup", "email=" + em, user=u)
    return jsonify({"success": True, "redirect": "/dashboard"})

@app.route("/voice-login", methods=["POST"])
def voice_login():
    d      = request.get_json()
    spoken = d.get("username", "").strip().lower()
    if any(w in spoken for w in ["google", "gmail login", "sign in with google"]):
        return jsonify({"success": False, "action": "google_oauth", "message": "Redirecting to Google..."})
    for u in USERS_DB:
        if u.lower() == spoken:
            if not USERS_DB[u].get("active", True):
                return jsonify({"success": False, "message": "Account is disabled."})
            session["username"] = u
            session["role"]     = USERS_DB[u].get("role", "user")
            log_login(u, "voice", True)
            log_action("login", "via voice", user=u)
            return jsonify({"success": True, "redirect": "/dashboard"})
    log_login(spoken, "voice", False)
    return jsonify({"success": False, "message": "No account found for '" + spoken + "'. Please sign up first."})

@app.route("/auth/google")
def google_login():
    if not os.environ.get("GOOGLE_CLIENT_ID"):
        return "Google OAuth not configured. Add GOOGLE_CLIENT_ID to .env", 400
    return google.authorize_redirect(url_for("google_callback", _external=True))

@app.route("/auth/google/callback")
def google_callback():
    try:
        token    = google.authorize_access_token()
        userinfo = token.get("userinfo") or google.userinfo()
    except Exception as e:
        log_error("/auth/google/callback", e)
        return "Google login failed: " + str(e), 400
    gid      = userinfo.get("sub")
    email    = userinfo.get("email", "")
    name     = userinfo.get("name") or userinfo.get("given_name") or email.split("@")[0]
    username = re.sub(r"[^a-z0-9_]", "_", name.lower())[:30]
    for u, info in USERS_DB.items():
        if info.get("google_id") == gid:
            username = u
            break
    else:
        if username not in USERS_DB:
            USERS_DB[username] = {
                "password_hash": "",
                "email":         email,
                "google_id":     gid,
                "name":          name,
                "role":          "user",
                "created_at":    datetime.now().isoformat(),
                "active":        True,
            }
    session["username"]     = username
    session["role"]         = USERS_DB[username].get("role", "user")
    session["google_name"]  = name
    session["google_email"] = email
    log_login(username, "google", True)
    log_action("login", "via Google OAuth", user=username)
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    u = session.get("username", "")
    log_action("logout", "", user=u)
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    u    = session["username"]
    user = USERS_DB.get(u, {})
    if user.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))
    configured = {
        "telegram":    bool(TG_API_ID and TG_API_HASH and TG_SESSION_STR),
        "gmail":       bool(GMAIL_ADDRESS and GMAIL_APP_PASS),
        "openai":      bool(OPENAI_API_KEY),
        "google_oauth":bool(os.environ.get("GOOGLE_CLIENT_ID")),
    }
    return render_template("dashboard.html",
                           username=u,
                           display_name=session.get("google_name", u),
                           configured=configured)

# ── Admin routes ───────────────────────────────────────────────────────────────
@app.route("/admin")
@admin_required
def admin_dashboard():
    return render_template("admin.html", admin_username=session["username"])

@app.route("/api/admin/stats")
@admin_required
def admin_stats():
    return jsonify({
        "total_users":    len(USERS_DB),
        "active_users":   sum(1 for u in USERS_DB.values() if u.get("active", True)),
        "admin_count":    sum(1 for u in USERS_DB.values() if u.get("role") == "admin"),
        "total_actions":  len(ACTION_LOGS),
        "total_errors":   len(ERROR_LOGS),
        "api_total":      sum(API_USAGE.values()),
        "api_breakdown":  dict(API_USAGE),
        "recent_actions": ACTION_LOGS[-20:],
        "system": {
            "telegram_ok": bool(TG_API_ID and TG_API_HASH and TG_SESSION_STR),
            "gmail_ok":    bool(GMAIL_ADDRESS and GMAIL_APP_PASS),
            "openai_ok":   bool(OPENAI_API_KEY),
            "google_ok":   bool(os.environ.get("GOOGLE_CLIENT_ID")),
        }
    })

@app.route("/api/admin/users")
@admin_required
def admin_users():
    users = []
    for username, info in USERS_DB.items():
        users.append({
            "username":     username,
            "email":        info.get("email", ""),
            "role":         info.get("role", "user"),
            "active":       info.get("active", True),
            "created_at":   info.get("created_at", ""),
            "login_method": "google" if info.get("google_id") else "password",
        })
    return jsonify({"users": users})

@app.route("/api/admin/users/<username>/toggle", methods=["POST"])
@admin_required
def admin_toggle_user(username):
    if username == session["username"]:
        return jsonify({"success": False, "message": "Cannot disable yourself"})
    if username not in USERS_DB:
        return jsonify({"success": False, "message": "User not found"})
    USERS_DB[username]["active"] = not USERS_DB[username].get("active", True)
    state = "enabled" if USERS_DB[username]["active"] else "disabled"
    log_action("admin_toggle_user", username + " -> " + state, user=session["username"])
    return jsonify({"success": True, "active": USERS_DB[username]["active"], "message": "User " + state})

@app.route("/api/admin/users/<username>/role", methods=["POST"])
@admin_required
def admin_set_role(username):
    if username not in USERS_DB:
        return jsonify({"success": False, "message": "User not found"})
    d    = request.get_json()
    role = d.get("role", "user")
    if role not in ("admin", "user"):
        return jsonify({"success": False, "message": "Role must be admin or user"})
    USERS_DB[username]["role"] = role
    log_action("admin_set_role", username + " -> " + role, user=session["username"])
    return jsonify({"success": True, "message": "Role set to " + role})

@app.route("/api/admin/users/<username>/delete", methods=["POST"])
@admin_required
def admin_delete_user(username):
    if username == session["username"]:
        return jsonify({"success": False, "message": "Cannot delete yourself"})
    if username not in USERS_DB:
        return jsonify({"success": False, "message": "User not found"})
    del USERS_DB[username]
    log_action("admin_delete_user", "deleted " + username, user=session["username"])
    return jsonify({"success": True, "message": "User deleted"})

@app.route("/api/admin/logs")
@admin_required
def admin_logs():
    page        = int(request.args.get("page",  1))
    limit       = int(request.args.get("limit", 50))
    user_filter = request.args.get("user", "").strip()
    logs        = ACTION_LOGS[::-1]
    if user_filter:
        logs = [l for l in logs if l["user"] == user_filter]
    total = len(logs)
    start = (page - 1) * limit
    return jsonify({"logs": logs[start:start + limit], "total": total, "page": page})

@app.route("/api/admin/errors")
@admin_required
def admin_errors():
    return jsonify({"errors": ERROR_LOGS[::-1][:100]})

@app.route("/api/admin/logins")
@admin_required
def admin_logins():
    return jsonify({"logins": LOGIN_EVENTS[::-1][:100]})

@app.route("/api/admin/api-usage")
@admin_required
def admin_api_usage():
    return jsonify({"usage": dict(API_USAGE), "total": sum(API_USAGE.values())})

@app.route("/api/admin/clear-errors", methods=["POST"])
@admin_required
def admin_clear_errors():
    ERROR_LOGS.clear()
    log_action("admin_clear_errors", "", user=session["username"])
    return jsonify({"success": True, "message": "Error log cleared"})

# ── User API routes ────────────────────────────────────────────────────────────
@app.route("/api/voice-command", methods=["POST"])
@login_required
def voice_command():
    d   = request.get_json()
    cmd = d.get("command", "").strip()
    log_action("voice_command", cmd)
    return jsonify(ai_voice_command(cmd))

@app.route("/api/telegram/messages")
@login_required
def get_tg_messages():
    if not (TG_API_ID and TG_API_HASH and TG_SESSION_STR):
        return jsonify({"success": False, "message": "Telegram not configured. Add TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION to .env"})
    contact = request.args.get("contact", "").strip() or None
    # 5 messages for recent overview, 10 for specific contact
    limit = 10 if contact else 5
    log_action("read_telegram", "contact=" + (contact or "all"))
    try:
        msgs = run_async(_tg_fetch(limit, contact=contact))
    except Exception as e:
        log_error("/api/telegram/messages", e)
        err_str = str(e)
        if "AUTH_KEY" in err_str or "Session" in err_str or "not authorized" in err_str.lower():
            msg = "Telegram session expired. Run generate_session.py again and update TELEGRAM_SESSION in .env"
        elif "timeout" in err_str.lower():
            msg = "Telegram connection timed out. Check your internet connection and try again."
        elif "API_ID" in err_str or "api_id" in err_str:
            msg = "Invalid Telegram API credentials. Check TELEGRAM_API_ID and TELEGRAM_API_HASH in .env"
        else:
            msg = "Telegram error: " + err_str
        return jsonify({"success": False, "message": msg})
    for m in msgs:
        m["summary"]         = ai_summarise(m["text"], "Telegram message")
        m["suggested_reply"] = ai_suggest_reply(m["text"])
    return jsonify({"success": True, "messages": msgs})

@app.route("/api/telegram/send", methods=["POST"])
@login_required
def send_tg():
    d         = request.get_json()
    recipient = d.get("recipient", "").strip()
    message   = d.get("message",  "").strip()
    pin       = d.get("pin",      "")
    stored    = PENDING_PINS.get(session["username"])
    if not stored or pin != stored:
        log_action("send_telegram", "to=" + recipient + " - wrong PIN", "error")
        return jsonify({"success": False, "message": "Wrong PIN - message NOT sent."})
    PENDING_PINS.pop(session["username"], None)
    if not (TG_API_ID and TG_API_HASH and TG_SESSION_STR):
        return jsonify({"success": False, "message": "Telegram not configured."})
    try:
        if recipient.lstrip("-").isdigit():
            chat_id  = int(recipient)
            display  = recipient
        else:
            chat_id, display = run_async(_tg_resolve(recipient))
            if chat_id is None:
                return jsonify({
                    "success": False,
                    "message": (
                        "Could not find '" + recipient + "' on Telegram. "
                        "Use @username or +phone number with country code. "
                        "You must have chatted with them before."
                    )
                })
        run_async(_tg_send(chat_id, message))
        log_action("send_telegram", "to=" + recipient)
        return jsonify({"success": True, "message": "Message sent to " + (display or recipient)})
    except Exception as e:
        log_error("/api/telegram/send", e)
        return jsonify({"success": False, "message": "Send failed: " + str(e)})

@app.route("/api/gmail/messages")
@login_required
def get_gmail():
    if not (GMAIL_ADDRESS and GMAIL_APP_PASS):
        return jsonify({"success": False, "message": "Gmail not configured. Add GMAIL_ADDRESS and GMAIL_APP_PASSWORD to .env"})
    sender = request.args.get("sender", "").strip() or None
    # 5 emails for recent overview, 10 for specific sender
    limit = 10 if sender else 5
    log_action("read_gmail", "sender=" + (sender or "all"))
    try:
        emails = gmail_fetch(limit, sender=sender)
    except Exception as e:
        log_error("/api/gmail/messages", e)
        return jsonify({"success": False, "message": "Gmail error: " + str(e)})
    for em in emails:
        txt                   = "Subject: " + em["subject"] + "\n\n" + em["body"]
        em["summary"]         = ai_summarise(txt, "email")
        em["suggested_reply"] = ai_suggest_reply(txt)
    return jsonify({"success": True, "emails": emails})

@app.route("/api/gmail/send", methods=["POST"])
@login_required
def send_gmail_route():
    d       = request.get_json()
    to      = d.get("to",      "").strip()
    subject = d.get("subject", "").strip()
    body    = d.get("body",    "").strip()
    pin     = d.get("pin",     "")
    stored  = PENDING_PINS.get(session["username"])
    if not stored or pin != stored:
        log_action("send_email", "to=" + to + " - wrong PIN", "error")
        return jsonify({"success": False, "message": "Wrong PIN - email NOT sent."})
    PENDING_PINS.pop(session["username"], None)
    if not (GMAIL_ADDRESS and GMAIL_APP_PASS):
        return jsonify({"success": False, "message": "Gmail not configured."})
    try:
        gmail_send(to, subject, body)
        log_action("send_email", "to=" + to)
        return jsonify({"success": True, "message": "Email sent to " + to})
    except Exception as e:
        log_error("/api/gmail/send", e)
        return jsonify({"success": False, "message": "Send failed: " + str(e)})

@app.route("/api/suggest-reply", methods=["POST"])
@login_required
def suggest_reply():
    d = request.get_json()
    t = d.get("text", "")
    if not t:
        return jsonify({"success": False, "message": "No text provided"})
    log_action("ai_suggest_reply", t[:60])
    return jsonify({"success": True, "suggestion": ai_suggest_reply(t)})

@app.route("/api/generate-pin", methods=["POST"])
@login_required
def generate_pin():
    pin = str(secrets.randbelow(9000) + 1000)
    PENDING_PINS[session["username"]] = pin
    log_action("generate_pin", "")
    return jsonify({"success": True, "pin": pin})

@app.route("/api/status")
def api_status():
    return jsonify({
        "telegram":     bool(TG_API_ID and TG_API_HASH and TG_SESSION_STR),
        "gmail":        bool(GMAIL_ADDRESS and GMAIL_APP_PASS),
        "openai":       bool(OPENAI_API_KEY),
        "google_oauth": bool(os.environ.get("GOOGLE_CLIENT_ID")),
    })

if __name__ == "__main__":
    print("VoiceAI starting - http://localhost:5000")
    print("Admin login: " + ADMIN_USERNAME + " / " + ADMIN_PASSWORD)
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
