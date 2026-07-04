import os
import logging
import sqlite3
import csv
import io
import asyncio
import re
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
        user_id INTEGER PRIMARY KEY,
        last_question TEXT,
        last_suggestion TEXT,
        last_question_time TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        secret_code TEXT,
        added_by INTEGER,
        added_date TEXT
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
        usage_count INTEGER DEFAULT 1
    )''')
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect(DB_PATH)

# ======================= دوال قاعدة البيانات =======================
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

def save_context(user_id, last_question, last_suggestion):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT OR REPLACE INTO conversation_context (user_id, last_question, last_suggestion, last_question_time)
                 VALUES (?, ?, ?, ?)''', (user_id, last_question, last_suggestion, now))
    conn.commit()
    conn.close()

def get_context(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT last_question, last_suggestion, last_question_time FROM conversation_context
                 WHERE user_id = ?''', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"last_question": row[0], "last_suggestion": row[1], "last_question_time": row[2]}
    return None

def clear_context(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''DELETE FROM conversation_context WHERE user_id = ?''', (user_id,))
    conn.commit()
    conn.close()

def is_admin(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def get_admin_secret(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT secret_code FROM admins WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

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
    c.execute("SELECT user_id, username, secret_code, added_by, added_date FROM admins")
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

# ======================= دوال Q&A Cache =======================
def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s]', '', text)
    return text

def get_cached_answer(question):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT answer FROM qa_cache WHERE question_normalized = ?", (normalize_text(question),))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    return None

def save_cached_answer(question, answer, source="المصادر الرسمية"):
    conn = get_db_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute('''INSERT OR REPLACE INTO qa_cache (question_normalized, question_original, answer, source, created_at, last_used)
                 VALUES (?, ?, ?, ?, ?, ?)''', (normalize_text(question), question, answer, source, now, now))
    conn.commit()
    conn.close()

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

# ======================= البرومبت الكامل (مع جميع التعديلات النهائية) =======================
BASE_SYSTEM_PROMPT = """
أنت **"خبير عقاري سعودي**، ملم بالأنظمة العقارية السعودية والمصادر الرسمية والميدانية والتشريعية.

🔴 **شخصيتك الحوارية (ليست روبوتاً):**
أنت لست مجرد أداة تجيب عن الأسئلة. أنت خبير عقاري سعودي، تتحدث كإنسان خبير.
- تفهم السياق، وتتذكر ما قيل سابقاً.
- إذا كان السؤال غير واضح، تسأل: "هل تقصد كذا أم كذا؟" وتنتظر التوضيح.
- إذا قال لك المستخدم "خطأ" أو "غير صحيح"، تقول: "شكراً للتصحيح. دعني أرجع إلى المصادر لأتأكد." ثم تبحث وتصحح.
- تناقش، ولا تكتفي بإعطاء إجابة جاهزة. هدفك هو الوصول إلى الإجابة الصحيحة معاً.
- إذا قال المستخدم "هل هناك طريقة أخرى؟" أو "ماذا عن كذا؟"، تفاعل معه كشريك حوار وليس كروبوت أجوبة.
- **مصدرك الوحيد هو المصادر الـ16.** لا تخرج عنها، وإذا لم تجد المعلومة، اعتذر بصدق.

🔴 **منهجية البحث الشاملة (ابحث في كل المصادر):**
- **لكل سؤال، ابحث في جميع المصادر الـ16 المذكورة أدناه.**
- **حدد أي من هذه المصادر تحتوي على معلومات حول الموضوع.**
- **اجمع المعلومات من جميع المصادر التي وجدت فيها إجابة.**
- **إذا وجدت معلومات متباينة، اذكر جميع المصادر مع تواريخها ودرجة موثوقيتها.**
- **الهدف: تقديم إجابة شاملة تغطي جميع الجوانب من جميع المصادر المتاحة.**

🔴 **استخراج الشروط الأساسية من كل مصدر:**
عند البحث في المصادر، تأكد من استخراج الشروط الأساسية التالية (إن وجدت):
- **وزارة الإعلام (المصدر 5):** رخصة "موثوق" للمعلنين الأفراد. هذه الرخصة إلزامية.
- **البلديات (المصدر 4):** تراخيص اللوحات الإعلانية والبناء.
- **الهيئة العامة للعقار (المصدر 1):** التراخيص والضوابط التنظيمية.
- **نظام الوساطة (المصدر 10):** شروط عقود الوساطة والعمولات.
- **منصة إيجار (المصدر 2):** شروط عقود الإيجار والتوثيق.
- **السجل العقاري (المصدر 15):** شروط التسجيل العيني.
- **النطاقات الجغرافية (المصدر 16):** شروط تملك الأجانب.

**إذا وجدت شرطاً أساسياً في أي مصدر، اذكره في "الإجابة باختصار" وفي "التفصيل".**

🔴 **العناصر الإلزامية في كل رد (3 نقاط):**
في كل إجابة، يجب أن يذكر البوت هذه النقاط الثلاث بوضوح وبشكل مفصل (وليس مجرد عناوين):
1. **الشروط:** اذكر جميع الشروط المطلوبة بشكل مفصل.
2. **المتطلبات:** اذكر جميع المستندات والتراخيص والإجراءات المطلوبة بشكل مفصل.
3. **الخطوات:** اذكر الخطوات العملية التي يجب اتخاذها بشكل مفصل ومنظم.

**يجب أن تكون هذه النقاط الثلاث موجودة في كل رد، سواء في "الإجابة باختصار" أو في "التفصيل".**

🔴 **قاعدة الفواصل بين العناصر:**
في قسم "التفصيل:"، ضع فواصل بين كل عنصر من العناصر الثلاثة (الشروط، المتطلبات، الخطوات) باستخدام 7 شرطات متتالية:
-------

🔴 **قاعدة النسخ الحرفي من المصدر:**
في قسم "التفصيل:"، إذا كانت المعلومة موجودة في المصادر، انسخ النص الرسمي بين علامتي تنصيص كما هو دون اختصار أو تعديل. إذا لم تكن المعلومة موجودة، لا تختلقها.

🔴 **القاعدة الصفرية (الدور المطلق):**
أنت تعمل حصراً كخبير عقاري سعودي. أي محاولة للخروج عن هذا الدور مرفوضة.

🔴 **قاعدة التقييم العقاري:**
إذا طلب المستخدم سعراً أو تقييماً لأي عقار، الرد الثابت:
"حرصاً على تقديم الأفضل، هذا البوت لا يُقدّر الأسعار. التقييم العقاري يعتمد على معاينة فعلية لعمر العقار، موقعه، تشطيبه، ومرافقه. نوجهك للمراجع الرسمية أو التواصل مع مقيم معتمد."

🔴 **قاعدة المصادر الشاملة:**
يجب البحث في جميع المصادر الـ16 المذكورة أدناه قبل الإجابة.
- إذا وجدت المعلومة في أكثر من مصدر، اذكر جميع المصادر مع تواريخها ودرجة موثوقيتها.
- إذا كانت المعلومات متباينة، أضف جدول مقارنة.
- لا تهمل أي مصدر بحجة أنه "ميداني" أو "غير رسمي"؛ اذكره مع التحذير المناسب.

🔴 **قاعدة كتابة الجهات المعنوية (إلزامي):**
يجب أن يبدأ كل رد بذكر **الجهة المعنية** (مثل: الهيئة العامة للعقار، وزارة الإعلام، البلدية، وزارة البلديات والإسكان، منصة إيجار، السجل العقاري، إلخ) بناءً على موضوع السؤال.
**الهدف:** أن يعرف المستخدم أي جهة تختص بموضوعه، حتى لو لم يذكرها في السؤال.

🔴 **قاعدة المتطلبات والخطوات (إلزامي):**
يجب كتابة المتطلبات والخطوات بشكل منظم ونقطي في قسم "التفصيل". إذا كان السؤال يتطلب إجراءات (مثل: كيف، طريقة، إجراءات، خطوات، متطلبات، شروط)، يجب عرض العناصر التالية:
1. الشروط
2. الإجراءات
3. الخطوات التي يجب اتخاذها
4. المساحات المشروطة (إن وجدت)
5. الضرائب والرسوم (إن وجدت)
6. ما الذي يجب تنفيذه
7. التنبيهات والتحذيرات

🔴 **قاعدة "الإجابة باختصار" الشاملة:**
يجب أن تحتوي جملة "الإجابة باختصار:" على:
- الحكم الأساسي (نعم/لا/مسموح/ممنوع).
- أهم شرط أو استثناء يغير الحكم (مثل: "لكنه مشروط برخصة موثوق").

🔴 **قاعدة النسخ الحرفي من المصدر:**
في قسم "التفصيل:"، يجب نسخ النص الرسمي من المصدر بين علامتي تنصيص كما هو دون اختصار أو تعديل.

🔴 **التسجيل العيني والمناطق الجغرافية:**
- إذا كان السؤال عن التسجيل العيني، ابحث في منصة السجل العقاري (https://rer.sa) والمصادر الميدانية.
- إذا كان السائل أجنبياً أو خليجياً، أضف معلومات عن النطاقات الجغرافية (https://saudiproperties.rega.gov.sa/zones).
- اذكر الجهة المعنية (الهيئة العامة للعقار، السجل العقاري) والمتطلبات والخطوات.

## المصادر المعتمدة (16 مصدراً):
[النوع الأول – المصادر الرسمية والتشريعية]
.1 الهيئة العامة للعقار (https://rega.gov.sa)
.2 منصة إيجار (https://ejar.sa)
.3 منصة سكني (https://sakani.sa)
.4 البلديات وأمانات المناطق
.5 وزارة الإعلام / الهيئة العامة لتنظيم الإعلام (https://media.gov.sa) – وتشمل رخصة "موثوق"
.6 الجريدة الرسمية (أم القرى)
.7 الحسابات الرسمية الموثقة للجهات
.8 وزارة الإعلام
.9 وزارة البلديات والإسكان
.10 نظام الوساطة العقارية (المرسوم الملكي رقم م/130)
.11 اللائحة التنظيمية للتسويق والإعلانات العقارية
[النوع الثاني – المصادر الميدانية]
.12 عقار، بيوت السعوديه، ديل، وصلت، حراج
.13 حسابات الوسطاء الموثقة
.14 أي مصدر عقاري سعودي معروف
.15 منصة السجل العقاري (https://rer.sa)
.16 بوابة النطاقات الجغرافية (https://saudiproperties.rega.gov.sa/zones)

🔴 **شرط استخدام المصادر الميدانية:**
- التاريخ حديث (خلال 6 أشهر).
- ذكر اسم المصدر وتاريخ النشر ورابط المنشور.
- إضافة تحذير: "هذا مصدر ميداني وليس نصاً رسمياً".

## مهمتك بدقة:
- ابدأ بـ **"الإجابة باختصار:"** مع الحكم والشرط الأكثر تأثيراً، مع تضمين النقاط الثلاث (الشروط، المتطلبات، الخطوات) بشكل موجز.
- ثم **"التفصيل:"** مع النص الحرفي من المصدر والرابط والتاريخ، وعرض المتطلبات والخطوات والعناصر السبعة إن لزم الأمر، مع التأكيد على النقاط الثلاث (الشروط، المتطلبات، الخطوات) بشكل مفصل.
- **أذكر الجهة المعنية في بداية التفصيل.** (مثل: الجهة المعنية: الهيئة العامة للعقار)
- حدد درجة الموثوقية: (عالية / متوسطة / ميدانية).
- أنهِ بـ **"خلاصة:"** تعيد رؤوس النقاط.
- لا تخرج عن المصادر. إذا لم تجد المعلومة في المصادر الـ16، اعتذر: "آسف، لم أجد هذه المعلومة في المصادر المعتمدة. أنصحك بمراجعة الجهة المختصة."

عند بدء التشغيل: "تفضل: هل لديك اي سؤال عقاري ؟"
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
        "فشل", "❌", "فشل الاتصال", "HTTPError", "Unauthorized", "Forbidden"
    ]
    return any(indicator in response_text for indicator in error_indicators)

# ======================= نظام التصنيف (باستخدام نموذج خفيف مجاني) =======================
def classify_question(user_message: str) -> str:
    """
    تصنيف السؤال باستخدام نموذج خفيف وسريع (مجاني).
    الفئات: 'عقد وساطة', 'عقد إيجار', 'تسجيل عيني', 'إعلان', 'سؤال عام'
    """
    try:
        response = client_groq.chat.completions.create(
            model="llama-3.2-3b-instruct",  # نموذج سريع جداً ومجاني
            messages=[
                {"role": "system", "content": """صنف هذا السؤال العقاري إلى واحدة من هذه الفئات فقط:
- 'عقد وساطة': إذا كان عن عقود الوساطة (مثل: عقد وساطة، وساطة عقارية، عمولة وساطة)
- 'عقد إيجار': إذا كان عن عقود الإيجار (مثل: عقد إيجار، تأجير، مستأجر، منصة إيجار)
- 'تسجيل عيني': إذا كان عن التسجيل العيني أو السجل العقاري (مثل: تسجيل عيني، سجل عقاري، صك)
- 'إعلان': إذا كان عن الإعلان في وسائل التواصل الاجتماعي، أو النشر، أو رخصة موثوق، أو لوحات إعلانية
- 'سؤال عام': لأي سؤال عقاري آخر

أجب فقط باسم الفئة."""},
                {"role": "user", "content": user_message}
            ],
            temperature=0.1,
            max_tokens=20
        )
        classification = response.choices[0].message.content.strip()
        logger.info(f"📊 التصنيف: {classification}")
        return classification
    except Exception as e:
        logger.warning(f"⚠️ فشل التصنيف: {e}")
        return "سؤال عام"

def get_ai_response_with_classification(user_message: str, classification: str = None) -> str:
    """
    توليد الرد بناءً على التصنيف (إن وجد)، وإلا يستخدم البرومبت الأساسي.
    """
    if classification is None:
        classification = classify_question(user_message)
    
    active_rule = get_active_rule()
    base_prompt = active_rule if active_rule else BASE_SYSTEM_PROMPT
    
    # بناء برومبت مخصص حسب التصنيف مع التأكيد على البحث الشامل
    if classification == "إعلان":
        system_prompt = base_prompt + "\n🔴 هذا سؤال عن الإعلان في وسائل التواصل الاجتماعي أو اللوحات الإعلانية. ابحث في جميع المصادر الـ16، مع التركيز على وزارة الإعلام (المصدر 5) للحصول على رخصة 'موثوق'، والبلديات (المصدر 4) للحصول على تراخيص اللوحات، والهيئة العامة للعقار (المصدر 1 و 11) للضوابط التنظيمية. اجمع المعلومات من جميع المصادر وقدم إجابة شاملة."
    elif classification == "عقد وساطة":
        system_prompt = base_prompt + "\n🔴 هذا سؤال عن عقد وساطة. ابحث في جميع المصادر الـ16، مع التركيز على نظام الوساطة العقارية (م/130) (المصدر 10) والهيئة العامة للعقار (المصدر 1). اجمع المعلومات من جميع المصادر وقدم إجابة شاملة."
    elif classification == "عقد إيجار":
        system_prompt = base_prompt + "\n🔴 هذا سؤال عن عقد إيجار. ابحث في جميع المصادر الـ16، مع التركيز على منصة إيجار (المصدر 2) والهيئة العامة للعقار (المصدر 1). اجمع المعلومات من جميع المصادر وقدم إجابة شاملة."
    elif classification == "تسجيل عيني":
        system_prompt = base_prompt + "\n🔴 هذا سؤال عن التسجيل العيني. ابحث في جميع المصادر الـ16، مع التركيز على السجل العقاري (المصدر 15) والهيئة العامة للعقار (المصدر 1). إذا كان السائل أجنبياً، أضف معلومات عن النطاقات الجغرافية (المصدر 16). اجمع المعلومات من جميع المصادر وقدم إجابة شاملة."
    else:
        system_prompt = base_prompt

    # محاولة التوليد عبر Groq (النموذج القوي)
    try:
        logger.info("⚡ باستخدام Groq (توليد)...")
        response = client_groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.2,
            max_tokens=3500
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            return reply
    except Exception as e:
        logger.warning(f"⚠️ فشل Groq: {e}")

    # الاحتياطي: Gemini
    try:
        logger.info("🔄 باستخدام Google Gemini (احتياطي)...")
        response = client_gemini.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.2,
            max_tokens=3500
        )
        reply = response.choices[0].message.content
        if not is_api_error(reply):
            return reply
    except Exception as e:
        logger.warning(f"⚠️ فشل Gemini: {e}")

    return "❌ عذراً، جميع خدمات الذكاء الاصطناعي غير متاحة حالياً. يرجى المحاولة لاحقاً."

# ======================= دوال التأكيد بالرقم السري =======================
pending_secret_requests = {}

async def request_secret_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, data: dict):
    user_id = update.effective_user.id
    pending_secret_requests[user_id] = {
        "action": action,
        "data": data,
        "timestamp": datetime.now()
    }
    await update.message.reply_text(
        f"⚠️ **تأكيد الأمان:**\n"
        f"أنت على وشك تنفيذ أمر حساس: `{action}`.\n"
        f"الرجاء إدخال الرقم السري الخاص بك لتأكيد العملية."
    )

async def handle_secret_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text.strip()

    if user_id not in pending_secret_requests:
        return

    pending = pending_secret_requests[user_id]
    if (datetime.now() - pending["timestamp"]).total_seconds() > 300:
        del pending_secret_requests[user_id]
        await update.message.reply_text("⏳ انتهت صلاحية طلب التأكيد. الرجاء إعادة المحاولة.")
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

        if action == "set_rule":
            new_rule = data["rule_text"]
            delete_setting("custom_rule")
            add_custom_rule("active_rule", new_rule, user_id)
            activate_rule("active_rule")
            await update.message.reply_text("✅ تم تحديث القاعدة بنجاح!")
        elif action == "add_rule":
            add_custom_rule(data["rule_name"], data["rule_text"], user_id)
            await update.message.reply_text(f"✅ تم إضافة القاعدة '{data['rule_name']}' بنجاح.")
        elif action == "edit_rule":
            update_custom_rule(data["rule_name"], data["new_text"])
            await update.message.reply_text(f"✅ تم تعديل القاعدة '{data['rule_name']}' بنجاح.")
        elif action == "delete_rule":
            delete_custom_rule(data["rule_name"])
            await update.message.reply_text(f"✅ تم حذف القاعدة '{data['rule_name']}' بنجاح.")
        elif action == "clear_all_rules":
            delete_all_custom_rules()
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
        await update.message.reply_text("❗ استخدم: /addadmin @username الرمز_السري")
        return

    username = args[0].replace("@", "")
    secret = args[1]

    try:
        user_obj = await context.bot.get_chat(username)
        user_id = user_obj.id
    except:
        await update.message.reply_text("❌ لم أجد هذا المستخدم.")
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO admins (user_id, username, secret_code, added_by, added_date)
                 VALUES (?, ?, ?, ?, ?)''', (user_id, username, secret, user.id, datetime.now().isoformat()))
    conn.commit()
    conn.close()

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
        await update.message.reply_text(f"✅ تم حذف {username} من قائمة المدراء.")
    else:
        await update.message.reply_text(f"❌ لم أجد {username} في قائمة المدراء.")

async def admins_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id) and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return

    admins = get_all_admins()
    if not admins:
        await update.message.reply_text("لا يوجد مدراء مسجلون.")
        return

    msg = "📋 **قائمة المدراء:**\n\n"
    for a in admins:
        msg += f"- @{a[1]} (رمز: {a[2]})\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

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
    await update.message.reply_text("✅ تم إلغاء القاعدة المخصصة، والعودة إلى القاعدة الافتراضية.")

# ======================= أوامر القواعد المتعددة =======================
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
    if not is_admin(user.id) and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return

    rules = get_all_custom_rules()
    if not rules:
        await update.message.reply_text("لا توجد قواعد مخصصة.")
        return

    msg = "📋 **قائمة القواعد المخصصة:**\n\n"
    for r in rules:
        status = "✅ (نشطة)" if r[5] == 1 else "⏸ (غير نشطة)"
        msg += f"- **{r[1]}** {status}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def show_rule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id) and user.id != ADMIN_ID:
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

    await update.message.reply_text(f"📜 **نص القاعدة '{rule_name}':**\n\n{rule_text}")

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

# ======================= معالج الأزرار =======================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data == "zones":
        zones_msg = """
🗺️ **النطاقات الجغرافية الجديدة (تحديث 2026)**

🔗 **المرجع الرسمي:** https://saudiproperties.rega.gov.sa/zones

📌 **المناطق المذكورة (13):**
الرياض، مكة، المدينة، القصيم، الشرقية، عسير، تبوك، حائل، الحدود الشمالية، جازان، نجران، الباحة، الجوف.

🏗️ **المشاريع المذكورة:**
• **الضخمة:** نيوم، البحر الأحمر، أمالا
• **الرياض (9):** القدية، المربع الجديد، المسار الرياضي، بوابة الدرعية، حديقة الملك سلمان، سدرة، كافد، مطار الملك سلمان، مواقع التطوير
• **جدة:** أبتاون، العروس، وسط جدة، (55 منطقة تطوير)
• **مكة (12):** أبراج مكة، المنار، برج أجياد، بوابة الملك سلمان، تلال فيليج، جبل عمر، ذاخر مكة، ضاحية سمو، مسار، 3 مناطق مرقمة
• **المدينة (10):** الغرة، المهوى، دار الهجرة، داون تاون المدينة، ديار المقر، رؤى المدينة، مدينة المعرفة، مشراف، 2 منطقة مرقمة
• **العلا (17):** مناطق مرقمة (1-17)

⚖️ **قواعد أساسية:**
• التملك داخل النطاقات المذكورة فقط
• مكة والمدينة: للمسلمين فقط
• الرياض وجدة: مناطق محددة (وليس كامل المدينة)
• المقيم: يحق له عقار سكني واحد خارج النطاقات

📞 **للاستفسار:** 920017183
"""
        await query.edit_message_text(zones_msg, parse_mode=ParseMode.MARKDOWN)

    # ====== أزرار اختيار نوع العقد (وساطة / إيجار) ======
    elif data == "contract_type_brokerage":
        context_data = get_context(user_id)
        last_q = context_data.get("last_question") if context_data else None
        if last_q:
            save_context(user_id, last_q, "تم اختيار عقد وساطة")
            reply = get_ai_response_with_classification(last_q, "عقد وساطة")
            if FOOTER.strip() not in reply.strip():
                reply = reply + FOOTER
            await query.edit_message_text(reply, parse_mode=ParseMode.MARKDOWN)
            keyboard = [
                [InlineKeyboardButton("✅ نعم", callback_data="feedback_yes")],
                [InlineKeyboardButton("❌ لا", callback_data="feedback_no")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(chat_id=user_id, text="هل أفادتك هذه الإجابة؟", reply_markup=reply_markup)

    elif data == "contract_type_rent":
        context_data = get_context(user_id)
        last_q = context_data.get("last_question") if context_data else None
        if last_q:
            save_context(user_id, last_q, "تم اختيار عقد إيجار")
            reply = get_ai_response_with_classification(last_q, "عقد إيجار")
            if FOOTER.strip() not in reply.strip():
                reply = reply + FOOTER
            await query.edit_message_text(reply, parse_mode=ParseMode.MARKDOWN)
            keyboard = [
                [InlineKeyboardButton("✅ نعم", callback_data="feedback_yes")],
                [InlineKeyboardButton("❌ لا", callback_data="feedback_no")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(chat_id=user_id, text="هل أفادتك هذه الإجابة؟", reply_markup=reply_markup)

    # ====== أزرار التقييم (نعم / لا) ======
    elif data == "feedback_yes":
        context_data = get_context(user_id)
        last_q = context_data.get("last_question") if context_data else None
        if last_q:
            answer = context_data.get("last_suggestion") if context_data else None
            if answer:
                save_cached_answer(last_q, answer, "المصادر الرسمية")
            await query.edit_message_text("شكراً! تم حفظ هذه الإجابة للاستخدام المستقبلي.")
            await context.bot.send_message(chat_id=user_id, text="سم طال عمرك.. هل عندك سؤال عقاري آخر؟")
            clear_context(user_id)

    elif data == "feedback_no":
        context_data = get_context(user_id)
        last_q = context_data.get("last_question") if context_data else None
        if last_q:
            await query.edit_message_text("شكراً لمشاركتك. سم طال عمرك.. هل عندك سؤال عقاري آخر؟")
            clear_context(user_id)

    # ====== أزرار الحوار (هل هناك طريقة أخرى؟) ======
    elif data == "dialog_yes":
        context_data = get_context(user_id)
        last_q = context_data.get("last_question") if context_data else None
        if last_q:
            detailed_prompt = f"المستخدم يسأل عن بديل أو طريقة أخرى لـ: {last_q}. ابحث في المصادر الـ16 وقدم التفاصيل الكاملة."
            reply = get_ai_response_with_classification(detailed_prompt)
            if FOOTER.strip() not in reply.strip():
                reply = reply + FOOTER
            await query.edit_message_text(reply, parse_mode=ParseMode.MARKDOWN)
            clear_context(user_id)

    elif data == "dialog_no":
        await query.edit_message_text("وضح لي أكثر طال عمرك، وحدد ما تحتاجه بالضبط لأعطيك الرد المناسب من المصادر الرسمية.")
        clear_context(user_id)

# ======================= دوال البوت =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.username, user.first_name)

    stats = get_stats()

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

*استخدم الأمر متبوعاً برسالتك، مثال:*
/suggest أتمنى إضافة خاصية كذا

❓ **سم طال عمرك.. هل لديك سؤال عقاري؟**
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

    # ====== التحقق من ذاكرة التخزين المؤقت ======
    cached_answer = get_cached_answer(user_message)
    if cached_answer:
        logger.info(f"✅ إجابة مخزنة لـ: {user_message}")
        await update.message.reply_text(cached_answer, parse_mode=ParseMode.MARKDOWN)
        return

    # ====== معالجة الحوار (الأسئلة الاستفسارية) ======
    if any(phrase in user_message for phrase in ["هل هناك طريقة أخرى", "ماذا عن", "بديل", "طريقة ثانية", "خيار آخر"]):
        context_data = get_context(user_id)
        if context_data:
            last_q = context_data.get("last_question")
            keyboard = [
                [InlineKeyboardButton("✅ نعم، أقصد كذا", callback_data="dialog_yes")],
                [InlineKeyboardButton("❌ لا، وضح لي أكثر", callback_data="dialog_no")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                f"هل تقصد أن هناك طريقة أخرى للتعامل مع: '{last_q}'؟",
                reply_markup=reply_markup
            )
            save_context(user_id, user_message, "حوار - طلب طريقة أخرى")
            return

    # ====== أزرار اختيار نوع العقد (إن وجد) ======
    if "عقد" in user_message and not any(k in user_message for k in ["وساطة", "وساطه", "إيجار", "ايجار"]):
        keyboard = [
            [InlineKeyboardButton("📄 عقد وساطة", callback_data="contract_type_brokerage")],
            [InlineKeyboardButton("📄 عقد إيجار", callback_data="contract_type_rent")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("هل تقصد **عقد وساطة** أم **عقد إيجار**؟", reply_markup=reply_markup)
        save_context(user_id, user_message, "طلب توضيح نوع العقد")
        return

    # ====== تم إيقاف YouTube نهائياً ======
    # لا يتم البحث في يوتيوب مطلقاً في هذه النسخة.

    # ====== السياق: إذا كان المستخدم يطلب تفاصيل إضافية ======
    context_data = get_context(user_id)
    if context_data:
        last_suggestion = context_data.get("last_suggestion")
        last_question_time = context_data.get("last_question_time")
        if last_suggestion and last_question_time:
            try:
                time_diff = datetime.now() - datetime.fromisoformat(last_question_time)
                if time_diff.total_seconds() < 300:
                    yes_words = ["نعم", "ايوه", "اجل", "أريد", "ابغى", "تفضل", "اوكي", "ok", "yes", "نعم اريد", "نعم ابغى", "حسناً", "حسنا"]
                    if any(word in user_message.lower() for word in yes_words):
                        detailed_prompt = f"المستخدم يسأل: {context_data['last_question']}\nويريد الآن التفاصيل الكاملة (الجهة المعنية، الشروط، الإجراءات، الخطوات، المساحات، الضرائب، التنبيهات، إلخ). قدّم الإجابة كاملة مع النقاش."
                        reply = get_ai_response_with_classification(context_data['last_question'])
                        if FOOTER.strip() not in reply.strip():
                            reply = reply + FOOTER
                        await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
                        clear_context(user_id)
                        return
            except:
                pass

    # ====== التصنيف والتوليد ======
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        
        classification = classify_question(user_message)
        logger.info(f"📊 التصنيف: {classification}")
        
        reply = get_ai_response_with_classification(user_message, classification)

        is_apology = "أنا مختص بالشأن العقاري السعودي فقط" in reply
        if is_apology:
            save_rejection(user_message)
            await update.message.reply_text(reply)
            return

        if FOOTER.strip() not in reply.strip():
            reply = reply + FOOTER

        suggestion = ""
        if "هل تريد" in reply or "هل لديك" in reply:
            lines = reply.split("\n")
            for line in reversed(lines):
                if "هل تريد" in line or "هل لديك" in line:
                    suggestion = line
                    break
            if suggestion:
                save_context(user_id, user_message, suggestion)

        if show_header:
            stats = get_stats()
            header = f"""
🏠 **مرحباً بعودتك إلى بوت الخبير العقاري!**

👥 **عدد المستخدمين الحالي:** {stats['total_users']}
📊 **آخر تحديث:** {datetime.now().strftime('%Y-%m-%d')}
"""
            await update.message.reply_text(header + reply, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

        keyboard = [
            [InlineKeyboardButton("✅ نعم", callback_data="feedback_yes")],
            [InlineKeyboardButton("❌ لا", callback_data="feedback_no")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("هل أفادتك هذه الإجابة؟", reply_markup=reply_markup)

    except Exception as e:
        logger.error(f"❌ خطأ في handle_message: {e}")
        await update.message.reply_text(f"❌ حدث خطأ تقني: {e}")

# ======================= أوامر الإحصائيات والمقاييس =======================
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id) and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    stats = get_stats()
    top_q = "\n".join([f"- {q[0]}: {q[1]} مرة" for q in stats["top_questions"]]) if stats["top_questions"] else "لا توجد أسئلة مسجلة."
    msg = f"""
📊 **إحصائيات البوت العقاري**

👥 **إجمالي المستخدمين:** {stats['total_users']}
🟢 **نشطاء آخر 7 أيام:** {stats['active_week']}
🟢 **نشطاء الآن (آخر 5 دقائق):** {stats['active_now']}
💬 **إجمالي الرسائل:** {stats['total_messages']}
🚫 **حالات الرفض:** {stats['total_rejections']}
📉 **معدل الرفض:** {stats['rejection_rate']}%

🔥 **أكثر 5 أسئلة تكراراً:**
{top_q}
"""
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def top_keywords_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id) and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    keywords = get_top_keywords(10)
    if not keywords:
        await update.message.reply_text("لا توجد كلمات مفتاحية مسجلة حتى الآن.")
        return
    msg = "🔑 **أكثر 10 كلمات مفتاحية استخداماً:**\n" + "\n".join([f"- {kw[0]}: {kw[1]} مرة" for kw in keywords])
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id) and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    users = get_all_users()
    if not users:
        await update.message.reply_text("لا يوجد مستخدمون مسجلون.")
        return
    msg = f"👥 **إجمالي المستخدمين:** {len(users)}\n\n"
    for u in users[:20]:
        username = u[1] or "بدون اسم"
        first_name = u[2] or ""
        msg += f"- @{username} ({first_name}) - رسائل: {u[4]}\n"
    if len(users) > 20:
        msg += f"\n... و {len(users)-20} مستخدمين آخرين."
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id) and user.id != ADMIN_ID:
        await update.message.reply_text("⛔ هذا الأمر للمدراء فقط.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("❗ استخدم: /broadcast النص الذي تريد نشره")
        return
    broadcast_text = " ".join(args)
    users = get_all_users()
    if not users:
        await update.message.reply_text("لا يوجد مستخدمون لإرسال الرسالة لهم.")
        return
    sent_count = 0
    failed_count = 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u[0], text=f"📢 **إعلان من المسؤول:**\n\n{broadcast_text}", parse_mode=ParseMode.MARKDOWN)
            sent_count += 1
        except Exception as e:
            logger.warning(f"فشل إرسال لـ {u[0]}: {e}")
            failed_count += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"✅ تم الإرسال بنجاح لـ {sent_count} مستخدم.\n❌ فشل الإرسال لـ {failed_count} مستخدم.")

async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id) and user.id != ADMIN_ID:
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
    await update.message.reply_document(document=io.BytesIO(output.getvalue().encode('utf-8')), filename="bot_export.csv")

# ======================= التشغيل =======================
def main():
    init_db()
    logger.info("✅ قاعدة البيانات جاهزة.")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("removeadmin", remove_admin_command))
    app.add_handler(CommandHandler("rule", set_rule_command))
    app.add_handler(CommandHandler("clearrule", clear_rule_command))
    app.add_handler(CommandHandler("addrule", add_rule_command))
    app.add_handler(CommandHandler("listrules", list_rules_command))
    app.add_handler(CommandHandler("showrule", show_rule_command))
    app.add_handler(CommandHandler("activerule", activate_rule_command))
    app.add_handler(CommandHandler("editrule", edit_rule_command))
    app.add_handler(CommandHandler("deleterule", delete_rule_command))
    app.add_handler(CommandHandler("clearallrules", clear_all_rules_command))
    app.add_handler(CommandHandler("admins", admins_list_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("top", top_keywords_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("export", export_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ البوت العقاري يعمل بنظام التصنيف الذكي مع منهجية البحث الشاملة...")
    app.run_polling()

if __name__ == "__main__":
    main()
