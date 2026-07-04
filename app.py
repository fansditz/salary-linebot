import base64
import hashlib
import hmac
import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from dotenv import load_dotenv
from flask import Flask, abort, request


load_dotenv()


DATABASE_URL = os.getenv("DATABASE_PATH", "salary_linebot.db")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://salary-linebot-opnl.onrender.com")
LINE_WEBHOOK_URL = os.getenv("LINE_WEBHOOK_URL", f"{APP_BASE_URL.rstrip('/')}/webhook")
TAIPEI_TZ = timezone(timedelta(hours=8))

COMMAND_SETUP_MENU = "設定"
COMMAND_SETUP = "設定工作"
COMMAND_PAY_MENU = "記薪"
COMMAND_CLOCK_IN = "社畜人來打卡啦！"
COMMAND_SALARY = "偷偷給我看一下薪水吧......"


app = Flask(__name__)


def get_connection():
    conn = sqlite3.connect(DATABASE_URL)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn, table_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def init_db():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                line_user_id TEXT PRIMARY KEY,
                work_name TEXT,
                hourly_wage TEXT,
                period_start TEXT,
                period_end TEXT,
                payday TEXT,
                state TEXT,
                state_data TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS time_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                line_user_id TEXT NOT NULL,
                work_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                break_hours TEXT NOT NULL DEFAULT '0',
                break_paid INTEGER NOT NULL,
                work_hours TEXT NOT NULL,
                daily_salary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (line_user_id) REFERENCES users(line_user_id)
            )
            """
        )

        user_columns = table_columns(conn, "users")
        if "work_name" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN work_name TEXT")

        entry_columns = table_columns(conn, "time_entries")
        if "break_hours" not in entry_columns:
            conn.execute("ALTER TABLE time_entries ADD COLUMN break_hours TEXT NOT NULL DEFAULT '0'")


def now_iso():
    return datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")


def money(value):
    amount = Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{amount:,}"


def decimal_text(value, places="0.01"):
    return str(Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def get_or_create_user(line_user_id):
    now = now_iso()
    with get_connection() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE line_user_id = ?", (line_user_id,)
        ).fetchone()
        if user:
            return dict(user)

        conn.execute(
            """
            INSERT INTO users (line_user_id, state_data, created_at, updated_at)
            VALUES (?, '{}', ?, ?)
            """,
            (line_user_id, now, now),
        )
        user = conn.execute(
            "SELECT * FROM users WHERE line_user_id = ?", (line_user_id,)
        ).fetchone()
        return dict(user)


def update_user(line_user_id, **fields):
    if not fields:
        return
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [line_user_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE users SET {assignments} WHERE line_user_id = ?", values)


def set_state(line_user_id, state, data=None):
    update_user(
        line_user_id,
        state=state,
        state_data=json.dumps(data or {}, ensure_ascii=False),
    )


def clear_state(line_user_id):
    set_state(line_user_id, None, {})


def parse_state_data(user):
    try:
        return json.loads(user.get("state_data") or "{}")
    except json.JSONDecodeError:
        return {}


def parse_work_name(text):
    value = text.strip()
    if not value:
        raise ValueError("請輸入工作名稱，例如 STBX。")
    return value[:40]


def parse_positive_decimal(text, field_name):
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{field_name}請輸入數字，例如 196。")
    if value <= 0:
        raise ValueError(f"{field_name}必須大於 0。")
    return decimal_text(value)


def parse_non_negative_decimal(text, field_name):
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{field_name}請輸入數字，例如 0.5。")
    if value < 0:
        raise ValueError(f"{field_name}不能小於 0。")
    return decimal_text(value)


def parse_day_of_month(text, field_name):
    try:
        value = int(text.strip())
    except (ValueError, AttributeError):
        raise ValueError(f"{field_name}請輸入 1 到 31 的日期，例如 25。")
    if value < 1 or value > 31:
        raise ValueError(f"{field_name}請輸入 1 到 31 的日期。")
    return f"{value:02d}"


def parse_compact_date(text, field_name="日期"):
    try:
        return datetime.strptime(text.strip(), "%Y%m%d").date()
    except (ValueError, AttributeError):
        raise ValueError(f"{field_name}請使用 YYYYMMDD 格式，例如 20260705。")


def parse_hhmm_time(text, field_name="時間"):
    value = text.strip()
    try:
        if len(value) == 4 and value.isdigit():
            return datetime.strptime(value, "%H%M").time()
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        raise ValueError(f"{field_name}請輸入 24 小時制，例如 0900 或 18:30。")


def parse_yes_no(text):
    normalized = text.strip().lower()
    if normalized in {"y", "yes", "是", "有", "會", "記薪"}:
        return True
    if normalized in {"n", "no", "否", "沒有", "不會", "不記薪"}:
        return False
    raise ValueError("請輸入 Y 或 N。")


def format_hours(hours):
    return Decimal(str(hours)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_work(work_date, start_time, end_time, break_hours, break_paid, hourly_wage):
    start_dt = datetime.combine(work_date, start_time)
    end_dt = datetime.combine(work_date, end_time)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)

    total_hours = Decimal(str((end_dt - start_dt).total_seconds())) / Decimal(3600)
    paid_hours = total_hours if break_paid else total_hours - Decimal(str(break_hours))
    if paid_hours <= 0:
        raise ValueError("扣除休息時間後，工時必須大於 0。")

    daily_salary = paid_hours * Decimal(str(hourly_wage))
    return decimal_text(paid_hours), decimal_text(daily_salary)


def current_month_range():
    today = datetime.now(TAIPEI_TZ).date()
    month_start = today.replace(day=1)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    return month_start.isoformat(), next_month.isoformat()


def save_time_entry(line_user_id, data, work_hours, daily_salary):
    with get_connection() as conn:
        columns = table_columns(conn, "time_entries")
        fields = [
            "line_user_id",
            "work_date",
            "start_time",
            "end_time",
            "break_hours",
            "break_paid",
            "work_hours",
            "daily_salary",
            "created_at",
        ]
        values = [
            line_user_id,
            data["work_date"],
            data["start_time"],
            data["end_time"],
            data["break_hours"],
            1 if data["break_paid"] else 0,
            work_hours,
            daily_salary,
            now_iso(),
        ]
        if "break_minutes" in columns:
            fields.insert(5, "break_minutes")
            minutes = int((Decimal(data["break_hours"]) * Decimal(60)).to_integral_value())
            values.insert(5, minutes)

        placeholders = ", ".join("?" for _ in fields)
        conn.execute(
            f"INSERT INTO time_entries ({', '.join(fields)}) VALUES ({placeholders})",
            values,
        )


def ensure_profile(user):
    required = ("work_name", "hourly_wage", "period_start", "period_end", "payday")
    return all(user.get(key) for key in required)


def start_setup(line_user_id):
    set_state(line_user_id, "setup_work_name")
    return "請輸入工作名稱，例如 STBX。"


def start_clock_in(line_user_id, user):
    if not ensure_profile(user):
        return "請先使用「設定」完成工作設定。"
    set_state(line_user_id, "entry_work_date")
    return "請輸入上班日期，格式為 YYYYMMDD，例如 20260705。"


def handle_setup_flow(line_user_id, state, text):
    user = get_or_create_user(line_user_id)
    data = parse_state_data(user)

    if state == "setup_work_name":
        data["work_name"] = parse_work_name(text)
        set_state(line_user_id, "setup_period_start", data)
        return "請輸入起始日，例如 05。"

    if state == "setup_period_start":
        data["period_start"] = parse_day_of_month(text, "起始日")
        set_state(line_user_id, "setup_period_end", data)
        return "請輸入結算日，例如 25。"

    if state == "setup_period_end":
        data["period_end"] = parse_day_of_month(text, "結算日")
        set_state(line_user_id, "setup_payday", data)
        return "請輸入發薪日，例如 30。"

    if state == "setup_payday":
        data["payday"] = parse_day_of_month(text, "發薪日")
        set_state(line_user_id, "setup_hourly_wage", data)
        return "請輸入時薪，例如 196。"

    if state == "setup_hourly_wage":
        data["hourly_wage"] = parse_positive_decimal(text, "時薪")
        update_user(
            line_user_id,
            work_name=data["work_name"],
            hourly_wage=data["hourly_wage"],
            period_start=data["period_start"],
            period_end=data["period_end"],
            payday=data["payday"],
            state=None,
            state_data="{}",
        )
        return (
            "設定完成\n"
            f"工作：{data['work_name']}\n"
            f"起始日：{data['period_start']}\n"
            f"結算日：{data['period_end']}\n"
            f"發薪日：{data['payday']}\n"
            f"時薪：{money(data['hourly_wage'])} 元"
        )

    return None


def handle_entry_flow(line_user_id, state, text):
    user = get_or_create_user(line_user_id)
    data = parse_state_data(user)

    if state == "entry_work_date":
        work_date = parse_compact_date(text, "上班日期")
        data["work_date"] = work_date.isoformat()
        set_state(line_user_id, "entry_start_time", data)
        return "請輸入上班時間，例如 0900。"

    if state == "entry_start_time":
        start_time = parse_hhmm_time(text, "上班時間")
        data["start_time"] = start_time.strftime("%H:%M")
        set_state(line_user_id, "entry_end_time", data)
        return "請輸入下班時間，例如 1830。"

    if state == "entry_end_time":
        end_time = parse_hhmm_time(text, "下班時間")
        data["end_time"] = end_time.strftime("%H:%M")
        set_state(line_user_id, "entry_break_hours", data)
        return "請輸入休息時間，以小時計算，例如 0.5。"

    if state == "entry_break_hours":
        data["break_hours"] = parse_non_negative_decimal(text, "休息時間")
        set_state(line_user_id, "entry_break_paid", data)
        return "休息時間是否記薪？請輸入 Y 或 N。"

    if state == "entry_break_paid":
        data["break_paid"] = parse_yes_no(text)
        work_date = date.fromisoformat(data["work_date"])
        start_time = time.fromisoformat(data["start_time"])
        end_time = time.fromisoformat(data["end_time"])
        work_hours, daily_salary = calculate_work(
            work_date,
            start_time,
            end_time,
            data["break_hours"],
            data["break_paid"],
            user["hourly_wage"],
        )
        save_time_entry(line_user_id, data, work_hours, daily_salary)
        clear_state(line_user_id)
        return (
            "打卡完成\n"
            f"上班日期：{data['work_date']}\n"
            f"上班時間：{data['start_time']}\n"
            f"下班時間：{data['end_time']}\n"
            f"休息時間：{format_hours(data['break_hours'])} 小時\n"
            f"休息記薪：{'Y' if data['break_paid'] else 'N'}\n"
            f"工時：{format_hours(work_hours)} 小時\n"
            f"日薪：{money(daily_salary)} 元"
        )

    return None


def salary_summary(line_user_id, user):
    if not ensure_profile(user):
        return "請先使用「設定」完成工作設定。"

    month_start, next_month = current_month_range()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(work_hours), 0) AS hours,
                   COALESCE(SUM(daily_salary), 0) AS salary
            FROM time_entries
            WHERE line_user_id = ? AND work_date >= ? AND work_date < ?
            """,
            (line_user_id, month_start, next_month),
        ).fetchone()

    month_label = month_start[:7]
    return (
        f"{month_label} 目前薪資\n"
        f"工作：{user['work_name']}\n"
        f"打卡筆數：{row['count']} 筆\n"
        f"總工時：{format_hours(row['hours'])} 小時\n"
        f"月薪：{money(row['salary'])} 元"
    )


