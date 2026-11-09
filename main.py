import sys, os, time, datetime, json, sqlite3, threading, re, io
import requests
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

TOKEN = "9030754644:AAHxX2fIejBORcD15LJTMtmz4QLlXEr9iqk"
API_URL = f"https://api.telegram.org/bot{TOKEN}/"
DB_PATH = os.path.join(os.path.dirname(__file__), "erik_tracker.db")

app = FastAPI(title="Erik Baby Tracker & Alice Skill")

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
        VALUES (datetime('now', 'localtime'), ?, ?, ?, ?, ?, ?)
    """, (user_id, user_name, category, sub_type, value, notes))
    conn.commit()
    conn.close()

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
    cur.execute("""
        SELECT category, sub_type, value, timestamp 
        FROM events 
        WHERE date(timestamp) = date('now', 'localtime')
        ORDER BY timestamp ASC
    """)
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
        r = requests.post(API_URL + method, json=data, timeout=15)
        return r.json()
    except Exception as e:
        print(f"API Error ({method}): {e}")
        return None

def tg_send_document(chat_id, filename, file_bytes, caption=""):
    try:
        files = {"document": (filename, file_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        requests.post(API_URL + "sendDocument", data=data, files=files, timeout=25)
    except Exception as e:
        print(f"Error sending document: {e}")

def broadcast_message(text, exclude_chat_id=None, reply_markup=None):
    for cid in db_get_subscribers():
        if exclude_chat_id and cid == exclude_chat_id:
            continue
        msg = {"chat_id": cid, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            msg["reply_markup"] = reply_markup
        tg_send("sendMessage", msg)

def format_time_diff(dt_str):
    try:
        t = datetime.datetime.fromisoformat(dt_str)
        now = datetime.datetime.now()
        diff = now - t
        total_minutes = int(diff.total_seconds() // 60)
        hours = total_minutes // 60
        mins = total_minutes % 60
        if hours > 0:
            return f"{hours} ч {mins} мин назад"
        return f"{mins} мин назад"
    except Exception:
        return "недавно"

def format_summary():
    now_str = datetime.datetime.now().strftime("%H:%M, %d.%m.%Y")
    text = f"<b>📊 Статус малыша Эрика на {now_str}:</b>\n\n"
    
    sleep_state = db_get_state("is_sleeping")
    is_sleeping = sleep_state and sleep_state[0] == "1"
    last_sleep = db_get_last_event("sleep")
    if is_sleeping:
        since = format_time_diff(last_sleep[0]) if last_sleep else "только что"
        text += f"😴 <b>Сон:</b> Эрик сейчас <b>спит</b> (уснул {since}).\n"
    else:
        wake_since = format_time_diff(last_sleep[0]) if last_sleep else "неизвестно"
        text += f"☀️ <b>Сон:</b> Бодрствует (проснулся {wake_since}).\n"
        
    last_feed = db_get_last_event("feed")
    if last_feed:
        t_str = last_feed[0].split()[1][:5]
        diff = format_time_diff(last_feed[0])
        val = f" ({last_feed[4]})" if last_feed[4] else ""
        text += f"🍼 <b>Кормление:</b> Было в {t_str} ({diff}){val}.\n"
        try:
            t = datetime.datetime.fromisoformat(last_feed[0])
            next_f = (t + datetime.timedelta(hours=3)).strftime("%H:%M")
            text += f"⏳ <i>Следующее ориентировочно в {next_f}.</i>\n"
        except Exception:
            pass
    else:
        text += "🍼 <b>Кормление:</b> Сегодня ещё не отмечали.\n"
        
    rows = db_get_today_summary()
    diapers = [r for r in rows if r[0] == "diaper"]
    bottles_ml = sum(int(r[2]) for r in rows if r[0] == "feed" and str(r[2]).isdigit())
    vit_d = any(r[1] == "care_vitd" for r in rows)
    
    text += f"\n<b>👶 За сегодня:</b>\n"
    text += f"• Подгузников: <b>{len(diapers)}</b> шт.\n"
    if bottles_ml > 0:
        text += f"• Смеси выпито: <b>{bottles_ml}</b> мл.\n"
    text += f"• Витамин D: {'✅ <b>Дали</b>' if vit_d else '❌ <i>Ещё не давали</i>'}\n"
    
    return text

def generate_excel_bytes():
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Журнал событий"
    headers1 = ["Дата и время", "Категория", "Действие", "Кто отметил", "Значение / Объём", "Примечание"]
    ws1.append(headers1)
    
    header_fill = PatternFill(start_color="3B7A57", end_color="3B7A57", fill_type="solid")
    header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
    
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
    
    rows = db_get_all_events()
    for row in rows:
        ts, uname, cat, sub, val, notes = row
        cat_title = category_map.get(cat, cat)
        ws1.append([ts, cat_title, sub, uname, val or "", notes or ""])
        
    for col in ws1.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws1.column_dimensions[col_letter].width = max(max_len + 4, 15)
        
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()

def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "🍼 Кормление"}, {"text": "😴 Сон"}],
            [{"text": "👶 Подгузник"}, {"text": "💊 Витамин D и Уход"}],
            [{"text": "📊 Статус и Сводка"}, {"text": "📁 Скачать Excel"}],
            [{"text": "💡 Подсказки"}]
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
            [{"text": "🛁 Искупали", "callback_data": "care_bath"}],
            [{"text": "💨 Капли от колик", "callback_data": "care_colic"}],
            [{"text": "🌡️ Измерили температуру", "callback_data": "care_temp"}],
            [{"text": "❌ Закрыть", "callback_data": "close_menu"}]
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

    now_time = datetime.datetime.now().strftime("%H:%M")

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
        next_t = (datetime.datetime.now() + datetime.timedelta(hours=3)).strftime("%H:%M")
        announcement = f"🍼 <b>{user_name}:</b> {val_name} в {now_time}.\n⏳ Следующее кормление ориентировочно в ~{next_t}."
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
        announcement = f"☀️ <b>{user_name}:</b> Эрик <b>проснулся!</b> в {now_time}.{dur_str}\n<i>Окно бодрствования: до ~{(datetime.datetime.now() + datetime.timedelta(minutes=75)).strftime('%H:%M')}.</i>"
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
            "care_bath": "🛁 Искупали малыша",
            "care_colic": "💨 Дали капли от колик",
            "care_temp": "🌡️ Измерили температуру (норма)"
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

    if text.startswith("/start"):
        welcome = (
            f"👋 <b>Добро пожаловать в Дневник малыша Эрика!</b>\n\n"
            f"Этот бот помогает родителям вести единый журнал кормлений, сна и ухода.\n"
            f"Все отметки моментально синхронизируются и видны вам обоим на iPhone 14 Pro Max!\n\n"
            f"<b>Как пользоваться:</b>\n"
            f"• Нажимайте кнопки внизу для быстрого выбора.\n"
            f"• В любой момент жмите <b>📁 Скачать Excel</b>, чтобы выгрузить таблицу для врача.\n"
            f"• Жмите <b>📊 Статус</b>, чтобы узнать, когда Эрик ел или спал."
        )
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": welcome,
            "parse_mode": "HTML",
            "reply_markup": get_main_keyboard()
        })
    elif text == "🍼 Кормление":
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": "🍼 <b>Что и сколько покушал малыш Эрик?</b>",
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
            "text": "💊 <b>Здоровье и гигиена малыша:</b>",
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
        dt_file = datetime.date.today().strftime("%d_%m_%Y")
        filename = f"Дневник_Эрика_{dt_file}.xlsx"
        caption = "📊 <b>Актуальный дневник малыша Эрика в Excel</b>\nВы можете открыть его на iPhone или сохранить на Google Диск."
        tg_send_document(chat_id, filename, excel_bytes, caption)

    elif text == "💡 Подсказки":
        tips = (
            "<b>💡 Полезные подсказки для родителей малыша Эрика:</b>\n\n"
            "• <b>Окно бодрствования:</b> у новорождённых (1–2 мес) составляет всего <b>50–80 минут</b>.\n"
            "• <b>Интервал кормления:</b> обычно каждые <b>2.5 – 3.5 часа</b>.\n"
            "• <b>Колонка Алиса:</b> скажите <i>«Алиса, включи белый шум на 40 минут»</i> — это лучший звук для засыпания."
        )
        tg_send("sendMessage", {
            "chat_id": chat_id,
            "text": tips,
            "parse_mode": "HTML"
        })

# --- YANDEX ALICE WEBHOOK HANDLER ---
@app.post("/alice")
async def alice_webhook(request: Request):
    req_data = await request.json()
    command = req_data.get("request", {}).get("command", "").lower().strip()
    session = req_data.get("session", {})
    version = req_data.get("version", "1.0")

    reply_text = ""
    now_time = datetime.datetime.now().strftime("%H:%M")

    if session.get("new") and not command:
        reply_text = "Привет! Я Дневник малыша Эрика. Вы можете спросить, когда он ел или спал, либо сказать: 'покормили 120 мл', 'Эрик уснул' или 'Эрик проснулся'."
    elif any(k in command for k in ["проснул", "встал", "проснулась"]):
        db_set_state("is_sleeping", "0")
        last_sleep = db_get_last_event("sleep", "Уснул")
        dur_str = ""
        if last_sleep:
            dur_str = f" Поспал {format_time_diff(last_sleep[0]).replace(' назад', '')}."
        db_log_event(0, "Алиса (Голос)", "sleep", "Проснулся", dur_str)
        broadcast_message(f"☀️ <b>Голосовая отметка через Алису:</b> Эрик <b>проснулся!</b> в {now_time}.{dur_str}")
        reply_text = f"Записала! Эрик проснулся.{dur_str}"
    elif any(k in command for k in ["уснул", "заснул", "спит", "уложить"]):
        db_set_state("is_sleeping", "1")
        db_log_event(0, "Алиса (Голос)", "sleep", "Уснул")
        broadcast_message(f"😴 <b>Голосовая отметка через Алису:</b> Эрик <b>уснул 💤</b> в {now_time}.")
        reply_text = "Записала! Спокойного сна малышу Эрику. Включить белый шум?"
    elif any(k in command for k in ["спал", "сон"]) and any(k in command for k in ["когда", "сколько", "как"]):
        sleep_state = db_get_state("is_sleeping")
        is_sleeping = sleep_state and sleep_state[0] == "1"
        last_sleep = db_get_last_event("sleep")
        if is_sleeping:
            since = format_time_diff(last_sleep[0]) if last_sleep else "только что"
            reply_text = f"Эрик сейчас спит. Уснул {since}."
        else:
            since = format_time_diff(last_sleep[0]) if last_sleep else "недавно"
            reply_text = f"Эрик сейчас бодрствует. Проснулся {since}."
    elif any(k in command for k in ["ел", "кушал", "поел", "кормлен", "еда", "кормили"]) and any(k in command for k in ["когда", "сколько", "во сколько"]):
        last_feed = db_get_last_event("feed")
        if last_feed:
            t_str = last_feed[0].split()[1][:5]
            diff = format_time_diff(last_feed[0])
            val = f", {last_feed[4]} мл" if last_feed[4] else ""
            try:
                t = datetime.datetime.fromisoformat(last_feed[0])
                next_f = (t + datetime.timedelta(hours=3)).strftime("%H:%M")
                reply_text = f"Последнее кормление было в {t_str} ({diff}){val}. Следующее ориентировочно в {next_f}."
            except Exception:
                reply_text = f"Последнее кормление было в {t_str} ({diff}){val}."
        else:
            reply_text = "Сегодня кормлений в дневнике пока не отмечено."
    elif any(k in command for k in ["покормил", "съел", "выпил", "кормление", "грудь", "поел", "покушал", "бутылочк"]):
        nums = re.findall(r'\d+', command)
        ml_val = nums[0] if nums else "100"
        db_log_event(0, "Алиса (Голос)", "feed", f"Смесь {ml_val} мл", ml_val)
        next_t = (datetime.datetime.now() + datetime.timedelta(hours=3)).strftime("%H:%M")
        broadcast_message(f"🍼 <b>Голосовая отметка через Алису:</b> Кормление {ml_val} мл в {now_time}.\n⏳ Следующее кормление в ~{next_t}.")
        reply_text = f"Записала кормление {ml_val} мл в {now_time}. Следующее кормление примерно в {next_t}."
    elif any(k in command for k in ["витамин", "витамин д"]):
        db_log_event(0, "Алиса (Голос)", "care", "Дали Витамин D")
        broadcast_message(f"☀️ <b>Голосовая отметка через Алису:</b> Дали Витамин D в {now_time}.")
        reply_text = "Отлично! Записала прием витамина D."
    elif any(k in command for k in ["подгузник", "памперс"]):
        db_log_event(0, "Алиса (Голос)", "diaper", "Смена подгузника")
        broadcast_message(f"👶 <b>Голосовая отметка через Алису:</b> Смена подгузника в {now_time}.")
        reply_text = "Записала смену подгузника!"
    elif any(k in command for k in ["статус", "сводка", "как дела"]):
        rows = db_get_today_summary()
        diapers = [r for r in rows if r[0] == "diaper"]
        bottles_ml = sum(int(r[2]) for r in rows if r[0] == "feed" and str(r[2]).isdigit())
        reply_text = f"Сегодня Эрик сменил {len(diapers)} подгузников, съел {bottles_ml} миллилитров смеси. В дневнике всё зафиксировано!"
    else:
        reply_text = "Я могу записать кормление, сон, подгузник или сказать, когда Эрик ел. Что нужно сделать?"

    return JSONResponse({
        "response": {
            "text": reply_text,
            "tts": reply_text,
            "end_session": False
        },
        "version": version
    })

@app.get("/")
def health():
    return {"status": "ok", "app": "ErikBabyTracker", "time": datetime.datetime.now().isoformat()}

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
    last_vitd = None
    last_feed_alert = 0
    while True:
        try:
            now = datetime.datetime.now()
            if now.hour == 10 and last_vitd != now.date():
                rows = db_get_today_summary()
                if not any(r[1] == "care_vitd" for r in rows):
                    broadcast_message("☀️ <b>Доброе утро!</b> Напоминание: не забудьте дать малышу Эрику <b>Витамин D</b> 💊")
                    last_vitd = now.date()

            last_feed = db_get_last_event("feed")
            if last_feed:
                try:
                    f_dt = datetime.datetime.fromisoformat(last_feed[0])
                    diff_h = (now - f_dt).total_seconds() / 3600
                    if diff_h >= 3.5 and (time.time() - last_feed_alert) > 3600:
                        broadcast_message(f"🍼 <b>Внимание:</b> С последнего кормления прошло уже <b>{int(diff_h)} ч {int((diff_h%1)*60)} мин</b>. Пора покормить малыша!")
                        last_feed_alert = time.time()
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(60)

@app.on_event("startup")
def startup():
    init_db()
    threading.Thread(target=tg_polling_worker, daemon=True).start()
    threading.Thread(target=reminder_worker, daemon=True).start()
    print("Erik Baby Tracker & Alice Skill service started!")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
