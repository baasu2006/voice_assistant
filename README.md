# 🎙️ VoiceAI – Multilingual Voice Assistant

A full-stack Flask voice assistant for Telegram & Gmail with GPT-4o, Google OAuth, and admin dashboard.

## Features
- 🎤 Voice commands in **English, Hindi, Telugu**
- ✈️ **Telegram** – read & send messages via MTProto
- 📧 **Gmail** – read & send emails via IMAP/SMTP
- 🤖 **GPT-4o** – AI summaries & suggested replies
- 🔐 **Voice PIN** confirmation before sending anything
- 🔑 **Google OAuth** login
- 👁 **Admin dashboard** – users, logs, errors, API usage

## Quick Start

### 1. Configure `.env`
```bash
cp .env.example .env
# Edit .env with your real credentials
```

### 2. Generate Telegram session (once)
```bash
pip install telethon
python generate_session.py
# Paste output TELEGRAM_SESSION= into .env
```

### 3. Run with Docker
```bash
docker-compose up --build
# Open http://localhost:5000
```

### 3b. Run without Docker
```bash
pip install -r requirements.txt
python app.py
```

## Microphone Fix
- ✅ Use **Chrome** browser (Firefox/Safari have limited support)
- ✅ Access via `http://localhost:5000` (not via IP address)
- ✅ Click 🔒 in address bar → Allow microphone when prompted

## Default Admin Login
- URL: `http://localhost:5000/admin`
- Username: `admin`
- Password: `admin123`
- ⚠️ Change in `.env` before deploying!

## Voice Commands
| English | Hindi | Telugu |
|---------|-------|--------|
| Read my Telegram | Telegram padho | Telegram chadavu |
| Send message to [name] | [name] ko message bhejo | [name] ku message pampinchu |
| Read my Gmail | Gmail dekho | Gmail chadavu |
| Send email to [name] | [name] ko email bhejo | [name] ku email pampinchu |
| Summarize my messages | Messages ka saar do | Messages samkshepanam |
