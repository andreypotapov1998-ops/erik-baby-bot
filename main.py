import sys, os, time, datetime, json, sqlite3, threading, re, io, base64, calendar
import requests
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9030754644:AAHxX2fIejBORcD15LJTMtmz4QLlXEr9iqk")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6IMaP_hPyqTMZCsl8wCINItMLzk0ymb0ylha8QBSUyY5Q")
API_URL = f"https://api.telegram.org/bot{TOKEN}/"
FILE_URL = f"https://api.telegram.org/file/bot{TOKEN}/"
DB_PATH = os.path.join(os.path.dirname(__file__), "erik_tracker.db")

app = FastAPI(title="Erik Baby Tracker & Alice Skill")

MSK_TZ = datetime.timezone(datetime.timedelta(hours=3), name="MSK")

def msk_now():
    """Возвращает актуальное Московское время (UTC+3) даже на серверах Render в США/Европе."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone(MSK_TZ).replace(tzinfo=None)

def msk_today():
    """Текущая дата по Московскому времени."""
    return msk_now().date()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER,
            user_name TEXT,
            category TEXT,
            sub_type TEXT,
            value TEXT,
            notes TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            user_name TEXT,
            original_text TEXT,
            highlights TEXT,
            health_notes TEXT,
            raw_ai_summary TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Default settings
    defaults = {
        "feed_interval_hours": "3.0",
        "wake_window_minutes": "75",
        "bath_time": "20:50",
        "is_sleeping": "0"
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO state (key, value) VALUES (?, ?)", (k, v))
        
    # Постоянные подписчики (семейная группа и родители) — сохраняются при любых перезапусках
    default_subs = [
        (-5595572014, "Семья: Эрик 👶"),
        (1985221994, "Андрей")
    ]
    for cid, title in default_subs:
        cur.execute("INSERT OR IGNORE INTO subscribers (chat_id, title) VALUES (?, ?)", (cid, title))
        
    conn.commit()
    conn.close()

def db_set_state(key, value):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)", (key, str(value)))
    conn.commit()
    conn.close()

def db_get_state(key):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value, updated_at FROM state WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row

def db_log_event(user_id, user_name, category, sub_type, value="", notes=""):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO events (timestamp, user_id, user_name, category, sub_type, value, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (msk_now().strftime("%Y-%m-%d %H:%M:%S"), user_id, user_name, category, sub_type, value, notes))
    conn.commit()
    conn.close()

def db_log_memory(user_name, original_text, highlights, health_notes, raw_ai_summary):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO memories (timestamp, user_name, original_text, highlights, health_notes, raw_ai_summary)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (msk_now().strftime("%Y-%m-%d %H:%M:%S"), user_name, original_text, highlights, health_notes, raw_ai_summary))
    conn.commit()
    conn.close()

def db_get_memories():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT timestamp, user_name, highlights, health_notes, original_text FROM memories ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

def db_register_chat(chat_id, title):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO subscribers (chat_id, title) VALUES (?, ?)", (chat_id, title))
    conn.commit()
    conn.close()

def db_get_subscribers():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM subscribers")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows

def db_get_all_events():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT timestamp, user_name, category, sub_type, value, notes FROM events ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    return rows

def db_get_today_summary():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    today_str = msk_today().isoformat()
    cur.execute("""
        SELECT category, sub_type, value, timestamp 
        FROM events 
        WHERE date(timestamp) = ?
        ORDER BY timestamp ASC
    """, (today_str,))
    rows = cur.fetchall()
    conn.close()
    return rows

def db_get_last_event(category=None, sub_type=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    query = "SELECT timestamp, user_name, category, sub_type, value FROM events WHERE 1=1"
    params = []
    if category:
        query += " AND category = ?"
        params.append(category)
    if sub_type:
        query += " AND sub_type = ?"
        params.append(sub_type)
    query += " ORDER BY id DESC LIMIT 1"
    cur.execute(query, params)
    row = cur.fetchone()
    conn.close()
    return row

def tg_send(method, data):
    try:
        r = requests.post(API_URL + method, json=data, timeout=20)
        return r.json()
    except Exception as e:
        print(f"API Error ({method}): {e}")
        return None

def tg_send_document(chat_id, filename, file_bytes, caption=""):
    try:
        files = {"document": (filename, file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        requests.post(API_URL + "sendDocument", data=data, files=files, timeout=30)
    except Exception as e:
        print(f"Error sending document: {e}")

def broadcast_message(text, exclude_chat_id=None, reply_markup=None):
    for cid in db_get_subscribers():
        # Семейную группу (отрицательный ID в Telegram) никогда не исключаем — туда всегда дублируются все события!
        if exclude_chat_id and cid == exclude_chat_id and cid > 0:
            continue
        msg = {"chat_id": cid, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            msg["reply_markup"] = reply_markup
        tg_send("sendMessage", msg)

def format_time_diff(dt_str):
    try:
        t = datetime.datetime.fromisoformat(dt_str)
        now = msk_now()
        diff = now - t
        total_minutes = int(diff.total_seconds() // 60)
        hours = total_minutes // 60
        mins = total_minutes % 60
        if hours > 0:
            return f"{hours} ч {mins} мин назад"
        return f"{mins} мин назад"
    except Exception:
        return "недавно"

def get_erik_age_parts(target_date=None):
    if target_date is None:
        target_date = msk_today()
    birth = datetime.date(2026, 10, 25)
    if target_date < birth:
        return (0, 0, 0)
    y = target_date.year - birth.year
    m = target_date.month - birth.month
    d = target_date.day - birth.day
    if d < 0:
        prev_m = target_date.month - 1 if target_date.month > 1 else 12
        prev_y = target_date.year if target_date.month > 1 else target_date.year - 1
        days_in_prev = calendar.monthrange(prev_y, prev_m)[1]
        d += days_in_prev
        m -= 1
    if m < 0:
        m += 12
        y -= 1
    return (y, m, d)

def plural_ru(n, f1, f2, f5):
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 10 < n_abs < 20:
        return f"{n} {f5}"
    if 1 < n1 < 5:
        return f"{n} {f2}"
    if n1 == 1:
        return f"{n} {f1}"
    return f"{n} {f5}"

def get_erik_age_str(target_date=None):
    y, m, d = get_erik_age_parts(target_date)
    y_str = plural_ru(y, "год", "года", "лет")
    m_str = plural_ru(m, "месяц", "месяца", "месяцев")
    d_str = plural_ru(d, "день", "дня", "дней")
    return f"{y_str}, {m_str} и {d_str}"

def format_summary():
    now_str = msk_now().strftime("%H:%M, %d.%m.%Y")
    age_str = get_erik_age_str()
    text = f"<b>📊 Статус малыша Эрика на {now_str}:</b>\n"
    text += f"👶 <b>Возраст:</b> {age_str} (род. 25.10.2026)\n\n"
    
    sleep_state = db_get_state("is_sleeping")
    is_sleeping = sleep_state and sleep_state[0] == "1"
    last_sleep = db_get_last_event("sleep")
    wake_win = int(float((db_get_state("wake_window_minutes") or ["75"])[0]))

    if is_sleeping:
        since = format_time_diff(last_sleep[0]) if last_sleep else "только что"
        text += f"😴 <b>Сон:</b> Эрик сейчас <b>спит</b> (уснул {since}).\n"
    else:
        wake_since = format_time_diff(last_sleep[0]) if last_sleep else "неизвестно"
        text += f"☀️ <b>Сон:</b> Бодрствует (проснулся {wake_since}).\n"
        if last_sleep:
            try:
                wake_t = datetime.datetime.fromisoformat(last_sleep[0])
                target_sleep = (wake_t + datetime.timedelta(minutes=wake_win)).strftime("%H:%M")
                text += f"⏳ <i>Окно бодрствования до ~{target_sleep} (комфортный сон без переутомления).</i>\n"
            except Exception:
                pass
        
    last_feed = db_get_last_event("feed")
    feed_int = float((db_get_state("feed_interval_hours") or ["3.0"])[0])
    if last_feed:
        t_str = last_feed[0].split()[1][:5]
        diff = format_time_diff(last_feed[0])
        val = f" ({last_feed[4]})" if last_feed[4] else ""
        text += f"🍼 <b>Кормление:</b> Было в {t_str} ({diff}){val}.\n"
        try:
            t = datetime.datetime.fromisoformat(last_feed[0])
            next_f = (t + datetime.timedelta(hours=feed_int)).strftime("%H:%M")
            text += f"⏳ <i>Следующее ориентировочно в {next_f}.</i>\n"
        except Exception:
            pass
    else:
        text += "🍼 <b>Кормление:</b> Сегодня ещё не отмечали.\n"
        
    rows = db_get_today_summary()
    diapers = [r for r in rows if r[0] == "diaper"]
    bottles_ml = sum(int(r[2]) for r in rows if r[0] == "feed" and str(r[2]).isdigit())
    vit_d = any("Витамин D" in str(r[1]) or r[1] == "care_vitd" for r in rows)
    
    text += f"\n<b>👶 За сегодня:</b>\n"
    text += f"• Подгузников: <b>{len(diapers)}</b> шт.\n"
    if bottles_ml > 0:
        text += f"• Смеси выпито: <b>{bottles_ml}</b> мл.\n"
    text += f"• Витамин D: {'✅ <b>Дали</b>' if vit_d else '❌ <i>Ещё не давали</i>'}\n"
    
    bath_time = (db_get_state("bath_time") or ["20:50"])[0]
    weekday = msk_now().weekday()
    shampoo_str = "🧴 С шампунем (среда/суббота)" if weekday in [2, 5] else "💧 Без шампуня (тёплая вода)"
    text += f"\n🛁 <b>Купание:</b> ежедневно в {bath_time} ({shampoo_str})."
    
    return text

def generate_excel_bytes():
    wb = openpyxl.Workbook()
    header_fill = PatternFill(start_color="3B7A57", end_color="3B7A57", fill_type="solid")
    header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
    
    # Sheet 1: Все события
    ws1 = wb.active
    ws1.title = "Журнал событий"
    headers1 = ["Дата и время", "Категория", "Действие", "Кто отметил", "Значение / Объём", "Примечание"]
    ws1.append(headers1)
    
    for col_num in range(1, len(headers1) + 1):
        cell = ws1.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        
    category_map = {
        "feed": "🍼 Кормление",
        "sleep": "😴 Сон",
        "diaper": "👶 Подгузник",
        "care": "💊 Уход и здоровье"
    }
    
    for row in db_get_all_events():
        ts, uname, cat, sub, val, notes = row
        cat_title = category_map.get(cat, cat)
        ws1.append([ts, cat_title, sub, uname, val or "", notes or ""])
        
    for col in ws1.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws1.column_dimensions[col_letter].width = max(max_len + 4, 15)
        
    # Sheet 2: Памятные моменты и хроника
    ws2 = wb.create_sheet(title="Дневник памяти и вехи")
    headers2 = ["Дата и время", "Кто записал", "🌟 Главные моменты и достижения", "🩺 Самочувствие", "Полный текст наблюдения"]
    ws2.append(headers2)
    
    for col_num in range(1, len(headers2) + 1):
        cell = ws2.cell(row=1, column=col_num)
        cell.fill = PatternFill(start_color="D97B43", end_color="D97B43", fill_type="solid")
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        
    for mem in db_get_memories():
        ts, uname, high, hlth, orig = mem
        ws2.append([ts, uname, high or "", hlth or "", orig or ""])
        
    for col in ws2.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws2.column_dimensions[col_letter].width = max(min(max_len + 4, 50), 20)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()

def process_voice_or_text_with_ai(audio_bytes=None, text_content=None, user_name="Аня"):
    key = os.getenv("GEMINI_API_KEY") or GEMINI_KEY
    if not key:
        if text_content:
            db_log_memory(user_name, text_content, text_content, "", text_content)
            return (
                f"📝 <b>Заметка сохранена в дневник:</b>\n\n"
                f"«{text_content}»"
            )
        else:
            db_log_memory(user_name, "[Голосовое сообщение]", "[Голосовое сообщение сохранено]", "", "[Голосовое сообщение]")
            return (
                "🎙️ <b>Голосовая заметка сохранена в дневник памяти!</b>\n\n"
                "💡 <i>Чтобы нейросеть расшифровывала речь в структурированный текст, добавьте бесплатный ключ GEMINI_API_KEY на Render (получить за 30 сек на aistudio.google.com).</i>"
            )

    prompt = (
        "Ты — заботливый персональный семейный ассистент и биограф малыша Эрика (новорождённый). "
        f"Мама (Аня) или папа ({user_name}) записали голосовое сообщение с наблюдениями за день, эмоциями, "
        "достижениями и состоянием здоровья малыша.\n\n"
        "Твоя задача:\n"
        "1. Внимательно прослушай (или прочитай) запись.\n"
        "2. Сделай структурированную, тёплую и красивую выжимку на русском языке:\n"
        "   - 🌟 **Главные события и вехи развития:** (первая улыбка, взгляд, первый зубик, новые звуки, купание, смешные реакции и т.д.).\n"
        "   - 🩺 **Самочувствие и настроение:** (животик, колики, спокойствие, сон).\n"
        "   - 🍼 **Режим и питание:** (если упоминались объёмы кормления или сны).\n"
        "   - 💡 **Теплая цитата дня:** (трогательное предложение из рассказа родителя на память в альбом).\n\n"
        "Отвечай красиво оформленным текстом с эмодзи в формате HTML (используй <b>жирный</b>, без markdown-звёздочек)."
    )

    parts = [{"text": prompt}]
    if audio_bytes:
        b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
        parts.append({
            "inline_data": {
                "mime_type": "audio/ogg",
                "data": b64_audio
            }
        })
    elif text_content:
        parts.append({"text": f"Текст сообщения от {user_name}:\n{text_content}"})

    payload = {"contents": [{"parts": parts}]}
    
    # Try modern Flash models with active quota
    models_to_try = [
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash"
    ]
    last_err = ""
    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}"
        try:
            r = requests.post(url, json=payload, timeout=40)
            if r.status_code == 200:
                res_json = r.json()
                if "candidates" in res_json and res_json["candidates"]:
                    ai_text = res_json["candidates"][0]["content"]["parts"][0]["text"]
                    ai_text = ai_text.replace("**", "<b>").replace("<b></b>", "")
                    db_log_memory(user_name, text_content or "[Голосовое сообщение]", ai_text, "", ai_text)
                    return ai_text
            else:
                last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)

    # Fallback if network or quota issue: still save the memory!
    db_log_memory(user_name, text_content or "[Голосовое сообщение]", "[Голосовая заметка]", "", last_err)
    return (
        f"🎙️ <b>Голосовая запись сохранена в хронику дневника!</b>\n\n"
        f"<i>(Нейросеть временно формирует ответ: {last_err}. Запись не потерялась и сохранена в базе!)</i>"
    )

def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "🍼 Кормление"}, {"text": "😴 Сон"}],
            [{"text": "👶 Подгузник"}, {"text": "💊 Витамин D и Уход"}],
            [{"text": "📊 Статус и Сводка"}, {"text": "📁 Скачать Excel"}],
            [{"text": "📖 Дневник памяти"}, {"text": "⚙️ Настройки"}]
        ],
        "resize_keyboard": True
    }

def get_feed_inline():
    return {
        "inline_keyboard": [
            [
                {"text": "🤱 Левая грудь", "callback_data": "feed_breast_left"},
                {"text": "🤱 Правая грудь", "callback_data": "feed_breast_right"}
            ],
            [
                {"text": "🍼 Смесь: 60 мл", "callback_data": "feed_bottle_60"},
                {"text": "🍼 Смесь: 90 мл", "callback_data": "feed_bottle_90"}
            ],
            [
                {"text": "🍼 Смесь: 120 мл", "callback_data": "feed_bottle_120"},
                {"text": "🍼 Смесь: 150 мл", "callback_data": "feed_bottle_150"}
            ],
            [
                {"text": "❌ Закрыть", "callback_data": "close_menu"}
            ]
        ]
    }

def get_sleep_inline():
    sleep_state = db_get_state("is_sleeping")
    is_sleeping = sleep_state and sleep_state[0] == "1"
    if is_sleeping:
        return {
            "inline_keyboard": [
                [{"text": "☀️ Эрик проснулся!", "callback_data": "sleep_wake"}],
                [{"text": "❌ Закрыть", "callback_data": "close_menu"}]
            ]
        }
    else:
        return {
            "inline_keyboard": [
                [{"text": "💤 Эрик уснул", "callback_data": "sleep_start"}],
                [{"text": "❌ Закрыть", "callback_data": "close_menu"}]
            ]
        }

def get_diaper_inline():
    return {
        "inline_keyboard": [
            [{"text": "💧 Мокрый", "callback_data": "diaper_wet"}],
            [{"text": "💩 Грязный", "callback_data": "diaper_dirty"}],
            [{"text": "💧+💩 Смешанный", "callback_data": "diaper_both"}],
            [{"text": "❌ Закрыть", "callback_data": "close_menu"}]
        ]
    }

def get_care_inline():
    return {
        "inline_keyboard": [
            [{"text": "☀️ Дали Витамин D", "callback_data": "care_vitd"}],
            [{"text": "👃 Закапали Аквамарис", "callback_data": "care_aquamaris"}],
            [{"text": "🧴 Масло Johnson's Baby", "callback_data": "care_oil"}],
            [{"text": "🛁 Искупали малыша", "callback_data": "care_bath"}],
            [{"text": "💨 Капли от колик", "callback_data": "care_colic"}],
            [{"text": "❌ Закрыть", "callback_data": "close_menu"}]
        ]
    }

def get_settings_inline():
    feed_int = (db_get_state("feed_interval_hours") or ["3.0"])[0]
    wake_win = (db_get_state("wake_window_minutes") or ["75"])[0]
    bath_t = (db_get_state("bath_time") or ["20:50"])[0]
    return {
        "inline_keyboard": [
            [{"text": f"🍼 Интервал кормления: {feed_int} ч", "callback_data": "menu_set_feed"}],
            [{"text": f"😴 Окно бодрствования: {wake_win} мин", "callback_data": "menu_set_wake"}],
            [{"text": f"🛁 Время купания: {bath_t}", "callback_data": "menu_set_bath"}],
            [{"text": "🎓 Пройти обучение заново", "callback_data": "start_tutorial"}],
            [{"text": "❌ Закрыть", "callback_data": "close_menu"}]
        ]
    }

def get_tutorial_step_1_inline():
    return {
        "inline_keyboard": [
            [
                {"text": "🤱 Левая грудь", "callback_data": "tut_feed_breast_left"},
                {"text": "🤱 Правая грудь", "callback_data": "tut_feed_breast_right"}
            ],
            [
                {"text": "🍼 Смесь: 90 мл", "callback_data": "tut_feed_bottle_90"},
                {"text": "🍼 Смесь: 120 мл", "callback_data": "tut_feed_bottle_120"}
            ],
            [
                {"text": "⏩ Пропустить обучение", "callback_data": "tut_skip"}
            ]
        ]
    }

def get_tutorial_step_2_inline():
    return {
        "inline_keyboard": [
            [{"text": "💤 Эрик уснул (тест)", "callback_data": "tut_sleep"}],
            [{"text": "⏩ Пропустить обучение", "callback_data": "tut_skip"}]
        ]
    }

def get_tutorial_step_3_inline():
    return {
        "inline_keyboard": [
            [
                {"text": "💧 Мокрый", "callback_data": "tut_diaper_wet"},
                {"text": "💩 Грязный", "callback_data": "tut_diaper_dirty"}
            ],
            [
                {"text": "💧+💩 Смешанный", "callback_data": "tut_diaper_both"}
            ],
            [{"text": "⏩ Пропустить обучение", "callback_data": "tut_skip"}]
        ]
    }

def get_tutorial_step_4_inline():
    return {
        "inline_keyboard": [
            [{"text": "✅ Завершить обучение и перейти в рабочий режим", "callback_data": "tut_finish"}]
        ]
    }

def handle_callback(cb):
    cb_id = cb["id"]
    data = cb.get("data")
    user = cb["from"]
    user_name = user.get("first_name", "Родитель")
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]

    tg_send("answerCallbackQuery", {"callback_query_id": cb_id})

    if data == "close_menu":
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
        return

    now_time = msk_now().strftime("%H:%M")

    # Settings callbacks
    if data == "menu_set_feed":
        kb = {
            "inline_keyboard": [
                [{"text": "2.5 часа", "callback_data": "set_feed_2.5"}, {"text": "3.0 часа", "callback_data": "set_feed_3.0"}],
                [{"text": "3.5 часа", "callback_data": "set_feed_3.5"}, {"text": "4.0 часа", "callback_data": "set_feed_4.0"}],
                [{"text": "🔙 Назад", "callback_data": "back_to_settings"}]
            ]
        }
        tg_send("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "🍼 <b>Выберите интервал между кормлениями:</b>", "parse_mode": "HTML", "reply_markup": kb})
        return

    elif data.startswith("set_feed_"):
        val = data.replace("set_feed_", "")
        db_set_state("feed_interval_hours", val)
        broadcast_message(f"⚙️ <b>{user_name}</b> установил интервал кормлений: <b>{val} часа</b>.")
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
        return

    elif data == "menu_set_wake":
        kb = {
            "inline_keyboard": [
                [{"text": "50 мин", "callback_data": "set_wake_50"}, {"text": "60 мин", "callback_data": "set_wake_60"}],
                [{"text": "75 мин", "callback_data": "set_wake_75"}, {"text": "90 мин", "callback_data": "set_wake_90"}],
                [{"text": "🔙 Назад", "callback_data": "back_to_settings"}]
            ]
        }
        tg_send("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "😴 <b>Выберите окно бодрствования для Эрика:</b>", "parse_mode": "HTML", "reply_markup": kb})
        return

    elif data.startswith("set_wake_"):
        val = data.replace("set_wake_", "")
        db_set_state("wake_window_minutes", val)
        broadcast_message(f"⚙️ <b>{user_name}</b> установил окно бодрствования: <b>{val} минут</b>.")
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
        return

    elif data == "menu_set_bath":
        kb = {
            "inline_keyboard": [
                [{"text": "20:30", "callback_data": "set_bath_20:30"}, {"text": "20:50", "callback_data": "set_bath_20:50"}],
                [{"text": "21:00", "callback_data": "set_bath_21:00"}, {"text": "21:30", "callback_data": "set_bath_21:30"}],
                [{"text": "🔙 Назад", "callback_data": "back_to_settings"}]
            ]
        }
        tg_send("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "🛁 <b>Выберите время ежедневного купания:</b>", "parse_mode": "HTML", "reply_markup": kb})
        return

    elif data.startswith("set_bath_"):
        val = data.replace("set_bath_", "")
        db_set_state("bath_time", val)
        broadcast_message(f"⚙️ <b>{user_name}</b> установил время купания: <b>{val}</b>.")
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
        return

    elif data == "back_to_settings":
        tg_send("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "⚙️ <b>Настройки интервалов и расписания:</b>", "parse_mode": "HTML", "reply_markup": get_settings_inline()})
        return

    # Tutorial callbacks
    if data == "start_tutorial":
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
        age_str = get_erik_age_str()
        welcome_letter = (
            f"👋 <b>Добро пожаловать в «Дневник малыша Эрика»!</b>\n\n"
            f"Этот бот создан специально для вас с Аней, чтобы в один клик фиксировать все важные события: кормления, сны, подгузники, процедуры ухода и памятные вехи.\n\n"
            f"👶 <b>Сегодня Эрику:</b> {age_str} (род. 25.10.2026)\n\n"
            f"✨ <b>Ключевые возможности:</b>\n"
            f"• 📱 <b>Синхронизация для мамы и папы:</b> все отметки моментально видны обоим на iPhone 14 Pro Max.\n"
            f"• 🍼 <b>Умные напоминания:</b> бот рассчитывает интервалы кормлений, утренний уход (Аквамарис + Витамин D в кормление с 7:00 до 10:00), а в 20:50 напомнит про купание, шапочку и масло Johnson's Baby.\n"
            f"• 🎙️ <b>Голосовой дневник с нейросетью:</b> отправляйте голосовые сообщения — Gemini бережно структурирует воспоминания и сохраняет в семейную хронику.\n"
            f"• 🗣️ <b>Яндекс Алиса:</b> управление голосом с домашней колонки.\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🎓 <b>ВКЛЮЧЕН ОБУЧАЮЩИЙ РЕЖИМ</b>\n"
            f"Давайте за 1 минуту попробуем основные кнопки на тестовых примерах. После обучения бот <b>автоматически перейдет в обычный рабочий режим</b>, а тестовые данные не попадут в журнал.\n\n"
            f"<b>Шаг 1 из 3: Кормление 🍼</b>\n"
            f"Попробуйте нажать кнопку ниже, чтобы сделать пробную отметку кормления:"
        )
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": welcome_letter,
            "parse_mode": "HTML",
            "reply_markup": get_tutorial_step_1_inline()
        })
        return

    elif data.startswith("tut_feed_"):
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
        feed_int = float((db_get_state("feed_interval_hours") or ["3.0"])[0])
        next_t = (msk_now() + datetime.timedelta(hours=feed_int)).strftime("%H:%M")
        step2_text = (
            f"🎉 <b>Отлично получилось!</b>\n"
            f"В обычном режиме бот сразу рассчитает время следующего кормления (~{next_t}) "
            f"и пришлет уведомление второму родителю.\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🎓 <b>Шаг 2 из 3: Контроль сна 😴</b>\n"
            f"Когда укладываете Эрика спать — нажмите «💤 Эрик уснул». "
            f"А когда он проснется — нажмите «☀️ Проснулся». Бот посчитает время сна "
            f"и подскажет окно бодрствования до следующего сна без переутомления.\n\n"
            f"Попробуйте нажать:"
        )
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": step2_text,
            "parse_mode": "HTML",
            "reply_markup": get_tutorial_step_2_inline()
        })
        return

    elif data == "tut_sleep":
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
        step3_text = (
            f"🎉 <b>Зафиксировано!</b>\n"
            f"Малыш отмечен спящим. Когда он проснется, кнопка сменится на «☀️ Эрик проснулся!», "
            f"а статус покажет, сколько минут он спал.\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🎓 <b>Шаг 3 из 3: Смена подгузника 👶</b>\n"
            f"Педиатры рекомендуют отслеживать количество подгузников в день. "
            f"Бот ведет счетчик за сутки и выводит его в сводку и в Excel.\n\n"
            f"Попробуйте отметить пробный подгузник:"
        )
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": step3_text,
            "parse_mode": "HTML",
            "reply_markup": get_tutorial_step_3_inline()
        })
        return

    elif data.startswith("tut_diaper_"):
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
        finish_preview = (
            f"🎉 <b>Великолепно! Все базовые навыки освоены!</b>\n\n"
            f"🎙️ <b>Как записать памятный момент:</b>\n"
            f"Просто отправьте в этот чат голосовое сообщение (например, <i>«Эрик сегодня первый раз осознанно улыбнулся погремушке»</i>) — нейросеть бережно структурирует и сохранит это в «📖 Дневник памяти».\n\n"
            f"🗣️ <b>Как спросить у Алисы с колонки:</b>\n"
            f"• <i>«Алиса, спроси у Дневника Эрика, когда он ел?»</i>\n"
            f"• <i>«Алиса, передай Дневнику Эрика: покормили 100 мл»</i>\n"
            f"• <i>«Алиса, напомни Дневнику Эрика про купание»</i>\n\n"
            f"Нажмите кнопку ниже, чтобы отключить тестовый режим и перейти к реальной работе:"
        )
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": finish_preview,
            "parse_mode": "HTML",
            "reply_markup": get_tutorial_step_4_inline()
        })
        return

    elif data in ["tut_finish", "tut_skip"]:
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
        db_set_state(f"tutorial_{chat_id}", "0")
        age_str = get_erik_age_str()
        done_msg = (
            f"🌟 <b>Обучающий режим успешно завершён!</b>\n\n"
            f"Бот переведён в <b>обычный рабочий режим</b>. Все тестовые действия сброшены, журнал чист и готов к реальным записям.\n\n"
            f"👶 <b>Эрику сегодня:</b> {age_str} (родился 25.10.2026).\n\n"
            f"👇 Все основные разделы доступны на клавиатуре ниже:"
        )
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": done_msg,
            "parse_mode": "HTML",
            "reply_markup": get_main_keyboard()
        })
        return

    # Regular action callbacks
    if data.startswith("feed_"):
        val_map = {
            "feed_breast_left": "🤱 Грудь (левая)",
            "feed_breast_right": "🤱 Грудь (правая)",
            "feed_bottle_60": "🍼 Смесь 60 мл",
            "feed_bottle_90": "🍼 Смесь 90 мл",
            "feed_bottle_120": "🍼 Смесь 120 мл",
            "feed_bottle_150": "🍼 Смесь 150 мл",
        }
        val_name = val_map.get(data, "Кормление")
        ml_val = data.replace("feed_bottle_", "") if "bottle" in data else ""
        db_log_event(user["id"], user_name, "feed", val_name, ml_val)
        
        feed_int = float((db_get_state("feed_interval_hours") or ["3.0"])[0])
        next_t = (msk_now() + datetime.timedelta(hours=feed_int)).strftime("%H:%M")
        
        now_dt = msk_now()
        today_date_str = str(now_dt.date())
        morning_care_done = db_get_state("morning_care_sent_date")
        morning_care_block = ""
        if "07:00" <= now_time <= "10:00" and (not morning_care_done or morning_care_done[0] != today_date_str):
            age_str = get_erik_age_str()
            morning_care_block = (
                f"\n\n☀️ <b>Утренний уход (сегодня Эрику {age_str}):</b>\n"
                f"• 👃 <b>Аквамарис в носик:</b> закапать и прочистить ватными жгутиками.\n"
                f"• 💊 <b>Витамин D:</b> дать 1 каплю во время или после еды."
            )
            db_set_state("morning_care_sent_date", today_date_str)

        announcement = (
            f"🍼 <b>{user_name}:</b> {val_name} в {now_time}.\n"
            f"⏳ Следующее кормление ориентировочно в ~{next_t}.\n\n"
            f"💡 <i>Напоминание на следующий раз: перед едой выложить на животик на 5–10 минут и проверить подгузник.</i>"
            f"{morning_care_block}"
        )
        broadcast_message(announcement)
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})

    elif data == "sleep_start":
        db_set_state("is_sleeping", "1")
        db_log_event(user["id"], user_name, "sleep", "Уснул")
        announcement = f"😴 <b>{user_name}:</b> Малыш Эрик <b>уснул 💤</b> в {now_time}.\n<i>(Включите белый шум на колонке, чтобы крепче спал)</i>"
        broadcast_message(announcement)
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})

    elif data == "sleep_wake":
        db_set_state("is_sleeping", "0")
        last_sleep = db_get_last_event("sleep", "Уснул")
        dur_str = ""
        if last_sleep:
            dur_str = f" Поспал <b>{format_time_diff(last_sleep[0]).replace(' назад', '')}</b>."
        db_log_event(user["id"], user_name, "sleep", "Проснулся", dur_str)
        
        wake_win = int(float((db_get_state("wake_window_minutes") or ["75"])[0]))
        target_t = (msk_now() + datetime.timedelta(minutes=wake_win)).strftime("%H:%M")
        
        announcement = (
            f"☀️ <b>{user_name}:</b> Эрик <b>проснулся!</b> в {now_time}.{dur_str}\n"
            f"⏳ Окно бодрствования: до ~<b>{target_t}</b> (пора будет укладывать, чтобы не перегулял)."
        )
        broadcast_message(announcement)
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})

    elif data.startswith("diaper_"):
        diaper_map = {
            "diaper_wet": "💧 Мокрый подгузник",
            "diaper_dirty": "💩 Грязный подгузник",
            "diaper_both": "💧+💩 Смешанный подгузник"
        }
        d_name = diaper_map.get(data, "Подгузник")
        db_log_event(user["id"], user_name, "diaper", d_name)
        announcement = f"👶 <b>{user_name}:</b> Смена подгузника — {d_name} в {now_time}."
        broadcast_message(announcement)
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})

    elif data.startswith("care_"):
        care_map = {
            "care_vitd": "☀️ Дали Витамин D",
            "care_aquamaris": "👃 Закапали Аквамарис в носик",
            "care_oil": "🧴 Намазали тело маслом Johnson's Baby",
            "care_bath": "🛁 Искупали малыша (надели шапочку)",
            "care_colic": "💨 Дали капли от колик"
        }
        c_name = care_map.get(data, "Уход")
        db_log_event(user["id"], user_name, "care", c_name)
        announcement = f"💊 <b>{user_name}:</b> {c_name} в {now_time}."
        broadcast_message(announcement)
        tg_send("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})

def handle_message(msg):
    chat = msg["chat"]
    chat_id = chat["id"]
    user = msg.get("from", {})
    user_name = user.get("first_name", "Родитель")
    text = msg.get("text", "").strip()

    title = chat.get("title") or user_name
    db_register_chat(chat_id, title)

    # 1. Voice message handling
    if "voice" in msg or "audio" in msg:
        file_obj = msg.get("voice") or msg.get("audio")
        file_id = file_obj.get("file_id")
        duration = file_obj.get("duration", 0)
        dur_str = f" ({duration} сек)" if duration else ""
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": f"🎙️ <i>Слушаю голосовое сообщение{dur_str} и раскладываю всё по полочкам...</i>",
            "parse_mode": "HTML"
        })
        
        file_info = tg_send("getFile", {"file_id": file_id})
        if file_info and file_info.get("ok"):
            file_path = file_info["result"]["file_path"]
            download_url = FILE_URL + file_path
            try:
                audio_bytes = requests.get(download_url, timeout=40).content
                ai_summary = process_voice_or_text_with_ai(audio_bytes=audio_bytes, user_name=user_name)
                response_text = f"📖 <b>Дневник малыша Эрика (Новая запись от {user_name}):</b>\n\n{ai_summary}"
                # Send to sender directly first
                tg_send("sendMessage", {"chat_id": chat_id, "text": response_text, "parse_mode": "HTML"})
                # Then notify other subscribers
                broadcast_message(response_text, exclude_chat_id=chat_id)
            except Exception as e:
                tg_send("sendMessage", {"chat_id": chat_id, "text": f"⚠️ Ошибка обработки аудио: {e}"})
        else:
            tg_send("sendMessage", {"chat_id": chat_id, "text": "⚠️ Не удалось получить аудиофайл от серверов Telegram."})
        return

    if text.startswith("/start") or text.startswith("/tutorial") or text == "🎓 Обучение":
        db_set_state(f"tutorial_{chat_id}", "1")
        age_str = get_erik_age_str()
        welcome = (
            f"👋 <b>Добро пожаловать в «Дневник малыша Эрика»!</b>\n\n"
            f"Этот бот создан специально для вас с Аней, чтобы в один клик вести совместный журнал малыша: кормления, сны, подгузники, уход и важные воспоминания.\n\n"
            f"👶 <b>Сегодня Эрику:</b> {age_str} (род. 25.10.2026)\n\n"
            f"✨ <b>Как устроен бот:</b>\n"
            f"• 📱 <b>Синхронизация:</b> все отметки моментально видны обоим родителям на iPhone 14 Pro Max.\n"
            f"• 🍼 <b>Умные напоминания:</b> бот сам рассчитывает интервалы между кормлениями, утром в окне 07:00–10:00 напоминает про Аквамарис и Витамин D, а в 20:50 зовёт на купание (с шампунем ср/сб, шапочкой и маслом Johnson's Baby).\n"
            f"• 🎙️ <b>Голосовой дневник:</b> надиктовывайте голосовые — нейросеть сохранит первые зубки, улыбки и наблюдения в семейный альбом.\n"
            f"• 🗣️ <b>Яндекс Алиса:</b> спросите у колонки: <i>«Алиса, когда Эрик ел?»</i> или скажите <i>«покормили 100 мл»</i>.\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🎓 <b>ВКЛЮЧЕН ТЕСТОВЫЙ ОБУЧАЮЩИЙ РЕЖИМ</b>\n"
            f"Давайте за 1 минуту попробуем основные функции на тестовых примерах. После обучения бот <b>автоматически перейдет в обычный рабочий режим</b>, а тестовые клики сбросятся.\n\n"
            f"<b>Шаг 1 из 3: Кормление 🍼</b>\n"
            f"Попробуйте нажать кнопку ниже, чтобы сделать пробную отметку кормления:"
        )
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": welcome,
            "parse_mode": "HTML",
            "reply_markup": get_tutorial_step_1_inline()
        })
        return
    elif text == "🍼 Кормление":
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": "🍼 <b>Что и сколько покушал малыш Эрик?</b>\n<i>💡 Не забудьте перед едой выложить на животик и проверить памперс!</i>",
            "parse_mode": "HTML",
            "reply_markup": get_feed_inline()
        })
    elif text == "😴 Сон":
        sleep_state = db_get_state("is_sleeping")
        is_sleeping = sleep_state and sleep_state[0] == "1"
        status_text = "😴 Сейчас Эрик <b>спит</b>." if is_sleeping else "☀️ Сейчас Эрик <b>бодрствует</b>."
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": f"{status_text}\nВыберите действие:",
            "parse_mode": "HTML",
            "reply_markup": get_sleep_inline()
        })
    elif text == "👶 Подгузник":
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": "👶 <b>Смена подгузника:</b>",
            "parse_mode": "HTML",
            "reply_markup": get_diaper_inline()
        })
    elif text == "💊 Витамин D и Уход":
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": "💊 <b>Здоровье и гигиена малыша Эрика:</b>",
            "parse_mode": "HTML",
            "reply_markup": get_care_inline()
        })
    elif text == "📊 Статус и Сводка":
        summary = format_summary()
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": summary,
            "parse_mode": "HTML"
        })
    elif text == "📁 Скачать Excel":
        tg_send("sendMessage", {"chat_id": chat_id, "text": "⏳ Формирую Excel-таблицу..."})
        excel_bytes = generate_excel_bytes()
        dt_file = msk_today().strftime("%d_%m_%Y")
        filename = f"Дневник_Эрика_{dt_file}.xlsx"
        caption = "📊 <b>Актуальный дневник малыша Эрика в Excel</b>\nСодержит два листа: журнал всех событий и памятные моменты."
        tg_send_document(chat_id, filename, excel_bytes, caption)

    elif text == "📖 Дневник памяти":
        mems = db_get_memories()
        if not mems:
            tg_send("sendMessage", {
                "chat_id": chat_id,
                "text": "📖 <b>Дневник памяти пока пуст.</b>\nНадиктуйте голосовое сообщение в чат (например: <i>«Сегодня Эрик впервые улыбнулся погремушке...»</i>), и нейросеть бережно сохранит его сюда на долгую память!",
                "parse_mode": "HTML"
            })
        else:
            text_out = "📖 <b>Последние памятные моменты малыша Эрика:</b>\n\n"
            for m in mems[:5]:
                ts, uname, high, hlth, orig = m
                date_str = ts.split()[0]
                text_out += f"📅 <b>{date_str} ({uname}):</b>\n{high}\n\n"
            tg_send("sendMessage", {"chat_id": chat_id, "text": text_out, "parse_mode": "HTML"})

    elif text == "⚙️ Настройки":
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": "⚙️ <b>Настройки интервалов и расписания:</b>\nЗдесь можно задать параметры ухода под ваш ритм дня:",
            "parse_mode": "HTML",
            "reply_markup": get_settings_inline()
        })

    elif len(text) > 30:
        ai_summary = process_voice_or_text_with_ai(text_content=text, user_name=user_name)
        response_text = f"📖 <b>Дневник малыша Эрика (Заметка от {user_name}):</b>\n\n{ai_summary}"
        broadcast_message(response_text)

# --- YANDEX ALICE WEBHOOK HANDLER ---
@app.post("/alice")
async def alice_webhook(request: Request):
    req_data = await request.json()
    command = req_data.get("request", {}).get("command", "").lower().strip()
    session = req_data.get("session", {})
    version = req_data.get("version", "1.0")

    reply_text = ""
    tts_text = None
    now_time = msk_now().strftime("%H:%M")

    # 1. Купание и вечерний уход
    if any(k in command for k in ["купание", "купать", "ванн", "помыть"]):
        weekday = msk_now().weekday()
        is_shampoo_day = weekday in [2, 5] # Среда = 2, Суббота = 5
        
        shampoo_msg = "Сегодня моем с шампунем (среда или суббота)!" if is_shampoo_day else "Сегодня купаем без шампуня, просто в тёплой воде."
        shampoo_tts = "Сегодня моем с шампунем." if is_shampoo_day else "Сегодня купаем без шампуня, в тёплой воде."
        
        reply_text = (
            f"🛁 <b>Правила купания и вечернего ухода малыша Эрика:</b>\n"
            f"• Температура воды: строго <b>36–37 °C</b> (проверьте водным термометром).\n"
            f"• Шампунь: {shampoo_msg}\n"
            f"• После купания: промокнуть полотенцем и <b>обязательно надеть шапочку</b>!\n"
            f"• Закапать <b>Аквамарис</b> в носик.\n"
            f"• Сделать массаж тела и складочек с маслом <b>Johnson's Baby</b>.\n"
            f"<i>(Витамин D вечером не даём — только утром)</i>"
        )
        # Use Alice Whisper TTS
        tts_text = f"<speaker is_whisper=\"true\">Пора купать малыша Эрика. Температура воды тридцать шесть, тридцать семь градусов. {shampoo_tts} После купания обязательно наденьте шапочку, закапайте Аквамарис и сделайте массаж с маслом Джонсонс Бейби.<speaker is_whisper=\"false\">"

    # 2. Уход / процедуры
    elif any(k in command for k in ["процедур", "уход", "аквамарис", "масло"]):
        age_str = get_erik_age_str()
        reply_text = (
            f"🩺 <b>Ежедневный уход за малышом Эриком (сегодня ему {age_str}):</b>\n\n"
            f"☀️ <b>Утром (в одно из кормлений с 7:00 до 10:00):</b>\n"
            f"• Закапать Аквамарис в носик и аккуратно очистить ватными жгутиками.\n"
            f"• Дать Витамин D (1 капля во время/после еды).\n\n"
            f"🛁 <b>Вечером (вместе с купанием в 20:50):</b>\n"
            f"• Купание при 36–37 °C (с шампунем по средам и субботам).\n"
            f"• Обязательно надеть шапочку после купания.\n"
            f"• Закапать Аквамарис в носик.\n"
            f"• Массаж с маслом Johnson's Baby (витамин D вечером не даём)."
        )
        tts_text = "Утром во время одного из кормлений закапайте Аквамарис, прочистите носик ватными жгутиками и дайте витамин Д. Вечером вместе с купанием наденьте шапочку, закапайте Аквамарис и сделайте массаж с маслом Джонсонс Бейби."

    # 3. Сон - проснулся
    elif any(k in command for k in ["проснул", "встал", "проснулась"]):
        db_set_state("is_sleeping", "0")
        last_sleep = db_get_last_event("sleep", "Уснул")
        dur_str = ""
        if last_sleep:
            dur_str = f" Поспал {format_time_diff(last_sleep[0]).replace(' назад', '')}."
        db_log_event(0, "Алиса (Голос)", "sleep", "Проснулся", dur_str)
        broadcast_message(f"☀️ <b>Голосовая отметка через Алису:</b> Эрик <b>проснулся!</b> в {now_time}.{dur_str}")
        reply_text = f"Записала! Эрик проснулся.{dur_str}"

    # 4. Сон - уснул
    elif any(k in command for k in ["уснул", "заснул", "спит", "уложить"]):
        db_set_state("is_sleeping", "1")
        db_log_event(0, "Алиса (Голос)", "sleep", "Уснул")
        broadcast_message(f"😴 <b>Голосовая отметка через Алису:</b> Эрик <b>уснул 💤</b> в {now_time}.")
        reply_text = "Записала! Спокойного сна малышу Эрику. Включить белый шум?"
        tts_text = "<speaker is_whisper=\"true\">Записала, спокойного сна малышу Эрику. Включить белый шум?<speaker is_whisper=\"false\">"

    # 5. Вопрос о сне
    elif any(k in command for k in ["спал", "сон"]) and any(k in command for k in ["когда", "сколько", "как"]):
        sleep_state = db_get_state("is_sleeping")
        is_sleeping = sleep_state and sleep_state[0] == "1"
        last_sleep = db_get_last_event("sleep")
        if is_sleeping:
            since = format_time_diff(last_sleep[0]) if last_sleep else "только что"
            reply_text = f"Эрик сейчас спит. Уснул {since}."
            tts_text = f"<speaker is_whisper=\"true\">Эрик сейчас спит. Уснул {since}.<speaker is_whisper=\"false\">"
        else:
            since = format_time_diff(last_sleep[0]) if last_sleep else "недавно"
            reply_text = f"Эрик сейчас бодрствует. Проснулся {since}."

    # 6. Кормление - вопрос
    elif any(k in command for k in ["ел", "кушал", "поел", "кормлен", "еда", "кормили"]) and any(k in command for k in ["когда", "сколько", "во сколько"]):
        last_feed = db_get_last_event("feed")
        feed_int = float((db_get_state("feed_interval_hours") or ["3.0"])[0])
        if last_feed:
            t_str = last_feed[0].split()[1][:5]
            diff = format_time_diff(last_feed[0])
            val = f", {last_feed[4]} мл" if last_feed[4] else ""
            try:
                t = datetime.datetime.fromisoformat(last_feed[0])
                next_f = (t + datetime.timedelta(hours=feed_int)).strftime("%H:%M")
                reply_text = f"Последнее кормление было в {t_str} ({diff}){val}. Следующее ориентировочно в {next_f}. Не забудьте перед едой выложить на животик!"
            except Exception:
                reply_text = f"Последнее кормление было в {t_str} ({diff}){val}."
        else:
            reply_text = "Сегодня кормлений в дневнике пока не отмечено."

    # 7. Кормление - отметка
    elif any(k in command for k in ["покормил", "съел", "выпил", "кормление", "грудь", "поел", "покушал", "бутылочк"]):
        nums = re.findall(r'\d+', command)
        ml_val = nums[0] if nums else "100"
        db_log_event(0, "Алиса (Голос)", "feed", f"Смесь {ml_val} мл", ml_val)
        feed_int = float((db_get_state("feed_interval_hours") or ["3.0"])[0])
        next_t = (msk_now() + datetime.timedelta(hours=feed_int)).strftime("%H:%M")
        
        now_dt = msk_now()
        today_date_str = str(now_dt.date())
        morning_care_done = db_get_state("morning_care_sent_date")
        morning_care_block = ""
        if "07:00" <= now_time <= "10:00" and (not morning_care_done or morning_care_done[0] != today_date_str):
            age_str = get_erik_age_str()
            morning_care_block = (
                f"\n\n☀️ <b>Утренний уход (сегодня Эрику {age_str}):</b>\n"
                f"• 👃 <b>Аквамарис в носик:</b> закапать и прочистить ватными жгутиками.\n"
                f"• 💊 <b>Витамин D:</b> дать 1 каплю во время или после еды."
            )
            db_set_state("morning_care_sent_date", today_date_str)

        broadcast_message(f"🍼 <b>Голосовая отметка через Алису:</b> Кормление {ml_val} мл в {now_time}.\n⏳ Следующее кормление в ~{next_t}.{morning_care_block}")
        reply_text = f"Записала кормление {ml_val} мл в {now_time}. Следующее кормление примерно в {next_t}."

    # 8. Витамин D
    elif any(k in command for k in ["витамин", "витамин д"]):
        db_log_event(0, "Алиса (Голос)", "care", "Дали Витамин D")
        broadcast_message(f"☀️ <b>Голосовая отметка через Алису:</b> Дали Витамин D в {now_time}.")
        reply_text = "Отлично! Записала прием витамина D."

    # 9. Подгузник
    elif any(k in command for k in ["подгузник", "памперс"]):
        db_log_event(0, "Алиса (Голос)", "diaper", "Смена подгузника")
        broadcast_message(f"👶 <b>Голосовая отметка через Алису:</b> Смена подгузника в {now_time}.")
        reply_text = "Записала смену подгузника!"

    elif session.get("new") and not command:
        reply_text = "Привет! Я Дневник малыша Эрика. Вы можете спросить, когда он ел или спал, спросить о купании, либо сказать: 'покормили 120 мл', 'Эрик уснул' или 'дали витамин Д'."
    else:
        reply_text = "Я могу записать кормление, сон, напомнить правила купания или сказать, когда Эрик ел. Что подсказать?"

    return JSONResponse({
        "response": {
            "text": reply_text,
            "tts": tts_text or reply_text,
            "end_session": False
        },
        "version": version
    })

@app.get("/")
def health():
    return {"status": "ok", "app": "ErikBabyTracker", "time": msk_now().isoformat()}

@app.get("/ping")
def ping():
    return {"status": "ok", "pong": time.time()}

# --- TELEGRAM WEBHOOK (Wakes up Render instantly on any message) ---
@app.post("/telegram")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        if "message" in data:
            handle_message(data["message"])
        elif "callback_query" in data:
            handle_callback(data["callback_query"])
    except Exception as e:
        print(f"Telegram webhook error: {e}")
    return {"ok": True}

# --- REST API FOR IPHONE WIDGETS & SHORTCUTS (Команды iOS) ---
@app.get("/api/feed")
def api_feed(user: str = "Родитель", ml: int = 90, breast: str = ""):
    now_time = msk_now().strftime("%H:%M")
    val_name = f"🤱 Грудь ({breast})" if breast else f"🍼 Смесь {ml} мл"
    db_log_event(0, f"{user} (Виджет)", "feed", val_name, str(ml) if ml else "")
    feed_int = float((db_get_state("feed_interval_hours") or ["3.0"])[0])
    next_t = (msk_now() + datetime.timedelta(hours=feed_int)).strftime("%H:%M")
    announcement = (
        f"🍼 <b>Виджет iPhone ({user}):</b> {val_name} в {now_time}.\n"
        f"⏳ Следующее кормление ориентировочно в ~{next_t}."
    )
    broadcast_message(announcement)
    return {"status": "ok", "action": "feed", "value": val_name, "next_feed": next_t}

@app.get("/api/sleep")
def api_sleep(user: str = "Родитель", action: str = "toggle"):
    now_time = msk_now().strftime("%H:%M")
    sleep_state = db_get_state("is_sleeping")
    is_sleeping = sleep_state and sleep_state[0] == "1"
    if action == "wake" or (action == "toggle" and is_sleeping):
        db_set_state("is_sleeping", "0")
        last_sleep = db_get_last_event("sleep", "Уснул")
        dur_str = f" Поспал {format_time_diff(last_sleep[0]).replace(' назад', '')}." if last_sleep else ""
        db_log_event(0, f"{user} (Виджет)", "sleep", "Проснулся", dur_str)
        broadcast_message(f"☀️ <b>Виджет iPhone ({user}):</b> Эрик <b>проснулся!</b> в {now_time}.{dur_str}")
        return {"status": "ok", "state": "awake", "time": now_time}
    else:
        db_set_state("is_sleeping", "1")
        db_log_event(0, f"{user} (Виджет)", "sleep", "Уснул")
        broadcast_message(f"😴 <b>Виджет iPhone ({user}):</b> Эрик <b>уснул 💤</b> в {now_time}.")
        return {"status": "ok", "state": "sleeping", "time": now_time}

@app.get("/api/diaper")
def api_diaper(user: str = "Родитель", type: str = "wet"):
    now_time = msk_now().strftime("%H:%M")
    d_name = "💧 Мокрый" if type == "wet" else ("💩 Грязный" if type == "dirty" else "💧+💩 Смешанный")
    db_log_event(0, f"{user} (Виджет)", "diaper", d_name)
    broadcast_message(f"👶 <b>Виджет iPhone ({user}):</b> Смена подгузника — {d_name} в {now_time}.")
    return {"status": "ok", "action": "diaper", "value": d_name}

@app.get("/api/status")
def api_status():
    age_str = get_erik_age_str()
    sleep_state = db_get_state("is_sleeping")
    is_sleeping = sleep_state and sleep_state[0] == "1"
    last_feed = db_get_last_event("feed")
    feed_str = f"{last_feed[0].split()[1][:5]} ({last_feed[4]} мл)" if last_feed else "нет"
    return {
        "status": "ok",
        "age": age_str,
        "is_sleeping": is_sleeping,
        "state_text": "Спит" if is_sleeping else "Бодрствует",
        "last_feed": feed_str
    }

@app.get("/export.xlsx")
def export_excel():
    excel_bytes = generate_excel_bytes()
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=erik_baby_tracker.xlsx"}
    )

def tg_polling_worker():
    offset = 0
    while True:
        try:
            res = tg_send("getUpdates", {"offset": offset, "timeout": 20})
            if res and res.get("ok"):
                for upd in res.get("result", []):
                    offset = upd["update_id"] + 1
                    if "message" in upd:
                        handle_message(upd["message"])
                    elif "callback_query" in upd:
                        handle_callback(upd["callback_query"])
            time.sleep(0.5)
        except Exception:
            time.sleep(5)

def reminder_worker():
    last_feed_alert = 0

    while True:
        try:
            now = msk_now()
            today_date = msk_today()
            today_date_str = str(today_date)
            time_now_str = now.strftime("%H:%M")

            # 1. Вечернее купание и вечерний уход в заданное время (по умолчанию 20:50)
            bath_time_cfg = (db_get_state("bath_time") or ["20:50"])[0]
            bath_done = db_get_state("bath_reminder_sent_date")
            if time_now_str == bath_time_cfg and (not bath_done or bath_done[0] != today_date_str):
                weekday = now.weekday()
                is_shampoo = weekday in [2, 5] # Среда=2, Суббота=5
                
                shampoo_alert = "🧴 <b>СЕГОДНЯ МОЕМ С ШАМПУНЕМ</b> (среда / суббота, 2 раза в неделю)!" if is_shampoo else "💧 <b>Сегодня купаем БЕЗ шампуня</b> (просто в чистой тёплой воде)."
                
                bath_msg = (
                    f"🛁 <b>ПОРА КУПАТЬ ЭРИКА И ВЕЧЕРНИЙ УХОД ({bath_time_cfg})</b>\n\n"
                    f"🌡️ <b>Температура воды:</b> строго <b>36–37 °C</b> (проверьте водным термометром перед купанием).\n"
                    f"{shampoo_alert}\n\n"
                    f"✨ <b>После купания (вечерний уход):</b>\n"
                    f"• 🧢 <b>ОБЯЗАТЕЛЬНО НАДЕТЬ ШАПОЧКУ</b>, чтобы не простудить ушки.\n"
                    f"• 👃 <b>Аквамарис в носик:</b> закапать перед сном для лёгкого дыхания.\n"
                    f"• 🧴 <b>Массаж с маслом Johnson's Baby:</b> мягко смазать тело и складочки.\n"
                    f"<i>(Витамин D вечером не даём — только утром)</i>"
                )
                broadcast_message(bath_msg)
                db_set_state("bath_reminder_sent_date", today_date_str)

            # 2. Напоминание о кормлении (по настраиваемому интервалу)
            last_feed = db_get_last_event("feed")
            feed_int = float((db_get_state("feed_interval_hours") or ["3.0"])[0])
            morning_care_done = db_get_state("morning_care_sent_date")
            is_morning_window = ("07:00" <= time_now_str <= "10:00")
            
            feed_due = False
            diff_h = 0
            if last_feed:
                try:
                    f_dt = datetime.datetime.fromisoformat(last_feed[0])
                    diff_h = (now - f_dt).total_seconds() / 3600
                    if diff_h >= feed_int and (time.time() - last_feed_alert) > 3600:
                        feed_due = True
                except Exception:
                    pass
            elif is_morning_window and (time.time() - last_feed_alert) > 7200:
                feed_due = True

            if feed_due:
                # Добавляем утренний уход, если он попадает в окно 07:00-10:00 и еще не отправлялся сегодня
                morning_block = ""
                if is_morning_window and (not morning_care_done or morning_care_done[0] != today_date_str):
                    age_str = get_erik_age_str()
                    morning_block = (
                        f"\n\n☀️ <b>Утренний уход (сегодня Эрику {age_str}):</b>\n"
                        f"• 👃 <b>Аквамарис в носик:</b> закапать и аккуратно очистить ватными жгутиками.\n"
                        f"• 💊 <b>Витамин D:</b> дать 1 каплю во время или после кормления."
                    )
                    db_set_state("morning_care_sent_date", today_date_str)
                    morning_care_done = [today_date_str]

                passed_str = f"С прошлого раза прошло уже <b>{int(diff_h)} ч {int((diff_h%1)*60)} мин</b>.\n\n" if diff_h > 0 else ""
                feed_msg = (
                    f"🍼 <b>ВРЕМЯ КОРМЛЕНИЯ!</b>\n"
                    f"{passed_str}"
                    f"💡 <b>Перед кормлением обязательно:</b>\n"
                    f"1. 🤸 <b>Выложить на животик на 5–10 минут</b> (профилактика коликов и газиков).\n"
                    f"2. 👶 <b>Проверить и поменять подгузник</b> (чтобы малыш кушал в сухом и спокойном состоянии)."
                    f"{morning_block}"
                )
                broadcast_message(feed_msg)
                last_feed_alert = time.time()

            # 3. Страховка утреннего ухода в конце утреннего окна (в 09:45), если напоминание о кормлении так и не сработало
            morning_care_done = db_get_state("morning_care_sent_date")
            if "09:45" <= time_now_str <= "10:00" and (not morning_care_done or morning_care_done[0] != today_date_str):
                age_str = get_erik_age_str()
                safety_msg = (
                    f"☀️ <b>Доброе утро! Время утреннего ухода и кормления:</b>\n"
                    f"👶 Сегодня Эрику: <b>{age_str}</b>.\n\n"
                    f"• 👃 <b>Аквамарис в носик:</b> закапать и аккуратно прочистить ватными жгутиками.\n"
                    f"• 💊 <b>Витамин D:</b> дать 1 каплю к утреннему кормлению.\n"
                    f"• 🤸 <i>Перед едой не забудьте выложить на животик и сменить подгузник!</i>"
                )
                broadcast_message(safety_msg)
                db_set_state("morning_care_sent_date", today_date_str)

        except Exception as e:
            print(f"Reminder loop error: {e}")
        time.sleep(45)

@app.on_event("startup")
def startup():
    init_db()
    # Auto-register Telegram Webhook so Telegram immediately wakes up Render
    try:
        wh_url = "https://erik-baby-bot.onrender.com/telegram"
        r = requests.post(f"{API_URL}setWebhook", json={"url": wh_url}, timeout=10)
        print("Webhook set response:", r.json())
    except Exception as e:
        print("Failed to set webhook, falling back to polling:", e)
        threading.Thread(target=tg_polling_worker, daemon=True).start()

    threading.Thread(target=reminder_worker, daemon=True).start()
    print("Erik Baby Tracker & Alice Skill service started!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