def setup_menu_message():
    return {
        "type": "template",
        "altText": "設定",
        "template": {
            "type": "carousel",
            "columns": [
                {
                    "title": "設定工作",
                    "text": "設定工作名稱、起始日、結算日、發薪日與時薪。",
                    "actions": [
                        {
                            "type": "message",
                            "label": "設定工作",
                            "text": COMMAND_SETUP,
                        }
                    ],
                }
            ],
        },
    }


def pay_menu_message():
    return {
        "type": "template",
        "altText": "記薪",
        "template": {
            "type": "carousel",
            "columns": [
                {
                    "title": "記錄打卡",
                    "text": "輸入日期、上下班時間、休息時間並計算日薪。",
                    "actions": [
                        {
                            "type": "message",
                            "label": "我要打卡",
                            "text": COMMAND_CLOCK_IN,
                        }
                    ],
                },
                {
                    "title": "查看薪水",
                    "text": "統整本月目前總工時與月薪。",
                    "actions": [
                        {
                            "type": "message",
                            "label": "查看薪水",
                            "text": COMMAND_SALARY,
                        }
                    ],
                },
            ],
        },
    }


def handle_text_message(line_user_id, text):
    user = get_or_create_user(line_user_id)
    text = text.strip()

    state = user.get("state")
    try:
        if state and state.startswith("setup_"):
            return handle_setup_flow(line_user_id, state, text)
        if state and state.startswith("entry_"):
            return handle_entry_flow(line_user_id, state, text)
    except ValueError as exc:
        return str(exc)

    if text == COMMAND_SETUP_MENU:
        return setup_menu_message()
    if text == COMMAND_SETUP:
        return start_setup(line_user_id)
    if text == COMMAND_PAY_MENU:
        return pay_menu_message()
    if text == COMMAND_CLOCK_IN:
        return start_clock_in(line_user_id, user)
    if text == COMMAND_SALARY:
        return salary_summary(line_user_id, user)

    return "請使用選單功能。"


