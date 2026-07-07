import os
import logging
import sqlite3
import csv
import io
import asyncio
import re
import json
import hashlib
import difflibimport os
import logging
import sqlite3
import csv
import io
import asyncio
import re
import json
import hashlib
import difflib
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from openai import OpenAI

# ======================= تحميل المتغيرات البيئية =======================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

if not TELEGRAM_TOKEN or not GROQ_API_KEY or not GOOGLE_API_KEY:
    raise ValueError("❌ تأكد من وجود TELEGRAM_BOT_TOKEN و GROQ_API_KEY و GOOGLE_API_KEY في ملف .env")

if ADMIN_ID == 0:
    print("⚠️ تحذير: ADMIN_ID غير مضبوط. لن تعمل أوامر /broadcast و /stats و /top و /users و /export و /addadmin.")

# ======================= إعداد العملاء =======================
client_groq = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
client_gemini = OpenAI(api_key=GOOGLE_API_KEY, base_url="https://generativelanguage.googleapis.com/v1beta/openai/")

client_openrouter = None
if OPENROUTER_API_KEY:
    client_openrouter = OpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ======================= دوال تطبيع النص العربي =======================
def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    text = text.replace("إ", "ا").replace("أ", "ا").replace("آ", "ا").replace("ى", "ا")
    text = text.replace("ة", "ه")
    text = re.sub(r'[\u064B-\u0652]', '', text)
    text = re.sub(r'[،؛؟!()\[\]{}"\'.,;:?!\-]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ======================= دوال التصحيح الإملائي البسيط =======================
SPELLING_CORRECTIONS = {
    "افراغ": "إفراغ",
    "ايجار": "إيجار",
    "سكني": "سكني",
    "تجاري": "تجاري",
    "تسجيل": "تسجيل",
    "وساطة": "وساطة",
    "مزاد": "مزاد",
    "ترخيص": "ترخيص",
    "مطالبة": "مطالبة",
    "عربون": "عربون",
    "انهاء": "إنهاء",
}

def correct_spelling(text: str) -> str:
    words = text.split()
    corrected = []
    for word in words:
        if word in SPELLING_CORRECTIONS:
            corrected.append(SPELLING_CORRECTIONS[word])
        else:
            found = False
            for misspelled, correct in SPELLING_CORRECTIONS.items():
                if misspelled in word or word in misspelled:
                    corrected.append(correct)
                    found = True
                    break
            if not found:
                corrected.append(word)
    return " ".join(corrected)

# ======================= قاعدة البيانات =======================
DB_PATH = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_activity TEXT,
        total_messages INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS questions (
        question_text TEXT PRIMARY KEY,
        count INTEGER DEFAULT 1,
        last_asked TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS keywords (
        keyword TEXT PRIMARY KEY,
        count INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS rejections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_text TEXT,
        timestamp TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS conversation_context (
        user_id INTEGER,
        session_id TEXT,
        last_question TEXT,
        last_suggestion TEXT,
        last_question_time TEXT,
        clarification_stage TEXT DEFAULT 'menu',
        classification TEXT DEFAULT '',
        youtube_links TEXT DEFAULT '',
        PRIMARY KEY (user_id, session_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        secret_code TEXT,
        added_by INTEGER,
        added_date TEXT,
        role TEXT DEFAULT 'admin'
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS custom_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_name TEXT UNIQUE,
        rule_text TEXT,
        created_by INTEGER,
        created_date TEXT,
        is_active INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS qa_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_normalized TEXT UNIQUE,
        question_original TEXT,
        answer TEXT,
        source TEXT,
        created_at TEXT,
        last_used TEXT,
        usage_count INTEGER DEFAULT 1,
        expiry_date TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        user_id INTEGER,
        start_time TEXT,
        end_time TEXT,
        is_active INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_preferences (
        user_id INTEGER PRIMARY KEY,
        city TEXT,
        property_type TEXT,
        price_range TEXT,
        preferred_contact TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS pending_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        data TEXT,
        timestamp TEXT,
        secret_code TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        details TEXT,
        timestamp TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS saved_responses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        question TEXT,
        answer TEXT,
        saved_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS faq (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_normalized TEXT UNIQUE,
        question_original TEXT,
        answer TEXT,
        source TEXT,
        created_at TEXT
    )''')
    
    c.execute("CREATE INDEX IF NOT EXISTS idx_context_user_session ON conversation_context (user_id, session_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log (user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cache_normalized ON qa_cache (question_normalized)")
    
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect(DB_PATH)

# ======================= دوال قاعدة البيانات المحدثة =======================
def save_user(user_id, username, first_name):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT OR IGNORE INTO users (user_id, username, first_name, last_activity, total_messages)
                 VALUES (?, ?, ?, ?, 0)''', (user_id, username, first_name, now))
    c.execute('''UPDATE users SET last_activity = ?, total_messages = total_messages + 1
                 WHERE user_id = ?''', (now, user_id))
    conn.commit()
    conn.close()

def get_last_activity(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT last_activity FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def update_last_activity(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("UPDATE users SET last_activity = ? WHERE user_id = ?", (now, user_id))
    conn.commit()
    conn.close()

def save_question(question_text):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT INTO questions (question_text, count, last_asked)
                 VALUES (?, 1, ?) ON CONFLICT(question_text) DO UPDATE SET
                 count = count + 1, last_asked = excluded.last_asked''', (question_text, now))
    conn.commit()
    conn.close()

def save_keywords(keywords_list):
    conn = get_db_connection()
    c = conn.cursor()
    for kw in keywords_list:
        if len(kw) < 2:
            continue
        c.execute('''INSERT INTO keywords (keyword, count) VALUES (?, 1)
                     ON CONFLICT(keyword) DO UPDATE SET count = count + 1''', (kw,))
    conn.commit()
    conn.close()

def save_rejection(question_text):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT INTO rejections (question_text, timestamp) VALUES (?, ?)''', (question_text, now))
    conn.commit()
    conn.close()

def save_context(user_id, session_id, last_question, last_suggestion, clarification_stage="menu", classification="", youtube_links=""):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    if isinstance(youtube_links, (list, dict)):
        youtube_links = json.dumps(youtube_links, ensure_ascii=False)
    c.execute('''INSERT OR REPLACE INTO conversation_context 
                 (user_id, session_id, last_question, last_suggestion, last_question_time, clarification_stage, classification, youtube_links)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', 
                 (user_id, session_id, last_question, last_suggestion, now, clarification_stage, classification, youtube_links))
    conn.commit()
    conn.close()

def get_context(user_id, session_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT last_question, last_suggestion, last_question_time, clarification_stage, classification, youtube_links 
                 FROM conversation_context
                 WHERE user_id = ? AND session_id = ?''', (user_id, session_id))
    row = c.fetchone()
    conn.close()
    if row:
        youtube_links = row[5] if row[5] else ""
        try:
            youtube_links_parsed = json.loads(youtube_links)
        except:
            youtube_links_parsed = []
        return {
            "last_question": row[0],
            "last_suggestion": row[1],
            "last_question_time": row[2],
            "clarification_stage": row[3] if row[3] else "menu",
            "classification": row[4] if row[4] else "",
            "youtube_links": youtube_links_parsed
        }
    return None

def clear_context(user_id, session_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''DELETE FROM conversation_context WHERE user_id = ? AND session_id = ?''', (user_id, session_id))
    conn.commit()
    conn.close()

def update_clarification_stage(user_id, session_id, stage):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''UPDATE conversation_context SET clarification_stage = ? 
                 WHERE user_id = ? AND session_id = ?''', (stage, user_id, session_id))
    conn.commit()
    conn.close()

# ======================= دوال الإدارة والأمان المحدثة =======================
def is_admin(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, role FROM admins WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return True, row[1] if len(row) > 1 else "admin"
    return False, None

def get_admin_secret(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT secret_code FROM admins WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_admin_role(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT role FROM admins WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_admin_role(user_id, role):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE admins SET role = ? WHERE user_id = ?", (role, user_id))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)''', (key, value))
    conn.commit()
    conn.close()

def delete_setting(key):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM bot_settings WHERE key = ?", (key,))
    conn.commit()
    conn.close()

def get_all_admins():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, username, secret_code, added_by, added_date, role FROM admins")
    rows = c.fetchall()
    conn.close()
    return rows

# ======================= دوال القواعد المتعددة =======================
def add_custom_rule(rule_name, rule_text, created_by):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT OR REPLACE INTO custom_rules (rule_name, rule_text, created_by, created_date, is_active)
                 VALUES (?, ?, ?, ?, 0)''', (rule_name, rule_text, created_by, now))
    conn.commit()
    conn.close()

def get_all_custom_rules():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, rule_name, rule_text, created_by, created_date, is_active FROM custom_rules")
    rows = c.fetchall()
    conn.close()
    return rows

def get_custom_rule(rule_name):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT rule_text FROM custom_rules WHERE rule_name = ?", (rule_name,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def update_custom_rule(rule_name, new_text):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE custom_rules SET rule_text = ? WHERE rule_name = ?", (new_text, rule_name))
    conn.commit()
    conn.close()

def delete_custom_rule(rule_name):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM custom_rules WHERE rule_name = ?", (rule_name,))
    conn.commit()
    conn.close()

def delete_all_custom_rules():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM custom_rules")
    conn.commit()
    conn.close()

def activate_rule(rule_name):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE custom_rules SET is_active = 0")
    c.execute("UPDATE custom_rules SET is_active = 1 WHERE rule_name = ?", (rule_name,))
    conn.commit()
    conn.close()

def get_active_rule():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT rule_text FROM custom_rules WHERE is_active = 1")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

# ======================= دوال Q&A Cache مع التشابه الدلالي =======================
def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s]', '', text)
    return text

def get_cached_answer(question):
    conn = get_db_connection()
    c = conn.cursor()
    norm_q = normalize_text(question)
    c.execute("SELECT answer, question_original, expiry_date FROM qa_cache WHERE question_normalized = ?", (norm_q,))
    row = c.fetchone()
    conn.close()
    if row:
        if row[2]:
            expiry = datetime.fromisoformat(row[2])
            if datetime.now() > expiry:
                return None
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE qa_cache SET usage_count = usage_count + 1, last_used = ? WHERE question_normalized = ?",
                  (datetime.now().isoformat(), norm_q))
        conn.commit()
        conn.close()
        return row[0]
    return None

def get_semantic_cached_answer(question, threshold=0.85):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT question_normalized, question_original, answer, expiry_date FROM qa_cache")
    rows = c.fetchall()
    conn.close()
    norm_q = normalize_text(question)
    best_match = None
    best_ratio = 0.0
    for row in rows:
        stored_norm = normalize_text(row[0])
        ratio = difflib.SequenceMatcher(None, norm_q, stored_norm).ratio()
        if ratio > best_ratio and ratio >= threshold:
            best_ratio = ratio
            best_match = row
    if best_match:
        if best_match[3]:
            expiry = datetime.fromisoformat(best_match[3])
            if datetime.now() > expiry:
                return None
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE qa_cache SET usage_count = usage_count + 1, last_used = ? WHERE question_normalized = ?",
                  (datetime.now().isoformat(), best_match[0]))
        conn.commit()
        conn.close()
        return best_match[2]
    return None

def save_cached_answer(question, answer, source="المصادر الرسمية", expiry_days=30):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    expiry = (datetime.now() + timedelta(days=expiry_days)).isoformat()
    c.execute('''INSERT OR REPLACE INTO qa_cache (question_normalized, question_original, answer, source, created_at, last_used, expiry_date)
                 VALUES (?, ?, ?, ?, ?, ?, ?)''', (normalize_text(question), question, answer, source, now, now, expiry))
    conn.commit()
    conn.close()

def add_faq(question, answer, source="المصادر الرسمية"):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT OR IGNORE INTO faq (question_normalized, question_original, answer, source, created_at)
                 VALUES (?, ?, ?, ?, ?)''', (normalize_text(question), question, answer, source, now))
    conn.commit()
    conn.close()

def get_faq_answer(question):
    conn = get_db_connection()
    c = conn.cursor()
    norm_q = normalize_text(question)
    c.execute("SELECT answer FROM faq WHERE question_normalized = ?", (norm_q,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT question_normalized, answer FROM faq")
    rows = c.fetchall()
    conn.close()
    best_match = None
    best_ratio = 0.0
    for r in rows:
        ratio = difflib.SequenceMatcher(None, norm_q, r[0]).ratio()
        if ratio > best_ratio and ratio >= 0.85:
            best_ratio = ratio
            best_match = r[1]
    return best_match

# ======================= دوال الإحصائيات =======================
def get_stats():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    c.execute("SELECT COUNT(*) FROM users WHERE last_activity > ?", (week_ago,))
    active_week = c.fetchone()[0]
    five_min_ago = (datetime.now() - timedelta(minutes=5)).isoformat()
    c.execute("SELECT COUNT(*) FROM users WHERE last_activity > ?", (five_min_ago,))
    active_now = c.fetchone()[0]
    c.execute("SELECT question_text, count FROM questions ORDER BY count DESC LIMIT 5")
    top_questions = c.fetchall()
    c.execute("SELECT COUNT(*) FROM rejections")
    total_rejections = c.fetchone()[0]
    c.execute("SELECT SUM(total_messages) FROM users")
    total_messages = c.fetchone()[0] or 0
    conn.close()
    rejection_rate = (total_rejections / total_messages * 100) if total_messages > 0 else 0
    return {
        "total_users": total_users,
        "active_week": active_week,
        "active_now": active_now,
        "top_questions": top_questions,
        "total_rejections": total_rejections,
        "rejection_rate": round(rejection_rate, 2),
        "total_messages": total_messages
    }

def get_top_keywords(limit=10):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT keyword, count FROM keywords ORDER BY count DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_users():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, last_activity, total_messages FROM users")
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_questions():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT question_text, count, last_asked FROM questions ORDER BY count DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_rejections():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT question_text, timestamp FROM rejections ORDER BY timestamp DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def save_saved_response(user_id, question, answer):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT INTO saved_responses (user_id, question, answer, saved_at)
                 VALUES (?, ?, ?, ?)''', (user_id, question, answer, now))
    conn.commit()
    conn.close()

def get_saved_responses(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, question, answer, saved_at FROM saved_responses WHERE user_id = ? ORDER BY saved_at DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_saved_response(user_id, response_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM saved_responses WHERE id = ? AND user_id = ?", (response_id, user_id))
    conn.commit()
    conn.close()

def set_user_preference(user_id, city, property_type, price_range):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO user_preferences (user_id, city, property_type, price_range)
                 VALUES (?, ?, ?, ?)''', (user_id, city, property_type, price_range))
    conn.commit()
    conn.close()

def get_user_preferences(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT city, property_type, price_range FROM user_preferences WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"city": row[0], "property_type": row[1], "price_range": row[2]}
    return None

def log_audit(user_id, action, details=""):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT INTO audit_log (user_id, action, details, timestamp)
                 VALUES (?, ?, ?, ?)''', (user_id, action, details, now))
    conn.commit()
    conn.close()

def save_pending_action(user_id, action, data, secret_code):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT INTO pending_actions (user_id, action, data, timestamp, secret_code)
                 VALUES (?, ?, ?, ?, ?)''', (user_id, action, data, now, secret_code))
    conn.commit()
    conn.close()
    return c.lastrowid

def get_pending_action(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, action, data, timestamp FROM pending_actions WHERE user_id = ? ORDER BY timestamp DESC LIMIT 1", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "action": row[1], "data": row[2], "timestamp": row[3]}
    return None

def delete_pending_action(action_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM pending_actions WHERE id = ?", (action_id,))
    conn.commit()
    conn.close()

def get_session_id(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("SELECT session_id FROM sessions WHERE user_id = ? AND is_active = 1", (user_id,))
    row = c.fetchone()
    if row:
        session_id = row[0]
        c.execute("UPDATE sessions SET end_time = ? WHERE session_id = ?", (now, session_id))
        conn.commit()
        conn.close()
        return session_id
    session_id = f"{user_id}_{datetime.now().timestamp()}"
    c.execute('''INSERT INTO sessions (session_id, user_id, start_time, end_time, is_active)
                 VALUES (?, ?, ?, ?, 1)''', (session_id, user_id, now, now))
    conn.commit()
    conn.close()
    return session_id

def end_session(session_id):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("UPDATE sessions SET is_active = 0, end_time = ? WHERE session_id = ?", (now, session_id))
    conn.commit()
    conn.close()

# ======================= روابط اليوتيوب =======================
YOUTUBE_LINKS = {
    "إفراغ عقاري (بورصة)": {
        "primary": "https://youtu.be/_0a2CbfFmMA",
        "title": "شرح طريقة الإفراغ العقاري عبر البورصة العقارية"
    },
    "إفراغ عقاري (سجل عقاري)": {
        "primary": "https://youtu.be/P2ehPAcdtvg",
        "secondary": ["https://youtu.be/ERRSS-74TUA", "https://youtu.be/IVPIgNsQE4o"],
        "title": "شرح الافراغ بالسجل العقاري"
    },
    "تسجيل عيني": {
        "primary": "https://youtu.be/bnuACgiKPv8",
        "secondary": ["https://youtu.be/dGHLinBQ8Pc"],
        "title": "شرح التسجيل العيني أو تسجيل العقار عينياً"
    },
    "عقد وساطة": {
        "primary": "https://youtu.be/VcAZkaevjRg",
        "title": "شرح عمل عقد وساطة"
    },
    "عقد وساطة مع مستثمر": {
        "primary": "https://youtu.be/sKopwc3byYs",
        "title": "شرح عمل عقد وساطة مع مستثمر أو مشتري أو مستأجر"
    },
    "عقد وساطة بين وسطاء": {
        "primary": "https://youtu.be/VcAZkaevjRg",
        "title": "شرح عمل عقد وساطة بين وسيط ووسيط"
    },
    "عقد إيجار سكني": {
        "primary": "https://youtu.be/kGN4zp0NDho",
        "title": "شرح طريقة إنشاء عقد إيجار سكني بالتفاصيل"
    },
    "عقد إيجار تجاري": {
        "primary": "https://youtu.be/M1a6oLV5y6g",
        "title": "شرح طريقة إنشاء عقد إيجار تجاري بالتفاصيل"
    },
    "ترخيص مزاد عقاري": {
        "primary": "https://youtu.be/fY7BxNYE1MY",
        "title": "شرح طريقة طلب ترخيص مزاد عقاري"
    },
    "مطالبة إيجار متأخر": {
        "primary": "https://youtu.be/RgoYAtTsb-g",
        "title": "شرح طريقة المطالبة بالإيجار المتأخر وفسخ العقد وإخلاء العقار"
    },
    "دفع العربون": {
        "primary": "https://youtu.be/xUZQod_vRpQ",
        "title": "شرح طريقة دفع العربون عبر منصة الهيئة العامة للعقار"
    },
    "إنهاء عقد إيجار": {
        "primary": "https://youtu.be/KMbtdGtbKjo",
        "title": "شرح طريقة إنهاء عقد إيجار بالتراضي بين المؤجر والمستأجر"
    }
}

# ======================= دالة تنسيق روابط اليوتيوب (Markdown) =======================
def format_youtube_message(youtube_links):
    if not youtube_links:
        return None
    msg = "🎥 *شروحات بالفيديو:*\n\n"
    for i, link in enumerate(youtube_links, 1):
        title = link.get('title', 'شرح')
        primary = link.get('primary', '')
        secondary = link.get('secondary', [])
        msg += f"{i}. *{title}*\n"
        msg += f"   🔗 {primary}\n"
        if secondary:
            for j, sec in enumerate(secondary, 1):
                msg += f"      - رابط إضافي {j}: {sec}\n"
        msg += "\n"
    return msg

# ======================= دالة استرجاع روابط اليوتيوب المحسنة =======================
def get_youtube_links(classification: str, user_message: str = "") -> list:
    results = []
    if classification is None:
        classification = ""
    if user_message is None:
        user_message = ""
    normalized_msg = normalize_arabic(user_message)
    if classification in YOUTUBE_LINKS:
        results.append(YOUTUBE_LINKS[classification])
    for key in YOUTUBE_LINKS.keys():
        if key in classification or classification in key:
            if YOUTUBE_LINKS[key] not in results:
                results.append(YOUTUBE_LINKS[key])
    keywords_map = {
        "سجل عقاري": "إفراغ عقاري (سجل عقاري)",
        "السجل العقاري": "إفراغ عقاري (سجل عقاري)",
        "بورصة": "إفراغ عقاري (بورصة)",
        "تسجيل عيني": "تسجيل عيني",
        "تسجيل العقار": "تسجيل عيني",
        "وساطة": "عقد وساطة",
        "عقد وساطة": "عقد وساطة",
        "وسيط": "عقد وساطة",
        "إيجار سكني": "عقد إيجار سكني",
        "ايجار سكني": "عقد إيجار سكني",
        "عقد إيجار سكني": "عقد إيجار سكني",
        "عقد ايجار سكني": "عقد إيجار سكني",
        "إيجار تجاري": "عقد إيجار تجاري",
        "ايجار تجاري": "عقد إيجار تجاري",
        "عقد إيجار تجاري": "عقد إيجار تجاري",
        "عقد ايجار تجاري": "عقد إيجار تجاري",
        "مزاد": "ترخيص مزاد عقاري",
        "ترخيص مزاد": "ترخيص مزاد عقاري",
        "مطالبة": "مطالبة إيجار متأخر",
        "إخلاء": "مطالبة إيجار متأخر",
        "فسخ": "مطالبة إيجار متأخر",
        "عربون": "دفع العربون",
        "إنهاء عقد": "إنهاء عقد إيجار",
        "انهاء عقد": "إنهاء عقد إيجار",
        "إنهاء الإيجار": "إنهاء عقد إيجار",
        "انهاء الايجار": "إنهاء عقد إيجار",
        "مستثمر": "عقد وساطة مع مستثمر",
        "مشتري": "عقد وساطة مع مستثمر",
        "مستأجر": "عقد وساطة مع مستثمر",
        "وسيط ووسيط": "عقد وساطة بين وسطاء"
    }
    for key, category in keywords_map.items():
        key_norm = normalize_arabic(key)
        if key in user_message or key_norm in normalized_msg:
            if category in YOUTUBE_LINKS:
                if YOUTUBE_LINKS[category] not in results:
                    results.append(YOUTUBE_LINKS[category])
    unique_results = []
    for item in results:
        if item not in unique_results:
            unique_results.append(item)
    return unique_results

# ======================= البرومبت الأساسي (بدون تغيير - Markdown) =======================
BASE_SYSTEM_PROMPT = """
أنت **خبير عقاري سعودي**، ملم بالأنظمة العقارية السعودية والمصادر الرسمية والميدانية.

🔴 **القاعدة الصفرية (الدور المطلق):**
أنت تعمل حصراً كخبير عقاري سعودي. الرد على أي سؤال غير عقاري هو: "أنا مختص بالشأن العقاري السعودي فقط. هل لديك سؤال عقاري؟"

🔴 **تحديد المصطلحات (إلزامي):**
- كلمة "مزاد" في سياق هذا البوت تعني **"المزاد العقاري"** فقط، وهو عملية بيع وشراء العقارات عبر المزاد العلني.
- كلمة "ترخيص مزاد" تعني **"ترخيص المزاد العقاري"** الصادر عن الهيئة العامة للعقار.
- أي سؤال يحتوي على "مزاد" أو "ترخيص مزاد" يُفهم على أنه عن **المزاد العقاري** وليس عن أي نوع آخر من المزادات (مثل مزاد السيارات، المزادات الحكومية، إلخ).
- إذا كان السؤال عن "مظاد" أو "صيد" أو أي مصطلح غير عقاري، يجب الرد بالجملة الثابتة: "أنا مختص بالشأن العقاري السعودي فقط. هل لديك سؤال عقاري؟"

🔴 **مهمتك الآن:**
قدّم **رداً مختصراً شاملاً** يحتوي على الأقسام التالية بوضوح (مع عناوينها):
1. **الجهة المعنية:** (مثل: الهيئة العامة للعقار، وزارة الإعلام، البلدية).
2. **الحكم:** (نعم/لا/مسموح/ممنوع).
3. **مختصر الشروط:** (أهم الشروط القانونية مختصرة، وليست مفصلة).
4. **مختصر المتطلبات:** (أهم المستندات والتراخيص مختصرة).
5. **مختصر الخطوات:** (أهم الخطوات العملية مختصرة).

**🔴 تعليمات البحث الإلزامية (يجب تنفيذها بدقة):**
- المصادر الـ16 المذكورة أدناه هي مصدرك الوحيد.
- **يجب أن تبحث فعلياً في هذه المصادر** ولا تكتفي بالقول "لم أجد معلومات".
- إذا كان السؤال عن التراخيص → ابحث في الهيئة العامة للعقار (المصدر 1) ووزارة الإعلام (المصدر 5).
- إذا كان السؤال عن الإيجار → ابحث في منصة إيجار (المصدر 2).
- إذا كان السؤال عن التسجيل العيني → ابحث في السجل العقاري (المصدر 15).
- إذا كان السؤال عن الوساطة → ابحث في نظام الوساطة (المصدر 10).
- إذا كان السؤال عن النطاقات الجغرافية → ابحث في بوابة النطاقات (المصدر 16).
- إذا كان السؤال عن المزاد العقاري أو ترخيص مزاد → ابحث في الهيئة العامة للعقار (المصدر 1).
- **إذا وجدت المعلومة في أي مصدر، اذكرها ولو كانت جزئية. لا تقل "لم أجد" إلا بعد التأكد من جميع المصادر.**
- **إذا لم تجد المعلومة في المصادر الـ16، اعتذر بصدق ولا تختلق معلومات من معرفتك العامة.**

## المصادر المعتمدة (16 مصدراً):
[النوع الأول – الرسمية والتشريعية]
.1 الهيئة العامة للعقار (rega.gov.sa)
.2 منصة إيجار (ejar.sa)
.3 منصة سكني (sakani.sa)
.4 البلديات وأمانات المناطق
.5 وزارة الإعلام (media.gov.sa) – رخصة "موثوق"
.6 الجريدة الرسمية (أم القرى)
.7 الحسابات الرسمية الموثقة
.8 وزارة الإعلام
.9 وزارة البلديات والإسكان
.10 نظام الوساطة العقارية (م/130)
.11 اللائحة التنظيمية للتسويق والإعلانات العقارية
[النوع الثاني – الميدانية]
.12 عقار، بيوت السعوديه، ديل، وصلت، حراج
.13 حسابات الوسطاء الموثقة
.14 أي مصدر عقاري سعودي معروف
.15 منصة السجل العقاري (rer.sa)
.16 بوابة النطاقات الجغرافية (saudiproperties.rega.gov.sa/zones)

## التنسيق المطلوب:
- ابدأ بـ "📌 **الإجابة المختصرة:**"
- ثم اذكر الأقسام الخمسة بالترتيب مع عناوينها كما هو مطلوب أعلاه.
- لا تذكر التفاصيل الكاملة (الشروط التفصيلية، المتطلبات الكاملة، الخطوات التفصيلية) هنا.
- **بعد الإجابة، أضف سطراً فارغاً، ثم هذا النص بالخط العريض:**
  
**🔍 اختر من الأزرار أدناه للحصول على التفاصيل أو الشرح بالفيديو:**

عند بدء التشغيل: "تفضل: هل لديك سؤال عقاري؟"
"""

# ======================= التذييل =======================
FOOTER = """

-------
**تمت بدعم من:** 
*سلطان آل ناجد العسيري*
المرجع المعلوماتي للوسيط العقاري
https://linktr.ee/sultan.al3siry
*(كدعم معلوماتي وتطبيقي للوسطاء العقاريين من خلال المصادر الرسمية، وليس استشارة استثمارية أو قانونية أو ترخيصاً.)*
**"الوسيط هو المسؤول الوحيد عن امتثال أعماله للأنظمة والتشريعات السعودية"**
"""

# ======================= دوال الذكاء الاصطناعي =======================
def is_api_error(response_text: str) -> bool:
    error_indicators = [
        "Error code:", "API key", "PERMISSION_DENIED", "API_KEY_SERVICE_BLOCKED",
        "Quota exceeded", "Requested entity was not found", "Resource has been exhausted",
        "The model is temporarily unavailable", "429", "500", "503",
        "فشل", "❌", "HTTPError", "Unauthorized", "Forbidden", "timeout"
    ]
    return any(indicator.lower() in response_text.lower() for indicator in error_indicators)

# ======================= التصنيف المحسن =======================
def classify_question(user_message: str) -> str:
    corrected = correct_spelling(user_message)
    normalized = normalize_arabic(corrected)
    try:
        response = client_groq.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": """أنت خبير تصنيف أسئلة عقارية. صنف السؤال إلى واحدة من هذه الفئات فقط:
- 'عقد وساطة': عن عقود الوساطة (مثل: عقد وساطة، وساطة عقارية، عمولة وساطة)
- 'عقد وساطة مع مستثمر': عن وساطة مع مستثمر أو مشتري أو مستأجر
- 'عقد وساطة بين وسطاء': عن وساطة بين وسيط ووسيط
- 'عقد إيجار سكني': عن عقود الإيجار السكني (مثل: إيجار سكني، عقد إيجار سكني)
- 'عقد إيجار تجاري': عن عقود الإيجار التجاري (مثل: إيجار تجاري، عقد إيجار تجاري)
- 'تسجيل عيني': عن التسجيل العيني في السجل العقاري (مثل: تسجيل عقار، تسجيل عيني، سجل عقاري)
- 'إفراغ عقاري': عن الإفراغ العقاري أو البورصة العقارية (مثل: افراغ، إفراغ، نقل ملكية، بورصة)
- 'إفراغ بالسجل العقاري': عن الافراغ عبر السجل العقاري (مثل: افراغ سجل عقاري، افراغ بالسجل)
- 'إعلان': عن الإعلان في وسائل التواصل
- 'ترخيص مزاد عقاري': عن تراخيص المزادات العقارية (مثل: مزاد عقاري، ترخيص مزاد)
- 'مطالبة إيجار متأخر': عن المطالبة بالإيجار المتأخر والفسخ والإخلاء
- 'دفع العربون': عن دفع العربون
- 'إنهاء عقد إيجار': عن إنهاء عقد الإيجار بالتراضي
- 'طلب توضيح': يطلب شرحاً لرد سابق
- 'سؤال عام': لأي سؤال عقاري آخر

أمثلة:
- سؤال: "كيف افرغ عقار عبر البورصة؟" → إفراغ عقاري
- سؤال: "ما هي خطوات التسجيل العيني؟" → تسجيل عيني
- سؤال: "عقد وساطة مع مستثمر" → عقد وساطة مع مستثمر
- سؤال: "ماذا أفعل إذا تأخر المستأجر؟" → مطالبة إيجار متأخر

أجب فقط باسم الفئة بدون أي إضافات."""},
                {"role": "user", "content": normalized}
            ],
            temperature=0.1,
            max_tokens=30
        )
        classification = response.choices[0].message.content.strip()
        logger.info(f"📊 التصنيف: {classification}")
        return classification
    except Exception as e:
        logger.warning(f"⚠️ فشل التصنيف: {e}")
        if "افراغ" in normalized or "إفراغ" in normalized:
            if "سجل عقاري" in normalized:
                return "إفراغ بالسجل العقاري"
            return "إفراغ عقاري"
        if "تسجيل" in normalized or "عيني" in normalized:
            return "تسجيل عيني"
        if "وساطة" in normalized or "عقد وساطة" in normalized:
            return "عقد وساطة"
        if "إيجار سكني" in normalized or "ايجار سكني" in normalized:
            return "عقد إيجار سكني"
        if "إيجار تجاري" in normalized or "ايجار تجاري" in normalized:
            return "عقد إيجار تجاري"
        if "مزاد" in normalized:
            return "ترخيص مزاد عقاري"
        if "مطالبة" in normalized or "إخلاء" in normalized or "فسخ" in normalized:
            return "مطالبة إيجار متأخر"
        if "عربون" in normalized:
            return "دفع العربون"
        if "إنهاء عقد" in normalized or "انهاء عقد" in normalized:
            return "إنهاء عقد إيجار"
        return "سؤال عام"

# ======================= توليد الرد المختصر =======================
def get_ai_summary_response(user_message: str, user_id: int = None) -> str:
    non_real_estate_keywords = [
        "قصة", "تاريخ", "ذو القرنين", "ديني", "ثقافي", "أدبي", "شعر", "رواية",
        "قصيدة", "نثر", "خيال", "علمي", "فلك", "نجوم", "فيزياء",
        "كيمياء", "أحياء", "طب", "جراحة", "علاج", "دواء", "موسيقى", "غناء",
        "فن", "رسم", "نحت", "هندسة", "برمجة", "حاسوب", "ذكاء اصطناعي",
        "مظاد", "صيد", "سلاح"
    ]
    if any(kw in user_message for kw in non_real_estate_keywords):
        return "أنا مختص بالشأن العقاري السعودي فقط. هل لديك سؤال عقاري؟"

    active_rule = get_active_rule()
    base_prompt = active_rule if active_rule else BASE_SYSTEM_PROMPT

    preferences = ""
    if user_id:
        prefs = get_user_preferences(user_id)
        if prefs:
            preferences = f"\n\n**تفضيلات المستخدم:** المدينة: {prefs['city'] or 'غير محدد'}, نوع العقار: {prefs['property_type'] or 'غير محدد'}, النطاق السعري: {prefs['price_range'] or 'غير محدد'}.\nيمكنك تخصيص الرد بناءً على هذه التفضيلات إذا كانت ذات صلة."

    enhanced_prompt = base_prompt + preferences + """

🔴 **تذكير إضافي:** 
- المصادر الـ16 هي مرجعك الوحيد.
- ابحث فيها جميعاً، واستخرج المعلومات حتى لو كانت جزئية.
- إذا وجدت معلومة في مصدر ميداني (مثل عقار، حراج)، اذكرها مع تحذير "مصدر ميداني".
- لا تخرج عن المصادر، ولا تختلق معلومات.
- إذا كانت المعلومة موجودة في أكثر من مصدر، اذكر المصادر كلها.
- تذكر: كلمة "مزاد" تعني "مزاد عقاري" فقط.
"""

    try:
        logger.info("⚡ توليد الرد المختصر...")
        response = client_groq.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": enhanced_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.2,
            max_tokens=700
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            if "🔍 اختر من الأزرار أدناه" not in reply:
                reply = reply + "\n\n**🔍 اختر من الأزرار أدناه للحصول على التفاصيل أو الشرح بالفيديو:**"
            return reply
    except Exception as e:
        logger.warning(f"⚠️ فشل توليد الرد المختصر (Groq): {e}")

    try:
        logger.info("🔄 باستخدام Gemini للرد المختصر...")
        response = client_gemini.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[
                {"role": "system", "content": enhanced_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.2,
            max_tokens=700
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            if "🔍 اختر من الأزرار أدناه" not in reply:
                reply = reply + "\n\n**🔍 اختر من الأزرار أدناه للحصول على التفاصيل أو الشرح بالفيديو:**"
            return reply
    except Exception as e:
        logger.warning(f"⚠️ فشل Gemini: {e}")

    return "❌ عذراً، جميع خدمات الذكاء الاصطناعي غير متاحة حالياً. يرجى المحاولة لاحقاً."

# ======================= توليد الأقسام التفصيلية =======================
def get_section_response(user_message: str, section: str) -> str:
    section_prompts = {
        "source": """أعطِ فقط الاقتباسات الحرفية من المصادر الرسمية مع رابط كل مصدر.
- ابحث في المصادر الـ16.
- انسخ النص الرسمي بين علامتي تنصيص كما هو.
- اذكر رابط المصدر بعد كل اقتباس.
- إذا وجدت أكثر من مصدر، اذكرها جميعاً.
- لا تختلق معلومات، ولا تقل "لم أجد" قبل البحث في جميع المصادر.""",
        "requirements": """أعطِ فقط قائمة المتطلبات (المستندات، التراخيص، الإجراءات المطلوبة) بشكل منظم ونقطي.
- اعتمد على المصادر الـ16.
- اذكر كل متطلب مع مصدره.
- لا تكرر الشروط أو الخطوات هنا.""",
        "conditions": """أعطِ فقط قائمة الشروط القانونية والتنظيمية بشكل منظم ونقطي.
- اعتمد على المصادر الـ16.
- اذكر كل شرط مع مصدره.
- لا تكرر المتطلبات أو الخطوات هنا.""",
        "steps": """أعطِ فقط الخطوات العملية التي يجب اتخاذها بشكل منظم ومتسلسل.
- اعتمد على المصادر الـ16.
- اذكر كل خطوة مع مصدرها.
- لا تكرر الشروط أو المتطلبات هنا.""",
        "procedures": """أعطِ فقط المعلومات المتعلقة بـ:
1. **الجهات المعنية:** اذكر الجهات الرسمية المختصة (الهيئة العامة للعقار، منصة إيجار، السجل العقاري، البلديات، وزارة الإعلام) مع شرح مختصر عن دور كل جهة.
2. **الرسوم والضرائب:** اذكر الرسوم المطلوبة (رسوم الهيئة، رسوم التوثيق، رسوم البلدية، الضرائب العقارية) مع المبالغ إن وجدت في المصادر الـ16.

**تحذير:** لا تذكر أي معلومات عن وزارة العدل، الغرفة التجارية، البنوك، أو أي جهة غير مدرجة في المصادر الـ16. إذا لم تجد المعلومات في المصادر الـ16، اعتذر ولا تختلق."""
    }
    instruction = section_prompts.get(section, "أعطِ التفاصيل المطلوبة فقط مع المصادر.")
    system_prompt = f"""
أنت خبير عقاري سعودي. مصدرك الوحيد هو المصادر الـ16 المذكورة سابقاً.
المستخدم يسأل عن: {user_message}

{instruction}

🔴 تذكير: ابحث في جميع المصادر الـ16 قبل الإجابة. إذا وجدت المعلومة ولو جزئياً، اذكرها مع المصدر. لا تقل "لم أجد" إلا بعد التأكد من جميع المصادر.
"""
    try:
        logger.info(f"⚡ توليد قسم: {section} (محاولة 1)")
        response = client_groq.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.2,
            max_tokens=1200
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            return reply
        else:
            logger.warning(f"⚠️ استجابة API تحتوي على خطأ: {reply[:100]}")
    except Exception as e:
        logger.warning(f"⚠️ فشل توليد القسم {section} (Groq): {e}")

    try:
        logger.info(f"🔄 باستخدام Gemini للقسم {section} (محاولة 2)")
        response = client_gemini.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.2,
            max_tokens=1200
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            return reply
        else:
            logger.warning(f"⚠️ استجابة Gemini تحتوي على خطأ: {reply[:100]}")
    except Exception as e:
        logger.warning(f"⚠️ فشل Gemini للقسم {section}: {e}")

    if client_openrouter:
        try:
            logger.info(f"🔄 باستخدام OpenRouter للقسم {section} (محاولة 3)")
            response = client_openrouter.chat.completions.create(
                model="meta-llama/llama-3.1-8b-instruct",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                temperature=0.2,
                max_tokens=1200
            )
            reply = response.choices[0].message.content
            if not is_api_error(reply):
                return reply
        except Exception as e:
            logger.warning(f"⚠️ فشل OpenRouter للقسم {section}: {e}")

    return f"❌ عذراً، لم أتمكن من استرجاع تفاصيل '{section}' بسبب مشكلة في الاتصال بخدمات الذكاء الاصطناعي. يرجى المحاولة لاحقاً، أو استخدام الأزرار الأخرى."

# ======================= دوال التأكيد بالرقم السري =======================
pending_secret_requests = {}

async def request_secret_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, data: dict):
    user_id = update.effective_user.id
    secret = get_admin_secret(user_id)
    if not secret:
        await update.message.reply_text("❌ ليس لديك صلاحية كمدير.")
        return
    save_pending_action(user_id, action, json.dumps(data), secret)
    pending_secret_requests[user_id] = {
        "action": action,
        "data": data,
        "timestamp": datetime.now()
    }
    await update.message.reply_text(
        f"⚠️ *تأكيد الأمان:*\n"
        f"أنت على وشك تنفيذ أمر حساس: `{action}`.\n"
        f"الرجاء إدخال الرقم السري الخاص بك لتأكيد العملية.",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_secret_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text.strip()

    if user_id not in pending_secret_requests:
        return

    pending = pending_secret_requests[user_id]
    if (datetime.now() - pending["timestamp"]).total_seconds() > 300:
        del pending_secret_requests[user_id]
        await update.message.reply_text("⏳ انتهت صلاحية طلب التأكيد.")
        return

    stored_secret = get_admin_secret(user_id)
    if not stored_secret:
        del pending_secret_requests[user_id]
        await update.message.reply_text("❌ ليس لديك صلاحية كمدير.")
        return

    if user_message == stored_secret:
        action = pending["action"]
        data = pending["data"]
        del pending_secret_requests[user_id]
        pending_record = get_pending_action(user_id)
        if pending_record:
            delete_pending_action(pending_record["id"])

        if action == "set_rule":
            new_rule = data["rule_text"]
            delete_setting("custom_rule")
            add_custom_rule("active_rule", new_rule, user_id)
            activate_rule("active_rule")
            log_audit(user_id, "set_rule", f"تم تحديث القاعدة بـ: {new_rule[:50]}...")
            await update.message.reply_text("✅ تم تحديث القاعدة بنجاح!")
        elif action == "add_rule":
            add_custom_rule(data["rule_name"], data["rule_text"], user_id)
            log_audit(user_id, "add_rule", f"تم إضافة قاعدة: {data['rule_name']}")
            await update.message.reply_text(f"✅ تم إضافة القاعدة '{data['rule_name']}'.")
        elif action == "edit_rule":
            update_custom_rule(data["rule_name"], data["new_text"])
            log_audit(user_id, "edit_rule", f"تم تعديل قاعدة: {data['rule_name']}")
            await update.message.reply_text(f"✅ تم تعديل القاعدة '{data['rule_name']}'.")
        elif action == "delete_rule":
            delete_custom_rule(data["rule_name"])
            log_audit(user_id, "delete_rule", f"تم حذف قاعدة: {data['rule_name']}")
            await update.message.reply_text(f"✅ تم حذف القاعدة '{data['rule_name']}'.")
        elif action == "clear_all_rules":
            delete_all_custom_rules()
            log_audit(user_id, "clear_all_rules", "تم حذف جميع القواعد المخصصة")
            await update.message.reply_text("✅ تم حذف جميع القواعد المخصصة.")
        else:
            await update.message.reply_text("❌ إجراء غير معروف.")
    else:
        await update.message.reply_text("❌ الرقم السري غير صحيح.")

# ======================= أوامر الإدارة =======================
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❗ استخدم: /addadmin @username الرمز_السري [role]")
        return

    username = args[0].replace("@", "")
    secret = args[1]
    role = args[2] if len(args) > 2 else "admin"

    try:
        user_obj = await context.bot.get_chat(username)
        user_id = user_obj.id
    except:
        await update.message.reply_text("❌ لم أجد هذا المستخدم.")
        return

    hashed_secret = hashlib.sha256(secret.encode()).hexdigest()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO admins (user_id, username, secret_code, added_by, added_date, role)
                 VALUES (?, ?, ?, ?, ?, ?)''', (user_id, username, hashed_secret, user.id, datetime.now().isoformat(), role))
    conn.commit()
    conn.close()

    log_audit(user.id, "add_admin", f"تم إضافة {username} كمدير")
    await update.message.reply_text(f"✅ تم إضافة {username} كمدير بنجاح!")

async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /removeadmin @username")
        return

    username = args[0].replace("@", "")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM admins WHERE username = ?", (username,))
    conn.commit()
    deleted = c.rowcount > 0
    conn.close()

    if deleted:
        log_audit(user.id, "remove_admin", f"تم حذف {username} من المدراء")
        await update.message.reply_text(f"✅ تم حذف {username} من قائمة المدراء.")
    else:
        await update.message.reply_text(f"❌ لم أجد {username} في قائمة المدراء.")

async def admins_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, _ = is_admin(user.id)
    if not is_adm and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return

    admins = get_all_admins()
    if not admins:
        await update.message.reply_text("لا يوجد مدراء مسجلون.")
        return

    msg = "📋 *قائمة المدراء:*\n\n"
    for a in admins:
        role = a[5] if len(a) > 5 else "admin"
        msg += f"- @{a[1]} (دور: {role})\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def set_admin_role_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❗ استخدم: /setadminrole @username role (admin/super_admin/moderator)")
        return

    username = args[0].replace("@", "")
    role = args[1]

    if role not in ["admin", "super_admin", "moderator"]:
        await update.message.reply_text("❌ دور غير صالح. الأدوار المتاحة: admin, super_admin, moderator")
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE admins SET role = ? WHERE username = ?", (role, username))
    conn.commit()
    if c.rowcount == 0:
        await update.message.reply_text(f"❌ لم أجد {username} في قائمة المدراء.")
    else:
        log_audit(user.id, "set_admin_role", f"تم تغيير دور {username} إلى {role}")
        await update.message.reply_text(f"✅ تم تغيير دور {username} إلى {role}.")
    conn.close()

# ======================= أوامر القواعد =======================
async def set_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /rule النص_الجديد للقاعدة")
        return

    new_rule = " ".join(args)
    await request_secret_confirmation(update, context, "set_rule", {"rule_text": new_rule})

async def clear_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    delete_setting("custom_rule")
    delete_all_custom_rules()
    log_audit(user.id, "clear_rule", "تم إلغاء القاعدة المخصصة")
    await update.message.reply_text("✅ تم إلغاء القاعدة المخصصة، والعودة إلى القاعدة الافتراضية.")

async def add_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❗ استخدم: /addrule اسم_القاعدة النص")
        return

    rule_name = args[0]
    rule_text = " ".join(args[1:])
    await request_secret_confirmation(update, context, "add_rule", {"rule_name": rule_name, "rule_text": rule_text})

async def list_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, _ = is_admin(user.id)
    if not is_adm and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return

    rules = get_all_custom_rules()
    if not rules:
        await update.message.reply_text("لا توجد قواعد مخصصة.")
        return

    msg = "📋 *قائمة القواعد المخصصة:*\n\n"
    for r in rules:
        status = "✅ (نشطة)" if r[5] == 1 else "⏸ (غير نشطة)"
        msg += f"- *{r[1]}* {status}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def show_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, _ = is_admin(user.id)
    if not is_adm and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /showrule اسم_القاعدة")
        return

    rule_name = args[0]
    rule_text = get_custom_rule(rule_name)
    if not rule_text:
        await update.message.reply_text(f"❌ لم أجد قاعدة باسم '{rule_name}'.")
        return

    await update.message.reply_text(f"📜 *نص القاعدة '{rule_name}':*\n\n{rule_text}", parse_mode=ParseMode.MARKDOWN)

async def activate_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /activerule اسم_القاعدة")
        return

    rule_name = args[0]
    if not get_custom_rule(rule_name):
        await update.message.reply_text(f"❌ لم أجد قاعدة باسم '{rule_name}'.")
        return

    activate_rule(rule_name)
    log_audit(user.id, "activate_rule", f"تم تفعيل قاعدة: {rule_name}")
    await update.message.reply_text(f"✅ تم تفعيل القاعدة '{rule_name}' بنجاح.")

async def edit_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❗ استخدم: /editrule اسم_القاعدة النص_الجديد")
        return

    rule_name = args[0]
    new_text = " ".join(args[1:])
    if not get_custom_rule(rule_name):
        await update.message.reply_text(f"❌ لم أجد قاعدة باسم '{rule_name}'.")
        return

    await request_secret_confirmation(update, context, "edit_rule", {"rule_name": rule_name, "new_text": new_text})

async def delete_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /deleterule اسم_القاعدة")
        return

    rule_name = args[0]
    if not get_custom_rule(rule_name):
        await update.message.reply_text(f"❌ لم أجد قاعدة باسم '{rule_name}'.")
        return

    await request_secret_confirmation(update, context, "delete_rule", {"rule_name": rule_name})

async def clear_all_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    await request_secret_confirmation(update, context, "clear_all_rules", {})

# ======================= أوامر المستخدم =======================
async def saved_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    saved = get_saved_responses(user.id)
    if not saved:
        await update.message.reply_text("ليس لديك أي ردود محفوظة.")
        return
    msg = "📚 *الردود المحفوظة:*\n\n"
    for s in saved[:10]:
        msg += f"*{s[1]}*\n{s[2][:100]}...\n\n"
    if len(saved) > 10:
        msg += f"... و {len(saved)-10} ردود أخرى."
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def save_response_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    session_id = get_session_id(user.id)
    context_data = get_context(user.id, session_id)
    if not context_data:
        await update.message.reply_text("لا يوجد رد لحفظه. اسأل سؤالاً أولاً.")
        return
    last_q = context_data.get("last_question")
    last_a = context_data.get("last_suggestion")
    if not last_q or not last_a:
        await update.message.reply_text("لا يوجد رد لحفظه.")
        return
    save_saved_response(user.id, last_q, last_a)
    await update.message.reply_text("✅ تم حفظ الرد للرجوع إليه لاحقاً. استخدم /saved لعرض المحفوظات.")

async def preferences_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("❗ استخدم: /preferences المدينة نوع_العقار النطاق_السعري\nمثال: /preferences الرياض سكني 500-1000")
        return
    city = args[0]
    property_type = args[1]
    price_range = " ".join(args[2:])
    set_user_preference(user.id, city, property_type, price_range)
    await update.message.reply_text("✅ تم حفظ تفضيلاتك بنجاح.")

async def audit_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, role = is_admin(user.id)
    if not is_adm or role not in ["super_admin", "admin"]:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, action, details, timestamp FROM audit_log ORDER BY timestamp DESC LIMIT 20")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("لا يوجد سجل للعمليات.")
        return
    msg = "📋 *سجل العمليات الإدارية (آخر 20):*\n\n"
    for r in rows:
        msg += f"- [{r[3]}] {r[1]}: {r[2]}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ======================= بناء لوحة المفاتيح التفاعلية =======================
def get_main_keyboard(has_youtube: bool = False, has_save: bool = False):
    keyboard = []
    
    if has_youtube:
        keyboard.append([InlineKeyboardButton("🎥 شرح باليوتيوب", callback_data="show_youtube")])
    
    keyboard.append([InlineKeyboardButton("📌 الرد المختصر", callback_data="show_summary"),
                     InlineKeyboardButton("📄 التفاصيل من المصادر", callback_data="detail_source")])
    keyboard.append([InlineKeyboardButton("📋 المتطلبات", callback_data="detail_requirements"),
                     InlineKeyboardButton("⚖️ الشروط", callback_data="detail_conditions")])
    keyboard.append([InlineKeyboardButton("📝 الخطوات", callback_data="detail_steps"),
                     InlineKeyboardButton("🛠️ الإجراءات التنظيمية", callback_data="detail_procedures")])
    
    if has_save:
        keyboard.append([InlineKeyboardButton("💾 حفظ الرد", callback_data="save_response")])
    
    keyboard.append([InlineKeyboardButton("❓ سؤال عقاري آخر", callback_data="ask_another")])
    keyboard.append([InlineKeyboardButton("❓ هل هذه الإجابة مفيدة؟", callback_data="dummy_feedback")])
    keyboard.append([InlineKeyboardButton("✅ نعم", callback_data="feedback_yes"),
                     InlineKeyboardButton("❌ لا", callback_data="feedback_no")])
    
    return InlineKeyboardMarkup(keyboard)

# ======================= معالج الأزرار المحدث =======================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data
    session_id = get_session_id(user_id)

    context_data = get_context(user_id, session_id)
    last_q = context_data.get("last_question") if context_data else None
    last_summary = context_data.get("last_suggestion") if context_data else None
    classification = context_data.get("classification") if context_data else None
    youtube_links = context_data.get("youtube_links") if context_data else []

    # ====== زر "شرح باليوتيوب" ======
    if data == "show_youtube":
        if last_q is None:
            last_q = ""
        
        links = youtube_links if isinstance(youtube_links, list) else []
        
        if not links and last_q:
            links = get_youtube_links(classification, last_q)
            logger.info(f"🔄 فولباك: تم إعادة حساب الروابط لـ: {last_q[:30]}... => {len(links)} رابط")
        
        if links:
            msg = format_youtube_message(links)
            if msg:
                msg += FOOTER
                await query.edit_message_text(
                    msg,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=get_main_keyboard(has_youtube=True, has_save=True)
                )
            else:
                await query.edit_message_text(
                    "❌ لم أجد شرحاً بالفيديو لهذا الموضوع حالياً.",
                    reply_markup=get_main_keyboard()
                )
        else:
            await query.edit_message_text(
                "❌ لم أجد شرحاً بالفيديو لهذا الموضوع حالياً.",
                reply_markup=get_main_keyboard()
            )

    # ====== زر "الرد المختصر" ======
    elif data == "show_summary":
        if last_summary:
            has_youtube = len(youtube_links) > 0 if isinstance(youtube_links, list) else False
            await query.edit_message_text(
                last_summary,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_main_keyboard(has_youtube=has_youtube, has_save=True)
            )
        else:
            await query.edit_message_text("لم أجد رداً مختصراً سابقاً. اطرح سؤالاً جديداً.")

    # ====== أزرار التفاصيل (5 أقسام) ======
    elif data in ["detail_source", "detail_requirements", "detail_conditions", "detail_steps", "detail_procedures"]:
        section_map = {
            "detail_source": "source",
            "detail_requirements": "requirements",
            "detail_conditions": "conditions",
            "detail_steps": "steps",
            "detail_procedures": "procedures"
        }
        section = section_map.get(data)
        if last_q and section:
            reply = get_section_response(last_q, section)
            reply += FOOTER
            has_youtube = len(youtube_links) > 0 if isinstance(youtube_links, list) else False
            await query.edit_message_text(
                reply,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_main_keyboard(has_youtube=has_youtube, has_save=True)
            )
        else:
            await query.edit_message_text("لم أجد سؤالاً سابقاً.")

    # ====== زر "حفظ الرد" ======
    elif data == "save_response":
        if last_q and last_summary:
            save_saved_response(user_id, last_q, last_summary)
            await query.edit_message_text(
                "✅ تم حفظ الرد بنجاح! يمكنك استعراضه لاحقاً باستخدام /saved",
                reply_markup=get_main_keyboard(has_save=True)
            )
        else:
            await query.edit_message_text("لا يوجد رد لحفظه حالياً.")

    # ====== زر "سؤال عقاري آخر" ======
    elif data == "ask_another":
        clear_context(user_id, session_id)
        end_session(session_id)
        await context.bot.send_message(
            chat_id=user_id,
            text="تفضل طال عمرك.. هل لديك سؤال عقاري آخر؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗺️ النطاقات الجغرافية", callback_data="zones")],
                [InlineKeyboardButton("📌 المرجع الرئيسي", url="https://saudiproperties.rega.gov.sa")],
                [InlineKeyboardButton("📞 الدعم واتساب", url="https://wa.me/966568708086")]
            ])
        )
        await query.edit_message_reply_markup(reply_markup=None)

    # ====== زر وهمي ======
    elif data == "dummy_feedback":
        pass

    # ====== زر "النطاقات الجغرافية" ======
    elif data == "zones":
        zones_msg = """
🗺️ *النطاقات الجغرافية الجديدة (تحديث 2026)*

🔗 *المرجع الرسمي:* https://saudiproperties.rega.gov.sa/zones

📌 *المناطق المذكورة (13):*
الرياض، مكة، المدينة، القصيم، الشرقية، عسير، تبوك، حائل، الحدود الشمالية، جازان، نجران، الباحة، الجوف.

🏗️ *المشاريع المذكورة:*
• نيوم، البحر الأحمر، أمالا
• الرياض: القدية، المربع الجديد، المسار الرياضي، بوابة الدرعية، حديقة الملك سلمان، سدرة، كافد، مطار الملك سلمان
• جدة: أبتاون، العروس، وسط جدة
• مكة: أبراج مكة، المنار، برج أجياد، بوابة الملك سلمان، جبل عمر، ذاخر مكة
• المدينة: الغرة، المهوى، دار الهجرة، داون تاون المدينة

⚖️ *قواعد أساسية:*
• التملك داخل النطاقات المذكورة فقط
• مكة والمدينة: للمسلمين فقط
• الرياض وجدة: مناطق محددة
• المقيم: يحق له عقار سكني واحد خارج النطاقات

📞 للاستفسار: 920017183
"""
        await query.edit_message_text(
            zones_msg,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard()
        )

    # ====== أزرار التقييم ======
    elif data == "feedback_yes":
        if last_q and last_summary:
            save_cached_answer(last_q, last_summary, "المصادر الرسمية")
        await context.bot.send_message(
            chat_id=user_id,
            text="شكراً! تم حفظ هذه الإجابة للاستخدام المستقبلي.\n\nسم طال عمرك.. هل عندك سؤال عقاري آخر؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗺️ النطاقات الجغرافية", callback_data="zones")],
                [InlineKeyboardButton("📌 المرجع الرئيسي", url="https://saudiproperties.rega.gov.sa")],
                [InlineKeyboardButton("📞 الدعم واتساب", url="https://wa.me/966568708086")]
            ])
        )
        clear_context(user_id, session_id)
        await query.edit_message_reply_markup(reply_markup=None)

    elif data == "feedback_no":
        await context.bot.send_message(
            chat_id=user_id,
            text="شكراً لمشاركتك.\n\nسم طال عمرك.. هل عندك سؤال عقاري آخر؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗺️ النطاقات الجغرافية", callback_data="zones")],
                [InlineKeyboardButton("📌 المرجع الرئيسي", url="https://saudiproperties.rega.gov.sa")],
                [InlineKeyboardButton("📞 الدعم واتساب", url="https://wa.me/966568708086")]
            ])
        )
        clear_context(user_id, session_id)
        await query.edit_message_reply_markup(reply_markup=None)

    # ====== الأزرار القديمة ======
    elif data in ["clarify_conditions", "clarify_requirements", "clarify_steps", "clarify_all", "clarify_other", "confirm_yes", "confirm_no"]:
        await query.edit_message_text(
            "🔄 تم تحديث نظام البوت. الرجاء استخدام الأزرار الجديدة للحصول على المعلومات المطلوبة.",
            reply_markup=get_main_keyboard()
        )
        clear_context(user_id, session_id)

# ======================= دوال البوت =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.username, user.first_name)
    session_id = get_session_id(user.id)

    stats = get_stats()

    keyboard = [
        [InlineKeyboardButton("🗺️ النطاقات الجغرافية", callback_data="zones")],
        [InlineKeyboardButton("📌 المرجع الرئيسي", url="https://saudiproperties.rega.gov.sa")],
        [InlineKeyboardButton("📞 الدعم واتساب", url="https://wa.me/966568708086")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = f"""
📊 *إحصائيات البوت:*
━━━━━━━━━━━━━━━━━━━
🟢 *المستخدمين الحاليين (آخر 5 دقائق):* {stats['active_now']}
📈 *النشطين (آخر 7 أيام):* {stats['active_week']}
📊 *جميع المستخدمين (منذ البداية):* {stats['total_users']}
━━━━━━━━━━━━━━━━━━━

🔒 تطمن، لا يمكن لأحد الاطلاع على محادثاتك.
خصوصيتك أمانة في أعناقنا.

📢 *للتواصل مع المسؤول:*
- /report للإبلاغ عن مشكلة
- /suggest لتقديم اقتراح
- /complain لتقديم شكوى

❓ *سم طال عمرك.. هل لديك سؤال عقاري؟*
"""
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    user_message = update.message.text.strip()

    if user_id in pending_secret_requests:
        await handle_secret_confirmation(update, context)
        return

    save_user(user_id, user.username, user.first_name)
    session_id = get_session_id(user_id)

    last_activity = get_last_activity(user_id)
    show_header = False
    if last_activity:
        try:
            last_time = datetime.fromisoformat(last_activity)
            time_diff = datetime.now() - last_time
            if time_diff.total_seconds() > 7200:
                show_header = True
        except:
            pass
    update_last_activity(user_id)

    save_question(user_message)
    keywords = [word for word in user_message.split() if len(word) > 2]
    save_keywords(keywords)

    corrected = correct_spelling(user_message)
    normalized = normalize_arabic(corrected)

    # FAQ
    faq_answer = get_faq_answer(normalized)
    if faq_answer:
        logger.info(f"✅ تم الاسترجاع من FAQ لـ: {user_message}")
        await update.message.reply_text(faq_answer, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard())
        return

    # Cache
    cached = get_semantic_cached_answer(user_message)
    if cached:
        logger.info(f"✅ تم الاسترجاع من التخزين المؤقت الدلالي لـ: {user_message}")
        await update.message.reply_text(cached, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard())
        return

    # Keyword classification
    keyword_classification = None
    if "افراغ" in normalized or "إفراغ" in normalized:
        if "سجل عقاري" in normalized:
            keyword_classification = "إفراغ بالسجل العقاري"
        else:
            keyword_classification = "إفراغ عقاري"
    elif "تسجيل عيني" in normalized or "تسجيل العقار" in normalized or "تسجيل عقار" in normalized or "عينيا" in normalized:
        keyword_classification = "تسجيل عيني"
    elif "وساطة" in normalized or "عقد وساطة" in normalized:
        if "بين وسيط" in normalized or "وسيط ووسيط" in normalized:
            keyword_classification = "عقد وساطة بين وسطاء"
        elif "مستثمر" in normalized or "مشتري" in normalized or "مستأجر" in normalized:
            keyword_classification = "عقد وساطة مع مستثمر"
        else:
            keyword_classification = "عقد وساطة"
    elif "إيجار سكني" in normalized or "ايجار سكني" in normalized:
        keyword_classification = "عقد إيجار سكني"
    elif "إيجار تجاري" in normalized or "ايجار تجاري" in normalized:
        keyword_classification = "عقد إيجار تجاري"
    elif "مزاد" in normalized:
        keyword_classification = "ترخيص مزاد عقاري"
    elif "مطالبة" in normalized or "إخلاء" in normalized or "فسخ" in normalized:
        keyword_classification = "مطالبة إيجار متأخر"
    elif "عربون" in normalized:
        keyword_classification = "دفع العربون"
    elif "إنهاء عقد" in normalized or "انهاء عقد" in normalized or "إنهاء الإيجار" in normalized:
        keyword_classification = "إنهاء عقد إيجار"

    if keyword_classification:
        classification = keyword_classification
        logger.info(f"📊 التصنيف (من المرشح): {classification}")
    else:
        classification = classify_question(user_message)
        logger.info(f"📊 التصنيف (من النموذج): {classification}")

    youtube_links = get_youtube_links(classification, user_message)
    has_youtube = len(youtube_links) > 0

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        
        reply = get_ai_summary_response(user_message, user_id)

        is_apology = "أنا مختص بالشأن العقاري السعودي فقط" in reply
        if is_apology:
            save_rejection(user_message)
            await update.message.reply_text(reply)
            return

        if FOOTER.strip() not in reply.strip():
            reply = reply + FOOTER

        save_context(user_id, session_id, user_message, reply, "menu", classification, youtube_links)

        if show_header:
            stats = get_stats()
            header = f"""
🏠 *مرحباً بعودتك إلى بوت الخبير العقاري!*

👥 *عدد المستخدمين الحالي:* {stats['total_users']}
📊 *آخر تحديث:* {datetime.now().strftime('%Y-%m-%d')}
"""
            await update.message.reply_text(
                header + reply,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_main_keyboard(has_youtube=has_youtube, has_save=True)
            )
        else:
            await update.message.reply_text(
                reply,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_main_keyboard(has_youtube=has_youtube, has_save=True)
            )

    except Exception as e:
        logger.error(f"❌ خطأ في handle_message: {e}")
        await update.message.reply_text(f"❌ حدث خطأ تقني: {e}")

# ======================= أوامر الإحصائيات =======================
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, _ = is_admin(user.id)
    if not is_adm and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    stats = get_stats()
    top_q = "\n".join([f"- {q[0]}: {q[1]} مرة" for q in stats["top_questions"]]) if stats["top_questions"] else "لا توجد أسئلة مسجلة."
    msg = f"""
📊 *إحصائيات البوت العقاري*

👥 *إجمالي المستخدمين:* {stats['total_users']}
🟢 *نشطاء آخر 7 أيام:* {stats['active_week']}
🟢 *نشطاء الآن (آخر 5 دقائق):* {stats['active_now']}
💬 *إجمالي الرسائل:* {stats['total_messages']}
🚫 *حالات الرفض:* {stats['total_rejections']}
📉 *معدل الرفض:* {stats['rejection_rate']}%

🔥 *أكثر 5 أسئلة تكراراً:*
{top_q}
"""
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def top_keywords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, _ = is_admin(user.id)
    if not is_adm and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    keywords = get_top_keywords(10)
    if not keywords:
        await update.message.reply_text("لا توجد كلمات مفتاحية مسجلة.")
        return
    msg = "🔑 *أكثر 10 كلمات مفتاحية استخداماً:*\n" + "\n".join([f"- {kw[0]}: {kw[1]} مرة" for kw in keywords])
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, _ = is_admin(user.id)
    if not is_adm and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("لا يوجد مستخدمون مسجلون.")
        return
    msg = f"👥 *إجمالي المستخدمين:* {len(users)}\n\n"
    for u in users[:20]:
        username = u[1] or "بدون اسم"
        first_name = u[2] or ""
        msg += f"- @{username} ({first_name}) - رسائل: {u[4]}\n"
    if len(users) > 20:
        msg += f"\n... و {len(users)-20} مستخدمين آخرين."
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, role = is_admin(user.id)
    if not is_adm or role not in ["super_admin", "admin"]:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /broadcast النص الذي تريد نشره")
        return
    broadcast_text = " ".join(args)
    users = get_all_users()
    if not users:
        await update.message.reply_text("لا يوجد مستخدمون.")
        return
    sent_count = 0
    failed_count = 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u[0], text=f"📢 *إعلان من المسؤول:*\n\n{broadcast_text}", parse_mode=ParseMode.MARKDOWN)
            sent_count += 1
        except Exception as e:
            logger.warning(f"فشل إرسال لـ {u[0]}: {e}")
            failed_count += 1
        await asyncio.sleep(0.05)
    log_audit(user.id, "broadcast", f"تم إرسال إعلان لـ {sent_count} مستخدم")
    await update.message.reply_text(f"✅ تم الإرسال لـ {sent_count} مستخدم.\n❌ فشل لـ {failed_count} مستخدم.")

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, role = is_admin(user.id)
    if not is_adm or role not in ["super_admin", "admin"]:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["النوع", "المعرف", "الاسم", "القيمة", "التكرار", "آخر تحديث"])
    users = get_all_users()
    for u in users:
        writer.writerow(["مستخدم", u[0], u[1] or u[2] or "", u[3], u[4], u[3]])
    questions = get_all_questions()
    for q in questions:
        writer.writerow(["سؤال", "", "", q[0], q[1], q[2]])
    rejections = get_all_rejections()
    for r in rejections:
        writer.writerow(["رفض", "", "", r[0], "", r[1]])
    output.seek(0)
    log_audit(user.id, "export", "تم تصدير البيانات")
    await update.message.reply_document(document=io.BytesIO(output.getvalue().encode('utf-8')), filename="bot_export.csv")

# ======================= التشغيل =======================
def main():
    init_db()
    logger.info("✅ قاعدة البيانات جاهزة.")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("removeadmin", remove_admin_command))
    app.add_handler(CommandHandler("setadminrole", set_admin_role_command))
    app.add_handler(CommandHandler("admins", admins_list_command))
    app.add_handler(CommandHandler("rule", set_rule_command))
    app.add_handler(CommandHandler("clearrule", clear_rule_command))
    app.add_handler(CommandHandler("addrule", add_rule_command))
    app.add_handler(CommandHandler("listrules", list_rules_command))
    app.add_handler(CommandHandler("showrule", show_rule_command))
    app.add_handler(CommandHandler("activerule", activate_rule_command))
    app.add_handler(CommandHandler("editrule", edit_rule_command))
    app.add_handler(CommandHandler("deleterule", delete_rule_command))
    app.add_handler(CommandHandler("clearallrules", clear_all_rules_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("top", top_keywords_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("audit", audit_log_command))
    app.add_handler(CommandHandler("saved", saved_command))
    app.add_handler(CommandHandler("save", save_response_command))
    app.add_handler(CommandHandler("preferences", preferences_command))
    
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ البوت العقاري يعمل بالنسخة النهائية (باستخدام Markdown للتنسيق).")

    async def delete_webhook():
        await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook تم حذفه")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(delete_webhook())

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
import html
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from openai import OpenAI

# ======================= تحميل المتغيرات البيئية =======================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

if not TELEGRAM_TOKEN or not GROQ_API_KEY or not GOOGLE_API_KEY:
    raise ValueError("❌ تأكد من وجود TELEGRAM_BOT_TOKEN و GROQ_API_KEY و GOOGLE_API_KEY في ملف .env")

if ADMIN_ID == 0:
    print("⚠️ تحذير: ADMIN_ID غير مضبوط. لن تعمل أوامر /broadcast و /stats و /top و /users و /export و /addadmin.")

# ======================= إعداد العملاء =======================
client_groq = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
client_gemini = OpenAI(api_key=GOOGLE_API_KEY, base_url="https://generativelanguage.googleapis.com/v1beta/openai/")

client_openrouter = None
if OPENROUTER_API_KEY:
    client_openrouter = OpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ======================= دوال تطبيع النص العربي =======================
def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    text = text.replace("إ", "ا").replace("أ", "ا").replace("آ", "ا").replace("ى", "ا")
    text = text.replace("ة", "ه")
    text = re.sub(r'[\u064B-\u0652]', '', text)
    text = re.sub(r'[،؛؟!()\[\]{}"\'.,;:?!\-]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ======================= دوال التصحيح الإملائي البسيط =======================
SPELLING_CORRECTIONS = {
    "افراغ": "إفراغ",
    "ايجار": "إيجار",
    "سكني": "سكني",
    "تجاري": "تجاري",
    "تسجيل": "تسجيل",
    "وساطة": "وساطة",
    "مزاد": "مزاد",
    "ترخيص": "ترخيص",
    "مطالبة": "مطالبة",
    "عربون": "عربون",
    "انهاء": "إنهاء",
}

def correct_spelling(text: str) -> str:
    words = text.split()
    corrected = []
    for word in words:
        if word in SPELLING_CORRECTIONS:
            corrected.append(SPELLING_CORRECTIONS[word])
        else:
            found = False
            for misspelled, correct in SPELLING_CORRECTIONS.items():
                if misspelled in word or word in misspelled:
                    corrected.append(correct)
                    found = True
                    break
            if not found:
                corrected.append(word)
    return " ".join(corrected)

# ======================= قاعدة البيانات =======================
DB_PATH = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_activity TEXT,
        total_messages INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS questions (
        question_text TEXT PRIMARY KEY,
        count INTEGER DEFAULT 1,
        last_asked TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS keywords (
        keyword TEXT PRIMARY KEY,
        count INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS rejections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_text TEXT,
        timestamp TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS conversation_context (
        user_id INTEGER,
        session_id TEXT,
        last_question TEXT,
        last_suggestion TEXT,
        last_question_time TEXT,
        clarification_stage TEXT DEFAULT 'menu',
        classification TEXT DEFAULT '',
        youtube_links TEXT DEFAULT '',
        PRIMARY KEY (user_id, session_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        secret_code TEXT,
        added_by INTEGER,
        added_date TEXT,
        role TEXT DEFAULT 'admin'
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS custom_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_name TEXT UNIQUE,
        rule_text TEXT,
        created_by INTEGER,
        created_date TEXT,
        is_active INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS qa_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_normalized TEXT UNIQUE,
        question_original TEXT,
        answer TEXT,
        source TEXT,
        created_at TEXT,
        last_used TEXT,
        usage_count INTEGER DEFAULT 1,
        expiry_date TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        user_id INTEGER,
        start_time TEXT,
        end_time TEXT,
        is_active INTEGER DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_preferences (
        user_id INTEGER PRIMARY KEY,
        city TEXT,
        property_type TEXT,
        price_range TEXT,
        preferred_contact TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS pending_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        data TEXT,
        timestamp TEXT,
        secret_code TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        details TEXT,
        timestamp TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS saved_responses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        question TEXT,
        answer TEXT,
        saved_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS faq (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_normalized TEXT UNIQUE,
        question_original TEXT,
        answer TEXT,
        source TEXT,
        created_at TEXT
    )''')
    
    c.execute("CREATE INDEX IF NOT EXISTS idx_context_user_session ON conversation_context (user_id, session_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log (user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cache_normalized ON qa_cache (question_normalized)")
    
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect(DB_PATH)

# ======================= دوال قاعدة البيانات المحدثة =======================
def save_user(user_id, username, first_name):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT OR IGNORE INTO users (user_id, username, first_name, last_activity, total_messages)
                 VALUES (?, ?, ?, ?, 0)''', (user_id, username, first_name, now))
    c.execute('''UPDATE users SET last_activity = ?, total_messages = total_messages + 1
                 WHERE user_id = ?''', (now, user_id))
    conn.commit()
    conn.close()

def get_last_activity(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT last_activity FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def update_last_activity(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("UPDATE users SET last_activity = ? WHERE user_id = ?", (now, user_id))
    conn.commit()
    conn.close()

def save_question(question_text):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT INTO questions (question_text, count, last_asked)
                 VALUES (?, 1, ?) ON CONFLICT(question_text) DO UPDATE SET
                 count = count + 1, last_asked = excluded.last_asked''', (question_text, now))
    conn.commit()
    conn.close()

def save_keywords(keywords_list):
    conn = get_db_connection()
    c = conn.cursor()
    for kw in keywords_list:
        if len(kw) < 2:
            continue
        c.execute('''INSERT INTO keywords (keyword, count) VALUES (?, 1)
                     ON CONFLICT(keyword) DO UPDATE SET count = count + 1''', (kw,))
    conn.commit()
    conn.close()

def save_rejection(question_text):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT INTO rejections (question_text, timestamp) VALUES (?, ?)''', (question_text, now))
    conn.commit()
    conn.close()

def save_context(user_id, session_id, last_question, last_suggestion, clarification_stage="menu", classification="", youtube_links=""):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    if isinstance(youtube_links, (list, dict)):
        youtube_links = json.dumps(youtube_links, ensure_ascii=False)
    c.execute('''INSERT OR REPLACE INTO conversation_context 
                 (user_id, session_id, last_question, last_suggestion, last_question_time, clarification_stage, classification, youtube_links)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', 
                 (user_id, session_id, last_question, last_suggestion, now, clarification_stage, classification, youtube_links))
    conn.commit()
    conn.close()

def get_context(user_id, session_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT last_question, last_suggestion, last_question_time, clarification_stage, classification, youtube_links 
                 FROM conversation_context
                 WHERE user_id = ? AND session_id = ?''', (user_id, session_id))
    row = c.fetchone()
    conn.close()
    if row:
        youtube_links = row[5] if row[5] else ""
        try:
            youtube_links_parsed = json.loads(youtube_links)
        except:
            youtube_links_parsed = []
        return {
            "last_question": row[0],
            "last_suggestion": row[1],
            "last_question_time": row[2],
            "clarification_stage": row[3] if row[3] else "menu",
            "classification": row[4] if row[4] else "",
            "youtube_links": youtube_links_parsed
        }
    return None

def clear_context(user_id, session_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''DELETE FROM conversation_context WHERE user_id = ? AND session_id = ?''', (user_id, session_id))
    conn.commit()
    conn.close()

def update_clarification_stage(user_id, session_id, stage):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''UPDATE conversation_context SET clarification_stage = ? 
                 WHERE user_id = ? AND session_id = ?''', (stage, user_id, session_id))
    conn.commit()
    conn.close()

# ======================= دوال الإدارة والأمان المحدثة =======================
def is_admin(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, role FROM admins WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return True, row[1] if len(row) > 1 else "admin"
    return False, None

def get_admin_secret(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT secret_code FROM admins WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_admin_role(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT role FROM admins WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_admin_role(user_id, role):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE admins SET role = ? WHERE user_id = ?", (role, user_id))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)''', (key, value))
    conn.commit()
    conn.close()

def delete_setting(key):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM bot_settings WHERE key = ?", (key,))
    conn.commit()
    conn.close()

def get_all_admins():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, username, secret_code, added_by, added_date, role FROM admins")
    rows = c.fetchall()
    conn.close()
    return rows

# ======================= دوال القواعد المتعددة =======================
def add_custom_rule(rule_name, rule_text, created_by):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT OR REPLACE INTO custom_rules (rule_name, rule_text, created_by, created_date, is_active)
                 VALUES (?, ?, ?, ?, 0)''', (rule_name, rule_text, created_by, now))
    conn.commit()
    conn.close()

def get_all_custom_rules():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, rule_name, rule_text, created_by, created_date, is_active FROM custom_rules")
    rows = c.fetchall()
    conn.close()
    return rows

def get_custom_rule(rule_name):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT rule_text FROM custom_rules WHERE rule_name = ?", (rule_name,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def update_custom_rule(rule_name, new_text):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE custom_rules SET rule_text = ? WHERE rule_name = ?", (new_text, rule_name))
    conn.commit()
    conn.close()

def delete_custom_rule(rule_name):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM custom_rules WHERE rule_name = ?", (rule_name,))
    conn.commit()
    conn.close()

def delete_all_custom_rules():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM custom_rules")
    conn.commit()
    conn.close()

def activate_rule(rule_name):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE custom_rules SET is_active = 0")
    c.execute("UPDATE custom_rules SET is_active = 1 WHERE rule_name = ?", (rule_name,))
    conn.commit()
    conn.close()

def get_active_rule():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT rule_text FROM custom_rules WHERE is_active = 1")
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

# ======================= دوال Q&A Cache مع التشابه الدلالي =======================
def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s]', '', text)
    return text

def get_cached_answer(question):
    conn = get_db_connection()
    c = conn.cursor()
    norm_q = normalize_text(question)
    c.execute("SELECT answer, question_original, expiry_date FROM qa_cache WHERE question_normalized = ?", (norm_q,))
    row = c.fetchone()
    conn.close()
    if row:
        if row[2]:
            expiry = datetime.fromisoformat(row[2])
            if datetime.now() > expiry:
                return None
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE qa_cache SET usage_count = usage_count + 1, last_used = ? WHERE question_normalized = ?",
                  (datetime.now().isoformat(), norm_q))
        conn.commit()
        conn.close()
        return row[0]
    return None

def get_semantic_cached_answer(question, threshold=0.85):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT question_normalized, question_original, answer, expiry_date FROM qa_cache")
    rows = c.fetchall()
    conn.close()
    norm_q = normalize_text(question)
    best_match = None
    best_ratio = 0.0
    for row in rows:
        stored_norm = normalize_text(row[0])
        ratio = difflib.SequenceMatcher(None, norm_q, stored_norm).ratio()
        if ratio > best_ratio and ratio >= threshold:
            best_ratio = ratio
            best_match = row
    if best_match:
        if best_match[3]:
            expiry = datetime.fromisoformat(best_match[3])
            if datetime.now() > expiry:
                return None
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE qa_cache SET usage_count = usage_count + 1, last_used = ? WHERE question_normalized = ?",
                  (datetime.now().isoformat(), best_match[0]))
        conn.commit()
        conn.close()
        return best_match[2]
    return None

def save_cached_answer(question, answer, source="المصادر الرسمية", expiry_days=30):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    expiry = (datetime.now() + timedelta(days=expiry_days)).isoformat()
    c.execute('''INSERT OR REPLACE INTO qa_cache (question_normalized, question_original, answer, source, created_at, last_used, expiry_date)
                 VALUES (?, ?, ?, ?, ?, ?, ?)''', (normalize_text(question), question, answer, source, now, now, expiry))
    conn.commit()
    conn.close()

def add_faq(question, answer, source="المصادر الرسمية"):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT OR IGNORE INTO faq (question_normalized, question_original, answer, source, created_at)
                 VALUES (?, ?, ?, ?, ?)''', (normalize_text(question), question, answer, source, now))
    conn.commit()
    conn.close()

def get_faq_answer(question):
    conn = get_db_connection()
    c = conn.cursor()
    norm_q = normalize_text(question)
    c.execute("SELECT answer FROM faq WHERE question_normalized = ?", (norm_q,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT question_normalized, answer FROM faq")
    rows = c.fetchall()
    conn.close()
    best_match = None
    best_ratio = 0.0
    for r in rows:
        ratio = difflib.SequenceMatcher(None, norm_q, r[0]).ratio()
        if ratio > best_ratio and ratio >= 0.85:
            best_ratio = ratio
            best_match = r[1]
    return best_match

# ======================= دوال الإحصائيات =======================
def get_stats():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    c.execute("SELECT COUNT(*) FROM users WHERE last_activity > ?", (week_ago,))
    active_week = c.fetchone()[0]
    five_min_ago = (datetime.now() - timedelta(minutes=5)).isoformat()
    c.execute("SELECT COUNT(*) FROM users WHERE last_activity > ?", (five_min_ago,))
    active_now = c.fetchone()[0]
    c.execute("SELECT question_text, count FROM questions ORDER BY count DESC LIMIT 5")
    top_questions = c.fetchall()
    c.execute("SELECT COUNT(*) FROM rejections")
    total_rejections = c.fetchone()[0]
    c.execute("SELECT SUM(total_messages) FROM users")
    total_messages = c.fetchone()[0] or 0
    conn.close()
    rejection_rate = (total_rejections / total_messages * 100) if total_messages > 0 else 0
    return {
        "total_users": total_users,
        "active_week": active_week,
        "active_now": active_now,
        "top_questions": top_questions,
        "total_rejections": total_rejections,
        "rejection_rate": round(rejection_rate, 2),
        "total_messages": total_messages
    }

def get_top_keywords(limit=10):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT keyword, count FROM keywords ORDER BY count DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_users():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, last_activity, total_messages FROM users")
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_questions():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT question_text, count, last_asked FROM questions ORDER BY count DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_rejections():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT question_text, timestamp FROM rejections ORDER BY timestamp DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def save_saved_response(user_id, question, answer):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT INTO saved_responses (user_id, question, answer, saved_at)
                 VALUES (?, ?, ?, ?)''', (user_id, question, answer, now))
    conn.commit()
    conn.close()

def get_saved_responses(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, question, answer, saved_at FROM saved_responses WHERE user_id = ? ORDER BY saved_at DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_saved_response(user_id, response_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM saved_responses WHERE id = ? AND user_id = ?", (response_id, user_id))
    conn.commit()
    conn.close()

def set_user_preference(user_id, city, property_type, price_range):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO user_preferences (user_id, city, property_type, price_range)
                 VALUES (?, ?, ?, ?)''', (user_id, city, property_type, price_range))
    conn.commit()
    conn.close()

def get_user_preferences(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT city, property_type, price_range FROM user_preferences WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"city": row[0], "property_type": row[1], "price_range": row[2]}
    return None

def log_audit(user_id, action, details=""):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT INTO audit_log (user_id, action, details, timestamp)
                 VALUES (?, ?, ?, ?)''', (user_id, action, details, now))
    conn.commit()
    conn.close()

def save_pending_action(user_id, action, data, secret_code):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT INTO pending_actions (user_id, action, data, timestamp, secret_code)
                 VALUES (?, ?, ?, ?, ?)''', (user_id, action, data, now, secret_code))
    conn.commit()
    conn.close()
    return c.lastrowid

def get_pending_action(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, action, data, timestamp FROM pending_actions WHERE user_id = ? ORDER BY timestamp DESC LIMIT 1", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "action": row[1], "data": row[2], "timestamp": row[3]}
    return None

def delete_pending_action(action_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM pending_actions WHERE id = ?", (action_id,))
    conn.commit()
    conn.close()

def get_session_id(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("SELECT session_id FROM sessions WHERE user_id = ? AND is_active = 1", (user_id,))
    row = c.fetchone()
    if row:
        session_id = row[0]
        c.execute("UPDATE sessions SET end_time = ? WHERE session_id = ?", (now, session_id))
        conn.commit()
        conn.close()
        return session_id
    session_id = f"{user_id}_{datetime.now().timestamp()}"
    c.execute('''INSERT INTO sessions (session_id, user_id, start_time, end_time, is_active)
                 VALUES (?, ?, ?, ?, 1)''', (session_id, user_id, now, now))
    conn.commit()
    conn.close()
    return session_id

def end_session(session_id):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("UPDATE sessions SET is_active = 0, end_time = ? WHERE session_id = ?", (now, session_id))
    conn.commit()
    conn.close()

# ======================= روابط اليوتيوب =======================
YOUTUBE_LINKS = {
    "إفراغ عقاري (بورصة)": {
        "primary": "https://youtu.be/_0a2CbfFmMA",
        "title": "شرح طريقة الإفراغ العقاري عبر البورصة العقارية"
    },
    "إفراغ عقاري (سجل عقاري)": {
        "primary": "https://youtu.be/P2ehPAcdtvg",
        "secondary": ["https://youtu.be/ERRSS-74TUA", "https://youtu.be/IVPIgNsQE4o"],
        "title": "شرح الافراغ بالسجل العقاري"
    },
    "تسجيل عيني": {
        "primary": "https://youtu.be/bnuACgiKPv8",
        "secondary": ["https://youtu.be/dGHLinBQ8Pc"],
        "title": "شرح التسجيل العيني أو تسجيل العقار عينياً"
    },
    "عقد وساطة": {
        "primary": "https://youtu.be/VcAZkaevjRg",
        "title": "شرح عمل عقد وساطة"
    },
    "عقد وساطة مع مستثمر": {
        "primary": "https://youtu.be/sKopwc3byYs",
        "title": "شرح عمل عقد وساطة مع مستثمر أو مشتري أو مستأجر"
    },
    "عقد وساطة بين وسطاء": {
        "primary": "https://youtu.be/VcAZkaevjRg",
        "title": "شرح عمل عقد وساطة بين وسيط ووسيط"
    },
    "عقد إيجار سكني": {
        "primary": "https://youtu.be/kGN4zp0NDho",
        "title": "شرح طريقة إنشاء عقد إيجار سكني بالتفاصيل"
    },
    "عقد إيجار تجاري": {
        "primary": "https://youtu.be/M1a6oLV5y6g",
        "title": "شرح طريقة إنشاء عقد إيجار تجاري بالتفاصيل"
    },
    "ترخيص مزاد عقاري": {
        "primary": "https://youtu.be/fY7BxNYE1MY",
        "title": "شرح طريقة طلب ترخيص مزاد عقاري"
    },
    "مطالبة إيجار متأخر": {
        "primary": "https://youtu.be/RgoYAtTsb-g",
        "title": "شرح طريقة المطالبة بالإيجار المتأخر وفسخ العقد وإخلاء العقار"
    },
    "دفع العربون": {
        "primary": "https://youtu.be/xUZQod_vRpQ",
        "title": "شرح طريقة دفع العربون عبر منصة الهيئة العامة للعقار"
    },
    "إنهاء عقد إيجار": {
        "primary": "https://youtu.be/KMbtdGtbKjo",
        "title": "شرح طريقة إنهاء عقد إيجار بالتراضي بين المؤجر والمستأجر"
    }
}

# ======================= دالة تنسيق روابط اليوتيوب (HTML) =======================
def format_youtube_message(youtube_links):
    if not youtube_links:
        return None
    msg = "🎥 <b>شروحات بالفيديو:</b>\n\n"
    for i, link in enumerate(youtube_links, 1):
        title = html.escape(link.get('title', 'شرح'))
        primary = html.escape(link.get('primary', ''))
        secondary = link.get('secondary', [])
        msg += f"{i}. <b>{title}</b>\n"
        msg += f"   🔗 <a href='{primary}'>{primary}</a>\n"
        if secondary:
            for j, sec in enumerate(secondary, 1):
                sec_escaped = html.escape(sec)
                msg += f"      - رابط إضافي {j}: <a href='{sec_escaped}'>{sec_escaped}</a>\n"
        msg += "\n"
    return msg

# ======================= دالة استرجاع روابط اليوتيوب المحسنة =======================
def get_youtube_links(classification: str, user_message: str = "") -> list:
    results = []
    if classification is None:
        classification = ""
    if user_message is None:
        user_message = ""
    normalized_msg = normalize_arabic(user_message)
    if classification in YOUTUBE_LINKS:
        results.append(YOUTUBE_LINKS[classification])
    for key in YOUTUBE_LINKS.keys():
        if key in classification or classification in key:
            if YOUTUBE_LINKS[key] not in results:
                results.append(YOUTUBE_LINKS[key])
    keywords_map = {
        "سجل عقاري": "إفراغ عقاري (سجل عقاري)",
        "السجل العقاري": "إفراغ عقاري (سجل عقاري)",
        "بورصة": "إفراغ عقاري (بورصة)",
        "تسجيل عيني": "تسجيل عيني",
        "تسجيل العقار": "تسجيل عيني",
        "وساطة": "عقد وساطة",
        "عقد وساطة": "عقد وساطة",
        "وسيط": "عقد وساطة",
        "إيجار سكني": "عقد إيجار سكني",
        "ايجار سكني": "عقد إيجار سكني",
        "عقد إيجار سكني": "عقد إيجار سكني",
        "عقد ايجار سكني": "عقد إيجار سكني",
        "إيجار تجاري": "عقد إيجار تجاري",
        "ايجار تجاري": "عقد إيجار تجاري",
        "عقد إيجار تجاري": "عقد إيجار تجاري",
        "عقد ايجار تجاري": "عقد إيجار تجاري",
        "مزاد": "ترخيص مزاد عقاري",
        "ترخيص مزاد": "ترخيص مزاد عقاري",
        "مطالبة": "مطالبة إيجار متأخر",
        "إخلاء": "مطالبة إيجار متأخر",
        "فسخ": "مطالبة إيجار متأخر",
        "عربون": "دفع العربون",
        "إنهاء عقد": "إنهاء عقد إيجار",
        "انهاء عقد": "إنهاء عقد إيجار",
        "إنهاء الإيجار": "إنهاء عقد إيجار",
        "انهاء الايجار": "إنهاء عقد إيجار",
        "مستثمر": "عقد وساطة مع مستثمر",
        "مشتري": "عقد وساطة مع مستثمر",
        "مستأجر": "عقد وساطة مع مستثمر",
        "وسيط ووسيط": "عقد وساطة بين وسطاء"
    }
    for key, category in keywords_map.items():
        key_norm = normalize_arabic(key)
        if key in user_message or key_norm in normalized_msg:
            if category in YOUTUBE_LINKS:
                if YOUTUBE_LINKS[category] not in results:
                    results.append(YOUTUBE_LINKS[category])
    unique_results = []
    for item in results:
        if item not in unique_results:
            unique_results.append(item)
    return unique_results

# ======================= البرومبت المختصر (معدل بتنسيق HTML) =======================
BASE_SYSTEM_PROMPT = """
أنت <b>خبير عقاري سعودي</b>، ملم بالأنظمة العقارية السعودية والمصادر الرسمية والميدانية.

🔴 <b>القاعدة الصفرية (الدور المطلق):</b>
أنت تعمل حصراً كخبير عقاري سعودي. الرد على أي سؤال غير عقاري هو: "أنا مختص بالشأن العقاري السعودي فقط. هل لديك سؤال عقاري؟"

🔴 <b>تحديد المصطلحات (إلزامي):</b>
- كلمة "مزاد" في سياق هذا البوت تعني <b>"المزاد العقاري"</b> فقط، وهو عملية بيع وشراء العقارات عبر المزاد العلني.
- كلمة "ترخيص مزاد" تعني <b>"ترخيص المزاد العقاري"</b> الصادر عن الهيئة العامة للعقار.
- أي سؤال يحتوي على "مزاد" أو "ترخيص مزاد" يُفهم على أنه عن <b>المزاد العقاري</b> وليس عن أي نوع آخر من المزادات (مثل مزاد السيارات، المزادات الحكومية، إلخ).
- إذا كان السؤال عن "مظاد" أو "صيد" أو أي مصطلح غير عقاري، يجب الرد بالجملة الثابتة: "أنا مختص بالشأن العقاري السعودي فقط. هل لديك سؤال عقاري؟"

🔴 <b>مهمتك الآن:</b>
قدّم <b>رداً مختصراً شاملاً</b> يحتوي على الأقسام التالية بوضوح (مع عناوينها):
1. <b>الجهة المعنية:</b> (مثل: الهيئة العامة للعقار، وزارة الإعلام، البلدية).
2. <b>الحكم:</b> (نعم/لا/مسموح/ممنوع).
3. <b>مختصر الشروط:</b> (أهم الشروط القانونية مختصرة، وليست مفصلة).
4. <b>مختصر المتطلبات:</b> (أهم المستندات والتراخيص مختصرة).
5. <b>مختصر الخطوات:</b> (أهم الخطوات العملية مختصرة).

<b>🔴 تعليمات البحث الإلزامية (يجب تنفيذها بدقة):</b>
- المصادر الـ16 المذكورة أدناه هي مصدرك الوحيد.
- <b>يجب أن تبحث فعلياً في هذه المصادر</b> ولا تكتفي بالقول "لم أجد معلومات".
- إذا كان السؤال عن التراخيص → ابحث في الهيئة العامة للعقار (المصدر 1) ووزارة الإعلام (المصدر 5).
- إذا كان السؤال عن الإيجار → ابحث في منصة إيجار (المصدر 2).
- إذا كان السؤال عن التسجيل العيني → ابحث في السجل العقاري (المصدر 15).
- إذا كان السؤال عن الوساطة → ابحث في نظام الوساطة (المصدر 10).
- إذا كان السؤال عن النطاقات الجغرافية → ابحث في بوابة النطاقات (المصدر 16).
- إذا كان السؤال عن المزاد العقاري أو ترخيص مزاد → ابحث في الهيئة العامة للعقار (المصدر 1).
- <b>إذا وجدت المعلومة في أي مصدر، اذكرها ولو كانت جزئية. لا تقل "لم أجد" إلا بعد التأكد من جميع المصادر.</b>
- <b>إذا لم تجد المعلومة في المصادر الـ16، اعتذر بصدق ولا تختلق معلومات من معرفتك العامة.</b>

## المصادر المعتمدة (16 مصدراً):
[النوع الأول – الرسمية والتشريعية]
.1 الهيئة العامة للعقار (rega.gov.sa)
.2 منصة إيجار (ejar.sa)
.3 منصة سكني (sakani.sa)
.4 البلديات وأمانات المناطق
.5 وزارة الإعلام (media.gov.sa) – رخصة "موثوق"
.6 الجريدة الرسمية (أم القرى)
.7 الحسابات الرسمية الموثقة
.8 وزارة الإعلام
.9 وزارة البلديات والإسكان
.10 نظام الوساطة العقارية (م/130)
.11 اللائحة التنظيمية للتسويق والإعلانات العقارية
[النوع الثاني – الميدانية]
.12 عقار، بيوت السعوديه، ديل، وصلت، حراج
.13 حسابات الوسطاء الموثقة
.14 أي مصدر عقاري سعودي معروف
.15 منصة السجل العقاري (rer.sa)
.16 بوابة النطاقات الجغرافية (saudiproperties.rega.gov.sa/zones)

## التنسيق المطلوب:
- ابدأ بـ "📌 <b>الإجابة المختصرة:</b>"
- ثم اذكر الأقسام الخمسة بالترتيب مع عناوينها كما هو مطلوب أعلاه.
- لا تذكر التفاصيل الكاملة (الشروط التفصيلية، المتطلبات الكاملة، الخطوات التفصيلية) هنا.
- <b>بعد الإجابة، أضف سطراً فارغاً، ثم هذا النص بالخط العريض:</b>
  
<b>🔍 اختر من الأزرار أدناه للحصول على التفاصيل أو الشرح بالفيديو:</b>

عند بدء التشغيل: "تفضل: هل لديك سؤال عقاري؟"
"""

# ======================= التذييل =======================
FOOTER = """

-------
**تمت بدعم من:** 
*سلطان آل ناجد العسيري*
المرجع المعلوماتي للوسيط العقاري
https://linktr.ee/sultan.al3siry
*(كدعم معلوماتي وتطبيقي للوسطاء العقاريين من خلال المصادر الرسمية، وليس استشارة استثمارية أو قانونية أو ترخيصاً.)*
**"الوسيط هو المسؤول الوحيد عن امتثال أعماله للأنظمة والتشريعات السعودية"**
"""

# ======================= دوال الذكاء الاصطناعي =======================
def is_api_error(response_text: str) -> bool:
    error_indicators = [
        "Error code:", "API key", "PERMISSION_DENIED", "API_KEY_SERVICE_BLOCKED",
        "Quota exceeded", "Requested entity was not found", "Resource has been exhausted",
        "The model is temporarily unavailable", "429", "500", "503",
        "فشل", "❌", "HTTPError", "Unauthorized", "Forbidden", "timeout"
    ]
    return any(indicator.lower() in response_text.lower() for indicator in error_indicators)

# ======================= التصنيف المحسن =======================
def classify_question(user_message: str) -> str:
    corrected = correct_spelling(user_message)
    normalized = normalize_arabic(corrected)
    try:
        response = client_groq.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": """أنت خبير تصنيف أسئلة عقارية. صنف السؤال إلى واحدة من هذه الفئات فقط:
- 'عقد وساطة': عن عقود الوساطة (مثل: عقد وساطة، وساطة عقارية، عمولة وساطة)
- 'عقد وساطة مع مستثمر': عن وساطة مع مستثمر أو مشتري أو مستأجر
- 'عقد وساطة بين وسطاء': عن وساطة بين وسيط ووسيط
- 'عقد إيجار سكني': عن عقود الإيجار السكني (مثل: إيجار سكني، عقد إيجار سكني)
- 'عقد إيجار تجاري': عن عقود الإيجار التجاري (مثل: إيجار تجاري، عقد إيجار تجاري)
- 'تسجيل عيني': عن التسجيل العيني في السجل العقاري (مثل: تسجيل عقار، تسجيل عيني، سجل عقاري)
- 'إفراغ عقاري': عن الإفراغ العقاري أو البورصة العقارية (مثل: افراغ، إفراغ، نقل ملكية، بورصة)
- 'إفراغ بالسجل العقاري': عن الافراغ عبر السجل العقاري (مثل: افراغ سجل عقاري، افراغ بالسجل)
- 'إعلان': عن الإعلان في وسائل التواصل
- 'ترخيص مزاد عقاري': عن تراخيص المزادات العقارية (مثل: مزاد عقاري، ترخيص مزاد)
- 'مطالبة إيجار متأخر': عن المطالبة بالإيجار المتأخر والفسخ والإخلاء
- 'دفع العربون': عن دفع العربون
- 'إنهاء عقد إيجار': عن إنهاء عقد الإيجار بالتراضي
- 'طلب توضيح': يطلب شرحاً لرد سابق
- 'سؤال عام': لأي سؤال عقاري آخر

أمثلة:
- سؤال: "كيف افرغ عقار عبر البورصة؟" → إفراغ عقاري
- سؤال: "ما هي خطوات التسجيل العيني؟" → تسجيل عيني
- سؤال: "عقد وساطة مع مستثمر" → عقد وساطة مع مستثمر
- سؤال: "ماذا أفعل إذا تأخر المستأجر؟" → مطالبة إيجار متأخر

أجب فقط باسم الفئة بدون أي إضافات."""},
                {"role": "user", "content": normalized}
            ],
            temperature=0.1,
            max_tokens=30
        )
        classification = response.choices[0].message.content.strip()
        logger.info(f"📊 التصنيف: {classification}")
        return classification
    except Exception as e:
        logger.warning(f"⚠️ فشل التصنيف: {e}")
        if "افراغ" in normalized or "إفراغ" in normalized:
            if "سجل عقاري" in normalized:
                return "إفراغ بالسجل العقاري"
            return "إفراغ عقاري"
        if "تسجيل" in normalized or "عيني" in normalized:
            return "تسجيل عيني"
        if "وساطة" in normalized or "عقد وساطة" in normalized:
            return "عقد وساطة"
        if "إيجار سكني" in normalized or "ايجار سكني" in normalized:
            return "عقد إيجار سكني"
        if "إيجار تجاري" in normalized or "ايجار تجاري" in normalized:
            return "عقد إيجار تجاري"
        if "مزاد" in normalized:
            return "ترخيص مزاد عقاري"
        if "مطالبة" in normalized or "إخلاء" in normalized or "فسخ" in normalized:
            return "مطالبة إيجار متأخر"
        if "عربون" in normalized:
            return "دفع العربون"
        if "إنهاء عقد" in normalized or "انهاء عقد" in normalized:
            return "إنهاء عقد إيجار"
        return "سؤال عام"

# ======================= توليد الرد المختصر =======================
def get_ai_summary_response(user_message: str, user_id: int = None) -> str:
    non_real_estate_keywords = [
        "قصة", "تاريخ", "ذو القرنين", "ديني", "ثقافي", "أدبي", "شعر", "رواية",
        "قصيدة", "نثر", "خيال", "علمي", "فلك", "نجوم", "فيزياء",
        "كيمياء", "أحياء", "طب", "جراحة", "علاج", "دواء", "موسيقى", "غناء",
        "فن", "رسم", "نحت", "هندسة", "برمجة", "حاسوب", "ذكاء اصطناعي",
        "مظاد", "صيد", "سلاح"
    ]
    if any(kw in user_message for kw in non_real_estate_keywords):
        return "أنا مختص بالشأن العقاري السعودي فقط. هل لديك سؤال عقاري؟"

    active_rule = get_active_rule()
    base_prompt = active_rule if active_rule else BASE_SYSTEM_PROMPT

    preferences = ""
    if user_id:
        prefs = get_user_preferences(user_id)
        if prefs:
            preferences = f"\n\n<b>تفضيلات المستخدم:</b> المدينة: {prefs['city'] or 'غير محدد'}, نوع العقار: {prefs['property_type'] or 'غير محدد'}, النطاق السعري: {prefs['price_range'] or 'غير محدد'}.\nيمكنك تخصيص الرد بناءً على هذه التفضيلات إذا كانت ذات صلة."

    enhanced_prompt = base_prompt + preferences + """

🔴 <b>تذكير إضافي:</b> 
- المصادر الـ16 هي مرجعك الوحيد.
- ابحث فيها جميعاً، واستخرج المعلومات حتى لو كانت جزئية.
- إذا وجدت معلومة في مصدر ميداني (مثل عقار، حراج)، اذكرها مع تحذير "مصدر ميداني".
- لا تخرج عن المصادر، ولا تختلق معلومات.
- إذا كانت المعلومة موجودة في أكثر من مصدر، اذكر المصادر كلها.
- تذكر: كلمة "مزاد" تعني "مزاد عقاري" فقط.
"""

    try:
        logger.info("⚡ توليد الرد المختصر...")
        response = client_groq.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": enhanced_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.2,
            max_tokens=700
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            if "🔍 اختر من الأزرار أدناه" not in reply:
                reply = reply + "\n\n<b>🔍 اختر من الأزرار أدناه للحصول على التفاصيل أو الشرح بالفيديو:</b>"
            return reply
    except Exception as e:
        logger.warning(f"⚠️ فشل توليد الرد المختصر (Groq): {e}")

    try:
        logger.info("🔄 باستخدام Gemini للرد المختصر...")
        response = client_gemini.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[
                {"role": "system", "content": enhanced_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.2,
            max_tokens=700
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            if "🔍 اختر من الأزرار أدناه" not in reply:
                reply = reply + "\n\n<b>🔍 اختر من الأزرار أدناه للحصول على التفاصيل أو الشرح بالفيديو:</b>"
            return reply
    except Exception as e:
        logger.warning(f"⚠️ فشل Gemini: {e}")

    return "❌ عذراً، جميع خدمات الذكاء الاصطناعي غير متاحة حالياً. يرجى المحاولة لاحقاً."

# ======================= توليد الأقسام التفصيلية =======================
def get_section_response(user_message: str, section: str) -> str:
    section_prompts = {
        "source": """أعطِ فقط الاقتباسات الحرفية من المصادر الرسمية مع رابط كل مصدر.
- ابحث في المصادر الـ16.
- انسخ النص الرسمي بين علامتي تنصيص كما هو.
- اذكر رابط المصدر بعد كل اقتباس.
- إذا وجدت أكثر من مصدر، اذكرها جميعاً.
- لا تختلق معلومات، ولا تقل "لم أجد" قبل البحث في جميع المصادر.""",
        "requirements": """أعطِ فقط قائمة المتطلبات (المستندات، التراخيص، الإجراءات المطلوبة) بشكل منظم ونقطي.
- اعتمد على المصادر الـ16.
- اذكر كل متطلب مع مصدره.
- لا تكرر الشروط أو الخطوات هنا.""",
        "conditions": """أعطِ فقط قائمة الشروط القانونية والتنظيمية بشكل منظم ونقطي.
- اعتمد على المصادر الـ16.
- اذكر كل شرط مع مصدره.
- لا تكرر المتطلبات أو الخطوات هنا.""",
        "steps": """أعطِ فقط الخطوات العملية التي يجب اتخاذها بشكل منظم ومتسلسل.
- اعتمد على المصادر الـ16.
- اذكر كل خطوة مع مصدرها.
- لا تكرر الشروط أو المتطلبات هنا.""",
        "procedures": """أعطِ فقط المعلومات المتعلقة بـ:
1. <b>الجهات المعنية:</b> اذكر الجهات الرسمية المختصة (الهيئة العامة للعقار، منصة إيجار، السجل العقاري، البلديات، وزارة الإعلام) مع شرح مختصر عن دور كل جهة.
2. <b>الرسوم والضرائب:</b> اذكر الرسوم المطلوبة (رسوم الهيئة، رسوم التوثيق، رسوم البلدية، الضرائب العقارية) مع المبالغ إن وجدت في المصادر الـ16.

<b>تحذير:</b> لا تذكر أي معلومات عن وزارة العدل، الغرفة التجارية، البنوك، أو أي جهة غير مدرجة في المصادر الـ16. إذا لم تجد المعلومات في المصادر الـ16، اعتذر ولا تختلق."""
    }
    instruction = section_prompts.get(section, "أعطِ التفاصيل المطلوبة فقط مع المصادر.")
    system_prompt = f"""
أنت خبير عقاري سعودي. مصدرك الوحيد هو المصادر الـ16 المذكورة سابقاً.
المستخدم يسأل عن: {user_message}

{instruction}

🔴 تذكير: ابحث في جميع المصادر الـ16 قبل الإجابة. إذا وجدت المعلومة ولو جزئياً، اذكرها مع المصدر. لا تقل "لم أجد" إلا بعد التأكد من جميع المصادر.
"""
    try:
        logger.info(f"⚡ توليد قسم: {section} (محاولة 1)")
        response = client_groq.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.2,
            max_tokens=1200
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            return reply
        else:
            logger.warning(f"⚠️ استجابة API تحتوي على خطأ: {reply[:100]}")
    except Exception as e:
        logger.warning(f"⚠️ فشل توليد القسم {section} (Groq): {e}")

    try:
        logger.info(f"🔄 باستخدام Gemini للقسم {section} (محاولة 2)")
        response = client_gemini.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.2,
            max_tokens=1200
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            return reply
        else:
            logger.warning(f"⚠️ استجابة Gemini تحتوي على خطأ: {reply[:100]}")
    except Exception as e:
        logger.warning(f"⚠️ فشل Gemini للقسم {section}: {e}")

    if client_openrouter:
        try:
            logger.info(f"🔄 باستخدام OpenRouter للقسم {section} (محاولة 3)")
            response = client_openrouter.chat.completions.create(
                model="meta-llama/llama-3.1-8b-instruct",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                temperature=0.2,
                max_tokens=1200
            )
            reply = response.choices[0].message.content
            if not is_api_error(reply):
                return reply
        except Exception as e:
            logger.warning(f"⚠️ فشل OpenRouter للقسم {section}: {e}")

    return f"❌ عذراً، لم أتمكن من استرجاع تفاصيل '{section}' بسبب مشكلة في الاتصال بخدمات الذكاء الاصطناعي. يرجى المحاولة لاحقاً، أو استخدام الأزرار الأخرى."

# ======================= دوال التأكيد بالرقم السري =======================
pending_secret_requests = {}

async def request_secret_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, data: dict):
    user_id = update.effective_user.id
    secret = get_admin_secret(user_id)
    if not secret:
        await update.message.reply_text("❌ ليس لديك صلاحية كمدير.")
        return
    save_pending_action(user_id, action, json.dumps(data), secret)
    pending_secret_requests[user_id] = {
        "action": action,
        "data": data,
        "timestamp": datetime.now()
    }
    await update.message.reply_text(
        f"⚠️ <b>تأكيد الأمان:</b>\n"
        f"أنت على وشك تنفيذ أمر حساس: <code>{action}</code>.\n"
        f"الرجاء إدخال الرقم السري الخاص بك لتأكيد العملية.",
        parse_mode=ParseMode.HTML
    )

async def handle_secret_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text.strip()

    if user_id not in pending_secret_requests:
        return

    pending = pending_secret_requests[user_id]
    if (datetime.now() - pending["timestamp"]).total_seconds() > 300:
        del pending_secret_requests[user_id]
        await update.message.reply_text("⏳ انتهت صلاحية طلب التأكيد.")
        return

    stored_secret = get_admin_secret(user_id)
    if not stored_secret:
        del pending_secret_requests[user_id]
        await update.message.reply_text("❌ ليس لديك صلاحية كمدير.")
        return

    if user_message == stored_secret:
        action = pending["action"]
        data = pending["data"]
        del pending_secret_requests[user_id]
        pending_record = get_pending_action(user_id)
        if pending_record:
            delete_pending_action(pending_record["id"])

        if action == "set_rule":
            new_rule = data["rule_text"]
            delete_setting("custom_rule")
            add_custom_rule("active_rule", new_rule, user_id)
            activate_rule("active_rule")
            log_audit(user_id, "set_rule", f"تم تحديث القاعدة بـ: {new_rule[:50]}...")
            await update.message.reply_text("✅ تم تحديث القاعدة بنجاح!")
        elif action == "add_rule":
            add_custom_rule(data["rule_name"], data["rule_text"], user_id)
            log_audit(user_id, "add_rule", f"تم إضافة قاعدة: {data['rule_name']}")
            await update.message.reply_text(f"✅ تم إضافة القاعدة '{data['rule_name']}'.")
        elif action == "edit_rule":
            update_custom_rule(data["rule_name"], data["new_text"])
            log_audit(user_id, "edit_rule", f"تم تعديل قاعدة: {data['rule_name']}")
            await update.message.reply_text(f"✅ تم تعديل القاعدة '{data['rule_name']}'.")
        elif action == "delete_rule":
            delete_custom_rule(data["rule_name"])
            log_audit(user_id, "delete_rule", f"تم حذف قاعدة: {data['rule_name']}")
            await update.message.reply_text(f"✅ تم حذف القاعدة '{data['rule_name']}'.")
        elif action == "clear_all_rules":
            delete_all_custom_rules()
            log_audit(user_id, "clear_all_rules", "تم حذف جميع القواعد المخصصة")
            await update.message.reply_text("✅ تم حذف جميع القواعد المخصصة.")
        else:
            await update.message.reply_text("❌ إجراء غير معروف.")
    else:
        await update.message.reply_text("❌ الرقم السري غير صحيح.")

# ======================= أوامر الإدارة =======================
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❗ استخدم: /addadmin @username الرمز_السري [role]")
        return

    username = args[0].replace("@", "")
    secret = args[1]
    role = args[2] if len(args) > 2 else "admin"

    try:
        user_obj = await context.bot.get_chat(username)
        user_id = user_obj.id
    except:
        await update.message.reply_text("❌ لم أجد هذا المستخدم.")
        return

    hashed_secret = hashlib.sha256(secret.encode()).hexdigest()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO admins (user_id, username, secret_code, added_by, added_date, role)
                 VALUES (?, ?, ?, ?, ?, ?)''', (user_id, username, hashed_secret, user.id, datetime.now().isoformat(), role))
    conn.commit()
    conn.close()

    log_audit(user.id, "add_admin", f"تم إضافة {username} كمدير")
    await update.message.reply_text(f"✅ تم إضافة {username} كمدير بنجاح!")

async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /removeadmin @username")
        return

    username = args[0].replace("@", "")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM admins WHERE username = ?", (username,))
    conn.commit()
    deleted = c.rowcount > 0
    conn.close()

    if deleted:
        log_audit(user.id, "remove_admin", f"تم حذف {username} من المدراء")
        await update.message.reply_text(f"✅ تم حذف {username} من قائمة المدراء.")
    else:
        await update.message.reply_text(f"❌ لم أجد {username} في قائمة المدراء.")

async def admins_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, _ = is_admin(user.id)
    if not is_adm and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return

    admins = get_all_admins()
    if not admins:
        await update.message.reply_text("لا يوجد مدراء مسجلون.")
        return

    msg = "📋 <b>قائمة المدراء:</b>\n\n"
    for a in admins:
        role = a[5] if len(a) > 5 else "admin"
        msg += f"- @{a[1]} (دور: {role})\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def set_admin_role_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❗ استخدم: /setadminrole @username role (admin/super_admin/moderator)")
        return

    username = args[0].replace("@", "")
    role = args[1]

    if role not in ["admin", "super_admin", "moderator"]:
        await update.message.reply_text("❌ دور غير صالح. الأدوار المتاحة: admin, super_admin, moderator")
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE admins SET role = ? WHERE username = ?", (role, username))
    conn.commit()
    if c.rowcount == 0:
        await update.message.reply_text(f"❌ لم أجد {username} في قائمة المدراء.")
    else:
        log_audit(user.id, "set_admin_role", f"تم تغيير دور {username} إلى {role}")
        await update.message.reply_text(f"✅ تم تغيير دور {username} إلى {role}.")
    conn.close()

# ======================= أوامر القواعد =======================
async def set_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /rule النص_الجديد للقاعدة")
        return

    new_rule = " ".join(args)
    await request_secret_confirmation(update, context, "set_rule", {"rule_text": new_rule})

async def clear_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    delete_setting("custom_rule")
    delete_all_custom_rules()
    log_audit(user.id, "clear_rule", "تم إلغاء القاعدة المخصصة")
    await update.message.reply_text("✅ تم إلغاء القاعدة المخصصة، والعودة إلى القاعدة الافتراضية.")

async def add_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❗ استخدم: /addrule اسم_القاعدة النص")
        return

    rule_name = args[0]
    rule_text = " ".join(args[1:])
    await request_secret_confirmation(update, context, "add_rule", {"rule_name": rule_name, "rule_text": rule_text})

async def list_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, _ = is_admin(user.id)
    if not is_adm and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return

    rules = get_all_custom_rules()
    if not rules:
        await update.message.reply_text("لا توجد قواعد مخصصة.")
        return

    msg = "📋 <b>قائمة القواعد المخصصة:</b>\n\n"
    for r in rules:
        status = "✅ (نشطة)" if r[5] == 1 else "⏸ (غير نشطة)"
        msg += f"- <b>{r[1]}</b> {status}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def show_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, _ = is_admin(user.id)
    if not is_adm and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /showrule اسم_القاعدة")
        return

    rule_name = args[0]
    rule_text = get_custom_rule(rule_name)
    if not rule_text:
        await update.message.reply_text(f"❌ لم أجد قاعدة باسم '{rule_name}'.")
        return

    await update.message.reply_text(f"📜 <b>نص القاعدة '{rule_name}':</b>\n\n{rule_text}", parse_mode=ParseMode.HTML)

async def activate_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /activerule اسم_القاعدة")
        return

    rule_name = args[0]
    if not get_custom_rule(rule_name):
        await update.message.reply_text(f"❌ لم أجد قاعدة باسم '{rule_name}'.")
        return

    activate_rule(rule_name)
    log_audit(user.id, "activate_rule", f"تم تفعيل قاعدة: {rule_name}")
    await update.message.reply_text(f"✅ تم تفعيل القاعدة '{rule_name}' بنجاح.")

async def edit_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❗ استخدم: /editrule اسم_القاعدة النص_الجديد")
        return

    rule_name = args[0]
    new_text = " ".join(args[1:])
    if not get_custom_rule(rule_name):
        await update.message.reply_text(f"❌ لم أجد قاعدة باسم '{rule_name}'.")
        return

    await request_secret_confirmation(update, context, "edit_rule", {"rule_name": rule_name, "new_text": new_text})

async def delete_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /deleterule اسم_القاعدة")
        return

    rule_name = args[0]
    if not get_custom_rule(rule_name):
        await update.message.reply_text(f"❌ لم أجد قاعدة باسم '{rule_name}'.")
        return

    await request_secret_confirmation(update, context, "delete_rule", {"rule_name": rule_name})

async def clear_all_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمسؤول الأساسي فقط.")
        return

    await request_secret_confirmation(update, context, "clear_all_rules", {})

# ======================= أوامر المستخدم =======================
async def saved_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    saved = get_saved_responses(user.id)
    if not saved:
        await update.message.reply_text("ليس لديك أي ردود محفوظة.")
        return
    msg = "📚 <b>الردود المحفوظة:</b>\n\n"
    for s in saved[:10]:
        safe_q = html.escape(s[1])
        safe_a = html.escape(s[2][:100])
        msg += f"<b>{safe_q}</b>\n{safe_a}...\n\n"
    if len(saved) > 10:
        msg += f"... و {len(saved)-10} ردود أخرى."
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def save_response_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    session_id = get_session_id(user.id)
    context_data = get_context(user.id, session_id)
    if not context_data:
        await update.message.reply_text("لا يوجد رد لحفظه. اسأل سؤالاً أولاً.")
        return
    last_q = context_data.get("last_question")
    last_a = context_data.get("last_suggestion")
    if not last_q or not last_a:
        await update.message.reply_text("لا يوجد رد لحفظه.")
        return
    save_saved_response(user.id, last_q, last_a)
    await update.message.reply_text("✅ تم حفظ الرد للرجوع إليه لاحقاً. استخدم /saved لعرض المحفوظات.")

async def preferences_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("❗ استخدم: /preferences المدينة نوع_العقار النطاق_السعري\nمثال: /preferences الرياض سكني 500-1000")
        return
    city = args[0]
    property_type = args[1]
    price_range = " ".join(args[2:])
    set_user_preference(user.id, city, property_type, price_range)
    await update.message.reply_text("✅ تم حفظ تفضيلاتك بنجاح.")

async def audit_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, role = is_admin(user.id)
    if not is_adm or role not in ["super_admin", "admin"]:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id, action, details, timestamp FROM audit_log ORDER BY timestamp DESC LIMIT 20")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("لا يوجد سجل للعمليات.")
        return
    msg = "📋 <b>سجل العمليات الإدارية (آخر 20):</b>\n\n"
    for r in rows:
        safe_details = html.escape(r[2])
        msg += f"- [{r[3]}] {r[1]}: {safe_details}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ======================= بناء لوحة المفاتيح التفاعلية (بدون الأزرار الجديدة) =======================
def get_main_keyboard(has_youtube: bool = False, has_save: bool = False):
    """إرجاع لوحة المفاتيح مع الأزرار الأساسية فقط (بدون عرض الكل، رجوع، مشاركة)."""
    keyboard = []
    
    if has_youtube:
        keyboard.append([InlineKeyboardButton("🎥 شرح باليوتيوب", callback_data="show_youtube")])
    
    keyboard.append([InlineKeyboardButton("📌 الرد المختصر", callback_data="show_summary"),
                     InlineKeyboardButton("📄 التفاصيل من المصادر", callback_data="detail_source")])
    keyboard.append([InlineKeyboardButton("📋 المتطلبات", callback_data="detail_requirements"),
                     InlineKeyboardButton("⚖️ الشروط", callback_data="detail_conditions")])
    keyboard.append([InlineKeyboardButton("📝 الخطوات", callback_data="detail_steps"),
                     InlineKeyboardButton("🛠️ الإجراءات التنظيمية", callback_data="detail_procedures")])
    
    if has_save:
        keyboard.append([InlineKeyboardButton("💾 حفظ الرد", callback_data="save_response")])
    
    keyboard.append([InlineKeyboardButton("❓ سؤال عقاري آخر", callback_data="ask_another")])
    keyboard.append([InlineKeyboardButton("❓ هل هذه الإجابة مفيدة؟", callback_data="dummy_feedback")])
    keyboard.append([InlineKeyboardButton("✅ نعم", callback_data="feedback_yes"),
                     InlineKeyboardButton("❌ لا", callback_data="feedback_no")])
    
    return InlineKeyboardMarkup(keyboard)

# ======================= معالج الأزرار المحدث (بدون الأزرار الجديدة) =======================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data
    session_id = get_session_id(user_id)

    context_data = get_context(user_id, session_id)
    last_q = context_data.get("last_question") if context_data else None
    last_summary = context_data.get("last_suggestion") if context_data else None
    classification = context_data.get("classification") if context_data else None
    youtube_links = context_data.get("youtube_links") if context_data else []

    # ====== زر "شرح باليوتيوب" ======
    if data == "show_youtube":
        if last_q is None:
            last_q = ""
        
        links = youtube_links if isinstance(youtube_links, list) else []
        
        if not links and last_q:
            links = get_youtube_links(classification, last_q)
            logger.info(f"🔄 فولباك: تم إعادة حساب الروابط لـ: {last_q[:30]}... => {len(links)} رابط")
        
        if links:
            msg = format_youtube_message(links)
            if msg:
                msg += FOOTER
                await query.edit_message_text(
                    msg,
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_main_keyboard(has_youtube=True, has_save=True)
                )
            else:
                await query.edit_message_text(
                    "❌ لم أجد شرحاً بالفيديو لهذا الموضوع حالياً.",
                    reply_markup=get_main_keyboard()
                )
        else:
            await query.edit_message_text(
                "❌ لم أجد شرحاً بالفيديو لهذا الموضوع حالياً.",
                reply_markup=get_main_keyboard()
            )

    # ====== زر "الرد المختصر" ======
    elif data == "show_summary":
        if last_summary:
            has_youtube = len(youtube_links) > 0 if isinstance(youtube_links, list) else False
            safe_summary = html.escape(last_summary)
            await query.edit_message_text(
                safe_summary,
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard(has_youtube=has_youtube, has_save=True)
            )
        else:
            await query.edit_message_text("لم أجد رداً مختصراً سابقاً. اطرح سؤالاً جديداً.")

    # ====== أزرار التفاصيل (5 أقسام) ======
    elif data in ["detail_source", "detail_requirements", "detail_conditions", "detail_steps", "detail_procedures"]:
        section_map = {
            "detail_source": "source",
            "detail_requirements": "requirements",
            "detail_conditions": "conditions",
            "detail_steps": "steps",
            "detail_procedures": "procedures"
        }
        section = section_map.get(data)
        if last_q and section:
            reply = get_section_response(last_q, section)
            safe_reply = html.escape(reply)
            safe_reply += FOOTER
            has_youtube = len(youtube_links) > 0 if isinstance(youtube_links, list) else False
            await query.edit_message_text(
                safe_reply,
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard(has_youtube=has_youtube, has_save=True)
            )
        else:
            await query.edit_message_text("لم أجد سؤالاً سابقاً.")

    # ====== زر "حفظ الرد" ======
    elif data == "save_response":
        if last_q and last_summary:
            save_saved_response(user_id, last_q, last_summary)
            await query.edit_message_text(
                "✅ تم حفظ الرد بنجاح! يمكنك استعراضه لاحقاً باستخدام /saved",
                reply_markup=get_main_keyboard(has_save=True)
            )
        else:
            await query.edit_message_text("لا يوجد رد لحفظه حالياً.")

    # ====== زر "سؤال عقاري آخر" ======
    elif data == "ask_another":
        clear_context(user_id, session_id)
        end_session(session_id)
        await context.bot.send_message(
            chat_id=user_id,
            text="تفضل طال عمرك.. هل لديك سؤال عقاري آخر؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗺️ النطاقات الجغرافية", callback_data="zones")],
                [InlineKeyboardButton("📌 المرجع الرئيسي", url="https://saudiproperties.rega.gov.sa")],
                [InlineKeyboardButton("📞 الدعم واتساب", url="https://wa.me/966568708086")]
            ])
        )
        await query.edit_message_reply_markup(reply_markup=None)

    # ====== زر وهمي ======
    elif data == "dummy_feedback":
        pass

    # ====== زر "النطاقات الجغرافية" ======
    elif data == "zones":
        zones_msg = """
🗺️ <b>النطاقات الجغرافية الجديدة (تحديث 2026)</b>

🔗 <b>المرجع الرسمي:</b> https://saudiproperties.rega.gov.sa/zones

📌 <b>المناطق المذكورة (13):</b>
الرياض، مكة، المدينة، القصيم، الشرقية، عسير، تبوك، حائل، الحدود الشمالية، جازان، نجران، الباحة، الجوف.

🏗️ <b>المشاريع المذكورة:</b>
• نيوم، البحر الأحمر، أمالا
• الرياض: القدية، المربع الجديد، المسار الرياضي، بوابة الدرعية، حديقة الملك سلمان، سدرة، كافد، مطار الملك سلمان
• جدة: أبتاون، العروس، وسط جدة
• مكة: أبراج مكة، المنار، برج أجياد، بوابة الملك سلمان، جبل عمر، ذاخر مكة
• المدينة: الغرة، المهوى، دار الهجرة، داون تاون المدينة

⚖️ <b>قواعد أساسية:</b>
• التملك داخل النطاقات المذكورة فقط
• مكة والمدينة: للمسلمين فقط
• الرياض وجدة: مناطق محددة
• المقيم: يحق له عقار سكني واحد خارج النطاقات

📞 للاستفسار: 920017183
"""
        await query.edit_message_text(
            zones_msg,
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )

    # ====== أزرار التقييم ======
    elif data == "feedback_yes":
        if last_q and last_summary:
            save_cached_answer(last_q, last_summary, "المصادر الرسمية")
        await context.bot.send_message(
            chat_id=user_id,
            text="شكراً! تم حفظ هذه الإجابة للاستخدام المستقبلي.\n\nسم طال عمرك.. هل عندك سؤال عقاري آخر؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗺️ النطاقات الجغرافية", callback_data="zones")],
                [InlineKeyboardButton("📌 المرجع الرئيسي", url="https://saudiproperties.rega.gov.sa")],
                [InlineKeyboardButton("📞 الدعم واتساب", url="https://wa.me/966568708086")]
            ])
        )
        clear_context(user_id, session_id)
        await query.edit_message_reply_markup(reply_markup=None)

    elif data == "feedback_no":
        await context.bot.send_message(
            chat_id=user_id,
            text="شكراً لمشاركتك.\n\nسم طال عمرك.. هل عندك سؤال عقاري آخر؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗺️ النطاقات الجغرافية", callback_data="zones")],
                [InlineKeyboardButton("📌 المرجع الرئيسي", url="https://saudiproperties.rega.gov.sa")],
                [InlineKeyboardButton("📞 الدعم واتساب", url="https://wa.me/966568708086")]
            ])
        )
        clear_context(user_id, session_id)
        await query.edit_message_reply_markup(reply_markup=None)

    # ====== الأزرار القديمة ======
    elif data in ["clarify_conditions", "clarify_requirements", "clarify_steps", "clarify_all", "clarify_other", "confirm_yes", "confirm_no"]:
        await query.edit_message_text(
            "🔄 تم تحديث نظام البوت. الرجاء استخدام الأزرار الجديدة للحصول على المعلومات المطلوبة.",
            reply_markup=get_main_keyboard()
        )
        clear_context(user_id, session_id)

# ======================= دوال البوت =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.username, user.first_name)
    session_id = get_session_id(user.id)

    stats = get_stats()

    keyboard = [
        [InlineKeyboardButton("🗺️ النطاقات الجغرافية", callback_data="zones")],
        [InlineKeyboardButton("📌 المرجع الرئيسي", url="https://saudiproperties.rega.gov.sa")],
        [InlineKeyboardButton("📞 الدعم واتساب", url="https://wa.me/966568708086")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = f"""
📊 <b>إحصائيات البوت:</b>
━━━━━━━━━━━━━━━━━━━
🟢 <b>المستخدمين الحاليين (آخر 5 دقائق):</b> {stats['active_now']}
📈 <b>النشطين (آخر 7 أيام):</b> {stats['active_week']}
📊 <b>جميع المستخدمين (منذ البداية):</b> {stats['total_users']}
━━━━━━━━━━━━━━━━━━━

🔒 تطمن، لا يمكن لأحد الاطلاع على محادثاتك.
خصوصيتك أمانة في أعناقنا.

📢 <b>للتواصل مع المسؤول:</b>
- /report للإبلاغ عن مشكلة
- /suggest لتقديم اقتراح
- /complain لتقديم شكوى

❓ <b>سم طال عمرك.. هل لديك سؤال عقاري؟</b>
"""
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    user_message = update.message.text.strip()

    if user_id in pending_secret_requests:
        await handle_secret_confirmation(update, context)
        return

    save_user(user_id, user.username, user.first_name)
    session_id = get_session_id(user_id)

    last_activity = get_last_activity(user_id)
    show_header = False
    if last_activity:
        try:
            last_time = datetime.fromisoformat(last_activity)
            time_diff = datetime.now() - last_time
            if time_diff.total_seconds() > 7200:
                show_header = True
        except:
            pass
    update_last_activity(user_id)

    save_question(user_message)
    keywords = [word for word in user_message.split() if len(word) > 2]
    save_keywords(keywords)

    corrected = correct_spelling(user_message)
    normalized = normalize_arabic(corrected)

    # FAQ
    faq_answer = get_faq_answer(normalized)
    if faq_answer:
        logger.info(f"✅ تم الاسترجاع من FAQ لـ: {user_message}")
        await update.message.reply_text(faq_answer, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())
        return

    # Cache
    cached = get_semantic_cached_answer(user_message)
    if cached:
        logger.info(f"✅ تم الاسترجاع من التخزين المؤقت الدلالي لـ: {user_message}")
        await update.message.reply_text(cached, parse_mode=ParseMode.HTML, reply_markup=get_main_keyboard())
        return

    # Keyword classification
    keyword_classification = None
    if "افراغ" in normalized or "إفراغ" in normalized:
        if "سجل عقاري" in normalized:
            keyword_classification = "إفراغ بالسجل العقاري"
        else:
            keyword_classification = "إفراغ عقاري"
    elif "تسجيل عيني" in normalized or "تسجيل العقار" in normalized or "تسجيل عقار" in normalized or "عينيا" in normalized:
        keyword_classification = "تسجيل عيني"
    elif "وساطة" in normalized or "عقد وساطة" in normalized:
        if "بين وسيط" in normalized or "وسيط ووسيط" in normalized:
            keyword_classification = "عقد وساطة بين وسطاء"
        elif "مستثمر" in normalized or "مشتري" in normalized or "مستأجر" in normalized:
            keyword_classification = "عقد وساطة مع مستثمر"
        else:
            keyword_classification = "عقد وساطة"
    elif "إيجار سكني" in normalized or "ايجار سكني" in normalized:
        keyword_classification = "عقد إيجار سكني"
    elif "إيجار تجاري" in normalized or "ايجار تجاري" in normalized:
        keyword_classification = "عقد إيجار تجاري"
    elif "مزاد" in normalized:
        keyword_classification = "ترخيص مزاد عقاري"
    elif "مطالبة" in normalized or "إخلاء" in normalized or "فسخ" in normalized:
        keyword_classification = "مطالبة إيجار متأخر"
    elif "عربون" in normalized:
        keyword_classification = "دفع العربون"
    elif "إنهاء عقد" in normalized or "انهاء عقد" in normalized or "إنهاء الإيجار" in normalized:
        keyword_classification = "إنهاء عقد إيجار"

    if keyword_classification:
        classification = keyword_classification
        logger.info(f"📊 التصنيف (من المرشح): {classification}")
    else:
        classification = classify_question(user_message)
        logger.info(f"📊 التصنيف (من النموذج): {classification}")

    youtube_links = get_youtube_links(classification, user_message)
    has_youtube = len(youtube_links) > 0

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        
        reply = get_ai_summary_response(user_message, user_id)

        is_apology = "أنا مختص بالشأن العقاري السعودي فقط" in reply
        if is_apology:
            save_rejection(user_message)
            await update.message.reply_text(reply)
            return

        if FOOTER.strip() not in reply.strip():
            reply = reply + FOOTER

        save_context(user_id, session_id, user_message, reply, "menu", classification, youtube_links)

        if show_header:
            stats = get_stats()
            header = f"""
🏠 <b>مرحباً بعودتك إلى بوت الخبير العقاري!</b>

👥 <b>عدد المستخدمين الحالي:</b> {stats['total_users']}
📊 <b>آخر تحديث:</b> {datetime.now().strftime('%Y-%m-%d')}
"""
            await update.message.reply_text(
                header + reply,
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard(has_youtube=has_youtube, has_save=True)
            )
        else:
            await update.message.reply_text(
                reply,
                parse_mode=ParseMode.HTML,
                reply_markup=get_main_keyboard(has_youtube=has_youtube, has_save=True)
            )

    except Exception as e:
        logger.error(f"❌ خطأ في handle_message: {e}")
        await update.message.reply_text(f"❌ حدث خطأ تقني: {e}")

# ======================= أوامر الإحصائيات =======================
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, _ = is_admin(user.id)
    if not is_adm and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    stats = get_stats()
    top_q = "\n".join([f"- {q[0]}: {q[1]} مرة" for q in stats["top_questions"]]) if stats["top_questions"] else "لا توجد أسئلة مسجلة."
    msg = f"""
📊 <b>إحصائيات البوت العقاري</b>

👥 <b>إجمالي المستخدمين:</b> {stats['total_users']}
🟢 <b>نشطاء آخر 7 أيام:</b> {stats['active_week']}
🟢 <b>نشطاء الآن (آخر 5 دقائق):</b> {stats['active_now']}
💬 <b>إجمالي الرسائل:</b> {stats['total_messages']}
🚫 <b>حالات الرفض:</b> {stats['total_rejections']}
📉 <b>معدل الرفض:</b> {stats['rejection_rate']}%

🔥 <b>أكثر 5 أسئلة تكراراً:</b>
{top_q}
"""
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def top_keywords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, _ = is_admin(user.id)
    if not is_adm and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    keywords = get_top_keywords(10)
    if not keywords:
        await update.message.reply_text("لا توجد كلمات مفتاحية مسجلة.")
        return
    msg = "🔑 <b>أكثر 10 كلمات مفتاحية استخداماً:</b>\n" + "\n".join([f"- {kw[0]}: {kw[1]} مرة" for kw in keywords])
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, _ = is_admin(user.id)
    if not is_adm and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("لا يوجد مستخدمون مسجلون.")
        return
    msg = f"👥 <b>إجمالي المستخدمين:</b> {len(users)}\n\n"
    for u in users[:20]:
        username = u[1] or "بدون اسم"
        first_name = u[2] or ""
        msg += f"- @{username} ({first_name}) - رسائل: {u[4]}\n"
    if len(users) > 20:
        msg += f"\n... و {len(users)-20} مستخدمين آخرين."
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, role = is_admin(user.id)
    if not is_adm or role not in ["super_admin", "admin"]:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /broadcast النص الذي تريد نشره")
        return
    broadcast_text = " ".join(args)
    users = get_all_users()
    if not users:
        await update.message.reply_text("لا يوجد مستخدمون.")
        return
    sent_count = 0
    failed_count = 0
    for u in users:
        try:
            safe_text = html.escape(broadcast_text)
            await context.bot.send_message(chat_id=u[0], text=f"📢 <b>إعلان من المسؤول:</b>\n\n{safe_text}", parse_mode=ParseMode.HTML)
            sent_count += 1
        except Exception as e:
            logger.warning(f"فشل إرسال لـ {u[0]}: {e}")
            failed_count += 1
        await asyncio.sleep(0.05)
    log_audit(user.id, "broadcast", f"تم إرسال إعلان لـ {sent_count} مستخدم")
    await update.message.reply_text(f"✅ تم الإرسال لـ {sent_count} مستخدم.\n❌ فشل لـ {failed_count} مستخدم.")

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_adm, role = is_admin(user.id)
    if not is_adm or role not in ["super_admin", "admin"]:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["النوع", "المعرف", "الاسم", "القيمة", "التكرار", "آخر تحديث"])
    users = get_all_users()
    for u in users:
        writer.writerow(["مستخدم", u[0], u[1] or u[2] or "", u[3], u[4], u[3]])
    questions = get_all_questions()
    for q in questions:
        writer.writerow(["سؤال", "", "", q[0], q[1], q[2]])
    rejections = get_all_rejections()
    for r in rejections:
        writer.writerow(["رفض", "", "", r[0], "", r[1]])
    output.seek(0)
    log_audit(user.id, "export", "تم تصدير البيانات")
    await update.message.reply_document(document=io.BytesIO(output.getvalue().encode('utf-8')), filename="bot_export.csv")

# ======================= التشغيل =======================
def main():
    init_db()
    logger.info("✅ قاعدة البيانات جاهزة.")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("removeadmin", remove_admin_command))
    app.add_handler(CommandHandler("setadminrole", set_admin_role_command))
    app.add_handler(CommandHandler("admins", admins_list_command))
    app.add_handler(CommandHandler("rule", set_rule_command))
    app.add_handler(CommandHandler("clearrule", clear_rule_command))
    app.add_handler(CommandHandler("addrule", add_rule_command))
    app.add_handler(CommandHandler("listrules", list_rules_command))
    app.add_handler(CommandHandler("showrule", show_rule_command))
    app.add_handler(CommandHandler("activerule", activate_rule_command))
    app.add_handler(CommandHandler("editrule", edit_rule_command))
    app.add_handler(CommandHandler("deleterule", delete_rule_command))
    app.add_handler(CommandHandler("clearallrules", clear_all_rules_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("top", top_keywords_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("audit", audit_log_command))
    app.add_handler(CommandHandler("saved", saved_command))
    app.add_handler(CommandHandler("save", save_response_command))
    app.add_handler(CommandHandler("preferences", preferences_command))
    
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ البوت العقاري يعمل بالنسخة النهائية (بدون أزرار عرض الكل، رجوع، مشاركة، مع إصلاح تنسيق HTML).")

    async def delete_webhook():
        await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook تم حذفه")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(delete_webhook())

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
