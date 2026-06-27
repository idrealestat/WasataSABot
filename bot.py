import os
import logging
import sqlite3
import csv
import io
import asyncio
import json
import re
from functools import lru_cache
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
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
FREELLM_API_KEY = os.getenv("FREELLM_API_KEY")

# ======================= إعداد التسجيل =======================
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ======================= إعداد العملاء =======================
client_groq = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
client_gemini = OpenAI(api_key=GOOGLE_API_KEY, base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
client_openrouter = OpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1") if OPENROUTER_API_KEY else None
client_freellm = OpenAI(api_key=FREELLM_API_KEY, base_url="https://api.freellm.com/v1") if FREELLM_API_KEY else None

# ======================= قاعدة البيانات =======================
DB_PATH = "bot_data.db"

def get_db_connection():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_activity TEXT, total_messages INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS questions (question_text TEXT PRIMARY KEY, count INTEGER DEFAULT 1, last_asked TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS keywords (keyword TEXT PRIMARY KEY, count INTEGER DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS rejections (id INTEGER PRIMARY KEY AUTOINCREMENT, question_text TEXT, timestamp TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS conversation_context (user_id INTEGER PRIMARY KEY, last_question TEXT, last_suggestion TEXT, last_question_time TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, username TEXT, secret_code TEXT, added_by INTEGER, added_date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS custom_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_name TEXT UNIQUE, rule_text TEXT, created_by INTEGER, created_date TEXT, is_active INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS unanswered_questions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, question_text TEXT, timestamp TEXT, is_notified INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS youtube_cache (query TEXT PRIMARY KEY, results TEXT, timestamp TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS qa_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, question_normalized TEXT UNIQUE, question_original TEXT, answer TEXT, source TEXT, created_at TEXT, last_used TEXT, usage_count INTEGER DEFAULT 1)''')
    conn.commit()
    conn.close()

# ============================================================
#                      طبقة المستودع (Repository)
# ============================================================

class QaCacheRepository:
    @staticmethod
    def get(question_norm: str) -> Optional[str]:
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT answer FROM qa_cache WHERE question_normalized = ?", (question_norm,))
        row = c.fetchone(); conn.close()
        if row:
            conn = get_db_connection(); c = conn.cursor()
            c.execute("UPDATE qa_cache SET last_used = ?, usage_count = usage_count + 1 WHERE question_normalized = ?", (datetime.now().isoformat(), question_norm))
            conn.commit(); conn.close()
            return row[0]
        return None
    @staticmethod
    def save(question_norm: str, question_orig: str, answer: str, source: str = "المصادر الرسمية"):
        conn = get_db_connection(); c = conn.cursor(); now = datetime.now().isoformat()
        c.execute('''INSERT OR REPLACE INTO qa_cache (question_normalized, question_original, answer, source, created_at, last_used) VALUES (?, ?, ?, ?, ?, ?)''', (question_norm, question_orig, answer, source, now, now))
        conn.commit(); conn.close()

class UserRepository:
    @staticmethod
    def save(user_id, username, first_name):
        conn = get_db_connection(); c = conn.cursor(); now = datetime.now().isoformat()
        c.execute('''INSERT OR IGNORE INTO users (user_id, username, first_name, last_activity, total_messages) VALUES (?, ?, ?, ?, 0)''', (user_id, username, first_name, now))
        c.execute('''UPDATE users SET last_activity = ?, total_messages = total_messages + 1 WHERE user_id = ?''', (now, user_id))
        conn.commit(); conn.close()
    @staticmethod
    def get_last_activity(user_id):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT last_activity FROM users WHERE user_id = ?", (user_id,)); row = c.fetchone(); conn.close(); return row[0] if row else None
    @staticmethod
    def update_activity(user_id):
        conn = get_db_connection(); c = conn.cursor(); now = datetime.now().isoformat()
        c.execute("UPDATE users SET last_activity = ? WHERE user_id = ?", (now, user_id)); conn.commit(); conn.close()

class QuestionRepository:
    @staticmethod
    def save(question):
        conn = get_db_connection(); c = conn.cursor(); now = datetime.now().isoformat()
        c.execute('''INSERT INTO questions (question_text, count, last_asked) VALUES (?, 1, ?) ON CONFLICT(question_text) DO UPDATE SET count = count + 1, last_asked = excluded.last_asked''', (question, now))
        conn.commit(); conn.close()
    @staticmethod
    def get_top(limit=5):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT question_text, count FROM questions ORDER BY count DESC LIMIT ?", (limit,)); rows = c.fetchall(); conn.close(); return rows

class KeywordRepository:
    @staticmethod
    def save(keywords):
        conn = get_db_connection(); c = conn.cursor()
        for kw in keywords:
            if len(kw) < 2: continue
            c.execute('''INSERT INTO keywords (keyword, count) VALUES (?, 1) ON CONFLICT(keyword) DO UPDATE SET count = count + 1''', (kw,))
        conn.commit(); conn.close()
    @staticmethod
    def get_top(limit=10):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT keyword, count FROM keywords ORDER BY count DESC LIMIT ?", (limit,)); rows = c.fetchall(); conn.close(); return rows

class RejectionRepository:
    @staticmethod
    def save(question):
        conn = get_db_connection(); c = conn.cursor(); now = datetime.now().isoformat()
        c.execute("INSERT INTO rejections (question_text, timestamp) VALUES (?, ?)", (question, now)); conn.commit(); conn.close()
    @staticmethod
    def count():
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM rejections"); count = c.fetchone()[0]; conn.close(); return count

class ContextRepository:
    @staticmethod
    def get(user_id):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT last_question, last_suggestion, last_question_time FROM conversation_context WHERE user_id = ?", (user_id,))
        row = c.fetchone(); conn.close()
        return {"last_question": row[0], "last_suggestion": row[1], "last_question_time": row[2]} if row else None
    @staticmethod
    def save(user_id, last_question, last_suggestion):
        conn = get_db_connection(); c = conn.cursor(); now = datetime.now().isoformat()
        c.execute('''INSERT OR REPLACE INTO conversation_context (user_id, last_question, last_suggestion, last_question_time) VALUES (?, ?, ?, ?)''', (user_id, last_question, last_suggestion, now))
        conn.commit(); conn.close()
    @staticmethod
    def clear(user_id):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM conversation_context WHERE user_id = ?", (user_id,)); conn.commit(); conn.close()

class AdminRepository:
    @staticmethod
    def is_admin(user_id):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,)); row = c.fetchone(); conn.close(); return row is not None
    @staticmethod
    def get_secret(user_id):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT secret_code FROM admins WHERE user_id = ?", (user_id,)); row = c.fetchone(); conn.close(); return row[0] if row else None
    @staticmethod
    def add(user_id, username, secret, added_by):
        conn = get_db_connection(); c = conn.cursor(); now = datetime.now().isoformat()
        c.execute('''INSERT OR REPLACE INTO admins (user_id, username, secret_code, added_by, added_date) VALUES (?, ?, ?, ?, ?)''', (user_id, username, secret, added_by, now))
        conn.commit(); conn.close()
    @staticmethod
    def remove(username):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM admins WHERE username = ?", (username,)); deleted = c.rowcount > 0; conn.commit(); conn.close(); return deleted
    @staticmethod
    def get_all():
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT user_id, username, secret_code FROM admins"); rows = c.fetchall(); conn.close(); return rows

class RuleRepository:
    @staticmethod
    def get_active():
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT rule_text FROM custom_rules WHERE is_active = 1"); row = c.fetchone(); conn.close(); return row[0] if row else None
    @staticmethod
    def get_all():
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT id, rule_name, rule_text, created_by, created_date, is_active FROM custom_rules"); rows = c.fetchall(); conn.close(); return rows
    @staticmethod
    def get_text(name):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT rule_text FROM custom_rules WHERE rule_name = ?", (name,)); row = c.fetchone(); conn.close(); return row[0] if row else None
    @staticmethod
    def add(name, text, created_by):
        conn = get_db_connection(); c = conn.cursor(); now = datetime.now().isoformat()
        c.execute('''INSERT OR REPLACE INTO custom_rules (rule_name, rule_text, created_by, created_date, is_active) VALUES (?, ?, ?, ?, 0)''', (name, text, created_by, now))
        conn.commit(); conn.close()
    @staticmethod
    def update(name, new_text):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE custom_rules SET rule_text = ? WHERE rule_name = ?", (new_text, name)); conn.commit(); conn.close()
    @staticmethod
    def delete(name):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM custom_rules WHERE rule_name = ?", (name,)); conn.commit(); conn.close()
    @staticmethod
    def delete_all():
        conn = get_db_connection(); c = conn.cursor()
        c.execute("DELETE FROM custom_rules"); conn.commit(); conn.close()
    @staticmethod
    def activate(name):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE custom_rules SET is_active = 0"); c.execute("UPDATE custom_rules SET is_active = 1 WHERE rule_name = ?", (name,)); conn.commit(); conn.close()

class UnansweredRepository:
    @staticmethod
    def save(user_id, username, question):
        conn = get_db_connection(); c = conn.cursor(); now = datetime.now().isoformat()
        c.execute('''INSERT INTO unanswered_questions (user_id, username, question_text, timestamp, is_notified) VALUES (?, ?, ?, ?, 0)''', (user_id, username, question, now))
        conn.commit(); conn.close()
    @staticmethod
    def mark_notified(question_id):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("UPDATE unanswered_questions SET is_notified = 1 WHERE id = ?", (question_id,)); conn.commit(); conn.close()

class YouTubeCacheRepository:
    @staticmethod
    def get(query):
        conn = get_db_connection(); c = conn.cursor(); week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        c.execute("SELECT results FROM youtube_cache WHERE query = ? AND timestamp > ?", (query, week_ago)); row = c.fetchone(); conn.close(); return json.loads(row[0]) if row else None
    @staticmethod
    def save(query, results):
        conn = get_db_connection(); c = conn.cursor(); now = datetime.now().isoformat()
        c.execute('''INSERT OR REPLACE INTO youtube_cache (query, results, timestamp) VALUES (?, ?, ?)''', (query, json.dumps(results), now)); conn.commit(); conn.close()

# ============================================================
#                      طبقة الخدمات
# ============================================================

class AIService:
    PROVIDERS = [
        {"name": "Groq", "client": client_groq, "model": "llama-3.3-70b-versatile"},
        {"name": "FreeLLM", "client": client_freellm, "model": "free-model"},
        {"name": "OpenRouter", "client": client_openrouter, "model": "google/gemini-2.5-flash"},
        {"name": "Gemini", "client": client_gemini, "model": "gemini-2.5-flash"},
    ]
    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt
    def generate(self, user_message: str) -> str:
        for provider in self.PROVIDERS:
            if provider["client"] is None: continue
            try:
                logger.info(f"⚡ باستخدام {provider['name']}...")
                response = provider["client"].chat.completions.create(
                    model=provider["model"],
                    messages=[{"role": "system", "content": self.system_prompt}, {"role": "user", "content": user_message}],
                    temperature=0.2, max_tokens=3500
                )
                reply = response.choices[0].message.content
                if not self._is_error(reply): return reply
            except Exception as e: logger.warning(f"⚠️ فشل {provider['name']}: {e}")
        return "❌ عذراً، جميع خدمات الذكاء الاصطناعي غير متاحة حالياً. يرجى المحاولة لاحقاً."
    def _is_error(self, text): return any(e in text.lower() for e in ["error", "api key", "quota", "429", "500", "503", "timeout"])

class YouTubeService:
    def search(self, query, max_results=3):
        logger.info("⏸️ البحث في يوتيوب معطل مؤقتاً.")
        return []

class ContextService:
    def __init__(self): self._cache = {}
    def get(self, user_id):
        if user_id in self._cache: return self._cache[user_id]
        ctx = ContextRepository.get(user_id); 
        if ctx: self._cache[user_id] = ctx
        return ctx
    def update(self, user_id, last_question, last_suggestion):
        ContextRepository.save(user_id, last_question, last_suggestion)
        self._cache[user_id] = {"last_question": last_question, "last_suggestion": last_suggestion, "last_question_time": datetime.now().isoformat()}
    def clear(self, user_id):
        ContextRepository.clear(user_id)
        if user_id in self._cache: del self._cache[user_id]
    def is_educational(self, msg): return any(k in msg for k in ["كيف", "طريقة", "شرح", "خطوات", "تعليم"])
    def is_correction(self, msg): return any(k in msg for k in ["خطأ", "غير صحيح", "ليس صحيح", "غلط", "صحح"])

class AdminService:
    def __init__(self, admin_id): self.admin_id = admin_id
    def is_owner(self, uid): return uid == self.admin_id
    def is_admin(self, uid): return self.is_owner(uid) or AdminRepository.is_admin(uid)
    def get_secret(self, uid): return AdminRepository.get_secret(uid)
    def add_admin(self, uid, username, secret, added_by): AdminRepository.add(uid, username, secret, added_by)
    def remove_admin(self, username): return AdminRepository.remove(username)
    def get_all(self): return AdminRepository.get_all()

class RulesService:
    def __init__(self, base_prompt): self.base_prompt = base_prompt; self._active = None; self._cache = {}
    def get_active_prompt(self):
        if self._active is None:
            active = RuleRepository.get_active()
            self._active = active if active else self.base_prompt
        return self._active
    def activate(self, name):
        text = RuleRepository.get_text(name)
        if not text: return False
        RuleRepository.activate(name); self._active = text; return True
    def add(self, name, text, created_by): RuleRepository.add(name, text, created_by); self._cache[name] = text
    def update(self, name, new_text): RuleRepository.update(name, new_text); self._cache[name] = new_text
    def delete(self, name): RuleRepository.delete(name); 
    def delete_all(self): RuleRepository.delete_all(); self._cache.clear(); self._active = None
    def get_all(self): return RuleRepository.get_all()
    def get_text(self, name): return self._cache.get(name) or RuleRepository.get_text(name)

# ============================================================
#                      البرومبت الأساسي (معدل)
# ============================================================

BASE_SYSTEM_PROMPT = """
🔴 **قاعدة أساسية لا تُكسر:**
أنت خبير عقاري سعودي، ومصدرك الوحيد هو المصادر الـ16 المذكورة أدناه.
**إذا لم تجد المعلومة في هذه المصادر، اعتذر.** لا تخمن، لا تفترض، لا تختلق.

🔴 **رخصة "موثوق":**
لأي سؤال عن الإعلان في وسائل التواصل الاجتماعي، **رخصة "موثوق" من وزارة الإعلام شرط أساسي لا يمكن تجاوزه**. هذه المعلومة موجودة في المصدر رقم 5.

🔴 **قاعدة إلزامية لعرض المتطلبات والخطوات:**
عند الإجابة عن أي سؤال يتعلق بإجراء (مثل: تسجيل، نقل ملكية، إفراغ، إلخ)، يجب عرض العناصر التالية في قسم "التفصيل" بشكل منفصل وواضح:
- **المتطلبات:** (المستندات، الشروط، الأهلية، إلخ).
- **الخطوات:** (الإجراءات بالترتيب).
- **المساحات المشروطة:** (إن وجدت).
- **الرسوم:** (إن وجدت).
- **التنبيهات:** (تحذيرات، استثناءات).
**إذا لم تتوفر معلومة لأي عنصر، اكتب: "لا تتوفر معلومات عن [اسم العنصر] في المصادر المعتمدة."**

🔴 **شخصيتك الحوارية:**
أنت خبير عقاري سعودي تتحدث كإنسان خبير، وليس كروبوت. تفهم السياق، وتتذكر المحادثة. إذا كان السؤال غير واضح، تسأل للتوضيح. إذا قال المستخدم "خطأ"، تعترف وتصحح.

## المصادر المعتمدة (16 مصدراً):
1. الهيئة العامة للعقار (rega.gov.sa)
2. منصة إيجار (ejar.sa)
3. منصة سكني (sakani.sa)
4. البلديات وأمانات المناطق
5. وزارة الإعلام (media.gov.sa) - وتشمل رخصة "موثوق"
6. الجريدة الرسمية (أم القرى)
7. الحسابات الرسمية الموثقة للجهات
8. وزارة الإعلام
9. وزارة البلديات والإسكان
10. نظام الوساطة العقارية (م/130)
11. اللائحة التنظيمية للتسويق والإعلانات العقارية
12. عقار، بيوت السعوديه، ديل، وصلت، حراج
13. حسابات الوسطاء الموثقة
14. أي مصدر عقاري سعودي معروف
15. منصة السجل العقاري (rer.sa)
16. بوابة النطاقات الجغرافية (saudiproperties.rega.gov.sa/zones)

## مهمتك:
- ابدأ بـ **"الإجابة باختصار:"** مع الحكم والشرط.
- ثم **"التفصيل:"** مع النص الحرفي من المصدر والرابط.
- حدد **درجة الموثوقية:** (عالية / متوسطة / ميدانية).
- أنهِ بـ **"خلاصة:"**.
- لا تخرج عن المصادر، وإذا لم تجد المعلومة اعتذر.

عند بدء التشغيل: "تفضل: هل لديك اي سؤال عقاري ؟"
"""

# ============================================================
#                      معالجات الأوامر والرسائل
# ============================================================

ai_service = None
youtube_service = None
context_service = None
admin_service = None
rules_service = None
pending_secret_requests = {}

FOOTER = """

-------
***تمت بدعم من: **سلطان آل ناجد العسيري**
المرجع المعلوماتي للوسيط العقاري
https://linktr.ee/sultan.al3siry
**(كدعم معلوماتي وتطبيقي للوسطاء العقاريين من خلال المصادر الرسمية، وليس استشارة استثمارية أو قانونية أو ترخيصاً. الوسيط هو المسؤول الوحيد عن امتثال أعماله للأنظمة والتشريعات السعودية)**
"""

def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s]', '', text)
    return text

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    UserRepository.save(user.id, user.username, user.first_name)
    stats = StatsRepository.get_stats()
    keyboard = [
        [InlineKeyboardButton("🗺️ النطاقات الجغرافية", callback_data="zones")],
        [InlineKeyboardButton("📌 المرجع الرئيسي", url="https://saudiproperties.rega.gov.sa")],
        [InlineKeyboardButton("📞 الدعم واتساب", url="https://wa.me/966568708086")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = f"""
📊 **إحصائيات البوت:**
━━━━━━━━━━━━━━━━━━━
🟢 **المستخدمين الحاليين (آخر 5 دقائق):** {stats['active_now']}
📈 **النشطين (آخر 7 أيام):** {stats['active_week']}
📊 **جميع المستخدمين (منذ البداية):** {stats['total_users']}
━━━━━━━━━━━━━━━━━━━

🔒 تطمن، لا يمكن لأحد الاطلاع على محادثاتك.
خصوصيتك أمانة في أعناقنا.

📢 **للتواصل مع المسؤول:**
- /report للإبلاغ عن مشكلة
- /suggest لتقديم اقتراح
- /complain لتقديم شكوى

❓ **سم طال عمرك.. هل لديك سؤال عقاري؟**
"""
    await update.message.reply_text(msg, parse_mode=None, reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data == "zones":
        await query.edit_message_text("🗺️ النطاقات الجغرافية...", parse_mode=None)

    # ====== أزرار اختيار نوع العقد ======
    elif data == "contract_type_brokerage":
        context_service.update(user_id, "عقد وساطة", "تم اختيار عقد وساطة")
        await query.edit_message_text("✅ تم الاختيار: **عقد وساطة**. جاري البحث في المصادر...")
        detailed_prompt = "المستخدم يسأل عن عقد وساطة عقارية. ابحث في المصادر الـ16 (باستثناء منصة إيجار)، مع التركيز على نظام الوساطة العقارية (م/130)، وقدم الإجابة كاملة مع المتطلبات والخطوات."
        reply = ai_service.generate(detailed_prompt)
        if FOOTER not in reply: reply += FOOTER
        await context.bot.send_message(chat_id=user_id, text=reply, parse_mode=None)
        
        # ====== إضافة سؤال التقييم ======
        if len(reply) > 500:
            keyboard = [
                [InlineKeyboardButton("✅ نعم", callback_data="feedback_yes")],
                [InlineKeyboardButton("❌ لا", callback_data="feedback_no")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(chat_id=user_id, text="هل أفادتك هذه الإجابة؟", reply_markup=reply_markup)
            context_service.update(user_id, "عقد وساطة", "تقييم الإجابة")

    elif data == "contract_type_rent":
        context_service.update(user_id, "عقد إيجار", "تم اختيار عقد إيجار")
        await query.edit_message_text("✅ تم الاختيار: **عقد إيجار**. جاري البحث في المصادر...")
        detailed_prompt = "المستخدم يسأل عن عقد إيجار. ابحث في المصادر الـ16 مع التركيز على منصة إيجار، والهيئة العامة للعقار، والمصادر الميدانية، وقدم الإجابة كاملة مع المتطلبات والخطوات."
        reply = ai_service.generate(detailed_prompt)
        if FOOTER not in reply: reply += FOOTER
        await context.bot.send_message(chat_id=user_id, text=reply, parse_mode=None)
        
        # ====== إضافة سؤال التقييم ======
        if len(reply) > 500:
            keyboard = [
                [InlineKeyboardButton("✅ نعم", callback_data="feedback_yes")],
                [InlineKeyboardButton("❌ لا", callback_data="feedback_no")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(chat_id=user_id, text="هل أفادتك هذه الإجابة؟", reply_markup=reply_markup)
            context_service.update(user_id, "عقد إيجار", "تقييم الإجابة")

    # ====== أزرار التقييم (نعم / لا) ======
    elif data == "feedback_yes":
        context_data = context_service.get(user_id)
        if context_data:
            last_q = context_data.get("last_question")
            if last_q:
                reply = ai_service.generate(last_q)
                QaCacheRepository.save(normalize_text(last_q), last_q, reply, "المصادر الرسمية")
                await query.edit_message_text("✅ شكراً! تم حفظ هذه الإجابة للاستخدام المستقبلي.")
                context_service.clear(user_id)

    elif data == "feedback_no":
        context_data = context_service.get(user_id)
        if context_data:
            last_q = context_data.get("last_question")
            if last_q:
                await query.edit_message_text("🔄 آسف. دعني أرجع إلى المصادر لأقدم لك إجابة أفضل.")
                new_reply = ai_service.generate(f"أعد صياغة الإجابة على: {last_q} مع التأكد من المصادر الـ16")
                if FOOTER not in new_reply: new_reply += FOOTER
                await context.bot.send_message(chat_id=user_id, text=new_reply, parse_mode=None)
                context_service.clear(user_id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        user_id = user.id
        user_message = update.message.text.strip()
        normalized_q = normalize_text(user_message)

        logger.info(f"📩 رسالة من @{user.username}: {user_message[:50]}...")
        UserRepository.save(user_id, user.username, user.first_name)
        UserRepository.update_activity(user_id)
        QuestionRepository.save(user_message)
        KeywordRepository.save([w for w in user_message.split() if len(w)>2])

        if user_id in pending_secret_requests:
            await handle_secret_confirmation(update, context); return

        context_data = context_service.get(user_id)
        if context_data and context_service.is_correction(user_message):
            await update.message.reply_text("شكراً للتصحيح. دعني أرجع إلى المصادر لأتأكد من المعلومة الصحيحة.")
            corrected_prompt = f"المستخدم يقول أن الإجابة السابقة عن '{context_data['last_question']}' كانت خاطئة. يرجى البحث في المصادر الـ16 وتقديم الإجابة الصحيحة."
            reply = ai_service.generate(corrected_prompt)
            if FOOTER not in reply: reply += FOOTER
            await update.message.reply_text(reply, parse_mode=None)
            context_service.clear(user_id)
            return

        cached_answer = QaCacheRepository.get(normalized_q)
        if cached_answer:
            logger.info(f"✅ إجابة مخزنة لـ: {user_message}")
            await update.message.reply_text(cached_answer, parse_mode=None)
            return

        # ====== معالجة السياق (للتقييم أو التفاصيل) ======
        should_use_context = False
        if context_data:
            last_suggestion = context_data.get("last_suggestion")
            last_time = context_data.get("last_question_time")
            if last_suggestion and last_time:
                try:
                    if (datetime.now() - datetime.fromisoformat(last_time)).total_seconds() < 300:
                        should_use_context = True
                except: pass

        if should_use_context and any(w in user_message for w in ["نعم", "ايوه", "اجل", "أريد", "ابغى", "تفضل"]):
            detailed_prompt = f"المستخدم يسأل: {context_data['last_question']}\nويريد الآن التفاصيل الكاملة مع المتطلبات والخطوات."
            reply = ai_service.generate(detailed_prompt)
            if FOOTER not in reply: reply += FOOTER
            await update.message.reply_text(reply, parse_mode=None)
            context_service.clear(user_id)
            return

        # ====== إذا كان السؤال يحتوي على كلمة "عقد" وليس محدداً، نعرض أزراراً ======
        if "عقد" in user_message and not ("وساطة" in user_message or "إيجار" in user_message):
            keyboard = [
                [InlineKeyboardButton("📄 عقد وساطة", callback_data="contract_type_brokerage")],
                [InlineKeyboardButton("📄 عقد إيجار", callback_data="contract_type_rent")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("هل تقصد **عقد وساطة** أم **عقد إيجار**؟", reply_markup=reply_markup)
            context_service.update(user_id, user_message, "طلب توضيح نوع العقد")
            return

        # ====== الرد العادي (سؤال عام) ======
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        system_prompt = rules_service.get_active_prompt()
        ai_service.system_prompt = system_prompt

        ai_reply = await asyncio.to_thread(ai_service.generate, user_message)

        if "أنا مختص بالشأن العقاري السعودي فقط" in ai_reply:
            RejectionRepository.save(user_message)
            await update.message.reply_text(ai_reply, parse_mode=None)
            return

        if FOOTER not in ai_reply: ai_reply += FOOTER

        if "هل تريد" in ai_reply:
            lines = ai_reply.split("\n")
            for line in reversed(lines):
                if "هل تريد" in line:
                    context_service.update(user_id, user_message, line); break

        # ====== إرسال الرد + أزرار التقييم ======
        await update.message.reply_text(ai_reply, parse_mode=None)
        if len(ai_reply) > 500:
            keyboard = [
                [InlineKeyboardButton("✅ نعم", callback_data="feedback_yes")],
                [InlineKeyboardButton("❌ لا", callback_data="feedback_no")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("هل أفادتك هذه الإجابة؟", reply_markup=reply_markup)
            context_service.update(user_id, user_message, "تقييم الإجابة")

    except Exception as e:
        logger.error(f"❌ خطأ: {e}", exc_info=True)
        try:
            if ADMIN_ID:
                await context.bot.send_message(ADMIN_ID, f"⚠️ خطأ في البوت:\n{str(e)[:200]}")
            await update.message.reply_text("❌ عذراً، حدث خطأ داخلي. يرجى المحاولة لاحقاً.", parse_mode=None)
        except: pass

async def handle_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # تم نقل هذه الوظيفة إلى button_callback
    pass

# ============================================================
#                      الإحصائيات والوظائف الأخرى
# ============================================================

class StatsRepository:
    @staticmethod
    def get_stats():
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users"); total_users = c.fetchone()[0]
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        c.execute("SELECT COUNT(*) FROM users WHERE last_activity > ?", (week_ago,)); active_week = c.fetchone()[0]
        five_min_ago = (datetime.now() - timedelta(minutes=5)).isoformat()
        c.execute("SELECT COUNT(*) FROM users WHERE last_activity > ?", (five_min_ago,)); active_now = c.fetchone()[0]
        c.execute("SELECT SUM(total_messages) FROM users"); total_messages = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM rejections"); total_rejections = c.fetchone()[0]
        conn.close()
        return {"total_users": total_users, "active_week": active_week, "active_now": active_now, "total_messages": total_messages, "total_rejections": total_rejections}

    @staticmethod
    def get_all_users(limit=20):
        conn = get_db_connection(); c = conn.cursor()
        c.execute("SELECT user_id, username, first_name, total_messages FROM users ORDER BY total_messages DESC LIMIT ?", (limit,))
        rows = c.fetchall(); conn.close(); return rows

# ======================= أوامر الإدارة (مختصرة) =======================

async def stats_command(update, context):
    if not admin_service.is_admin(update.effective_user.id): return await update.message.reply_text("⛔ للمدراء فقط.", parse_mode=None)
    stats = StatsRepository.get_stats(); top_q = QuestionRepository.get_top(5)
    top_txt = "\n".join([f"- {q[0]}: {q[1]} مرة" for q in top_q]) or "لا توجد"
    await update.message.reply_text(f"📊 الإحصائيات:\n👥 {stats['total_users']}\n🟢 {stats['active_week']}\n💬 {stats['total_messages']}\n🔥 {top_txt}", parse_mode=None)

async def users_command(update, context):
    if not admin_service.is_admin(update.effective_user.id): return await update.message.reply_text("⛔ للمدراء فقط.", parse_mode=None)
    users = StatsRepository.get_all_users(20)
    msg = "👥 المستخدمون:\n"
    for u in users: msg += f"- @{u[1] or 'بدون'} ({u[2]}) - {u[3]} رسائل\n"
    await update.message.reply_text(msg, parse_mode=None)

async def broadcast_command(update, context):
    if not admin_service.is_admin(update.effective_user.id): return await update.message.reply_text("⛔ للمدراء فقط.", parse_mode=None)
    args = context.args
    if not args: return await update.message.reply_text("❗ استخدم: /broadcast النص", parse_mode=None)
    txt = " ".join(args)
    users = StatsRepository.get_all_users(9999)
    sent, failed = 0, 0
    for u in users:
        try:
            await context.bot.send_message(u[0], f"📢 إعلان:\n{txt}", parse_mode=None)
            sent += 1
        except: failed += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"✅ {sent} | ❌ {failed}", parse_mode=None)

async def export_command(update, context):
    if not admin_service.is_admin(update.effective_user.id): return await update.message.reply_text("⛔ للمدراء فقط.", parse_mode=None)
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(["النوع", "المعرف", "الاسم", "القيمة"])
    conn = get_db_connection(); c = conn.cursor()
    c.execute("SELECT user_id, username, first_name FROM users")
    for row in c.fetchall(): writer.writerow(["مستخدم", row[0], row[1] or row[2] or "", ""])
    conn.close()
    output.seek(0)
    await update.message.reply_document(io.BytesIO(output.getvalue().encode('utf-8')), filename="export.csv")

async def add_admin_command(update, context):
    if not admin_service.is_owner(update.effective_user.id): return await update.message.reply_text("⛔ للمالك فقط.", parse_mode=None)
    args = context.args
    if len(args) < 2: return await update.message.reply_text("❗ /addadmin @username رمز", parse_mode=None)
    username = args[0].replace("@", ""); secret = args[1]
    try:
        user_obj = await context.bot.get_chat(username)
        admin_service.add_admin(user_obj.id, username, secret, update.effective_user.id)
        await update.message.reply_text(f"✅ تم إضافة {username}", parse_mode=None)
    except: await update.message.reply_text("❌ لم أجد المستخدم", parse_mode=None)

async def remove_admin_command(update, context):
    if not admin_service.is_owner(update.effective_user.id): return await update.message.reply_text("⛔ للمالك فقط.", parse_mode=None)
    args = context.args
    if not args: return await update.message.reply_text("❗ /removeadmin @username", parse_mode=None)
    username = args[0].replace("@", "")
    if admin_service.remove_admin(username): await update.message.reply_text(f"✅ تم حذف {username}", parse_mode=None)
    else: await update.message.reply_text(f"❌ لم أجد {username}", parse_mode=None)

async def admins_command(update, context):
    if not admin_service.is_admin(update.effective_user.id): return await update.message.reply_text("⛔ للمدراء فقط.", parse_mode=None)
    admins = admin_service.get_all()
    msg = "📋 المدراء:\n"
    for a in admins: msg += f"- @{a[1]}\n"
    await update.message.reply_text(msg, parse_mode=None)

async def set_rule_command(update, context):
    if not admin_service.is_owner(update.effective_user.id): return await update.message.reply_text("⛔ للمالك فقط.", parse_mode=None)
    args = context.args
    if not args: return await update.message.reply_text("❗ /rule النص", parse_mode=None)
    new_rule = " ".join(args)
    rules_service.add("active_rule", new_rule, update.effective_user.id)
    rules_service.activate("active_rule")
    await update.message.reply_text("✅ تم تحديث القاعدة", parse_mode=None)

async def clear_rule_command(update, context):
    if not admin_service.is_owner(update.effective_user.id): return await update.message.reply_text("⛔ للمالك فقط.", parse_mode=None)
    rules_service.delete_all()
    await update.message.reply_text("✅ تم إلغاء القاعدة المخصصة", parse_mode=None)

async def unknown_command(update, context):
    await update.message.reply_text("⚠️ أمر غير معروف. استخدم /start", parse_mode=None)

# ======================= التشغيل =======================

def main():
    init_db()
    global ai_service, youtube_service, context_service, admin_service, rules_service
    ai_service = AIService(BASE_SYSTEM_PROMPT)
    youtube_service = YouTubeService()
    context_service = ContextService()
    admin_service = AdminService(ADMIN_ID)
    rules_service = RulesService(BASE_SYSTEM_PROMPT)

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CommandHandler("admins", admins_command))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("removeadmin", remove_admin_command))
    app.add_handler(CommandHandler("rule", set_rule_command))
    app.add_handler(CommandHandler("clearrule", clear_rule_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ البوت العقاري الجديد يعمل...")

    async def delete_webhook():
        await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook تم حذفه")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(delete_webhook())

    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