def verify_line_signature(body, signature):
    if not LINE_CHANNEL_SECRET:
        return False
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature or "")


def normalize_reply_messages(reply):
    if isinstance(reply, list):
        return reply
    if isinstance(reply, dict):
        return [reply]
    return [{"type": "text", "text": str(reply)[:5000]}]


def reply_message(reply_token, reply):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        app.logger.warning("LINE_CHANNEL_ACCESS_TOKEN is not set; skipped reply.")
        return
    payload = json.dumps(
        {
            "replyToken": reply_token,
            "messages": normalize_reply_messages(reply),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    line_request = urllib.request.Request(
        "https://api.line.me/v2/bot/message/reply",
        data=payload,
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(line_request, timeout=10):
            return
    except urllib.error.HTTPError as exc:
        app.logger.error("LINE reply failed: %s %s", exc.code, exc.read().decode())
        raise


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "app_base_url": APP_BASE_URL,
        "webhook_url": LINE_WEBHOOK_URL,
    }


@app.post("/webhook")
def webhook():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")
    if not verify_line_signature(body, signature):
        abort(400)

    payload = request.get_json(silent=True) or {}
    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue
        message = event.get("message", {})
        if message.get("type") != "text":
            continue
        source = event.get("source", {})
        line_user_id = source.get("userId")
        reply_token = event.get("replyToken")
        if not line_user_id or not reply_token:
            continue
        reply_text = handle_text_message(line_user_id, message.get("text", ""))
        reply_message(reply_token, reply_text)
    return "OK"


init_db()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
