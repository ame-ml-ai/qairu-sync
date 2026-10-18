"""
QAIRU notify: читает data.json и отправляет сообщения в Telegram через вебхук Make.

- 22:00 — сводка на завтра: пары, дедлайны на 7 дней, сколько дней до экзаменов и сессии.
- За 2 часа до каждого блока пар — напоминание.

GitHub Actions запускает скрипт часто, но может опаздывать или пропускать запуски.
Поэтому скрипт отправляет всё, что уже «должно было» уйти, и запоминает отправленное
в sent.json, чтобы не было дублей.

Переменные окружения: MAKE_WEBHOOK_URL
  MODE=auto (по умолчанию) | summary (сводка сейчас) | test (тестовое сообщение)
  DRY_RUN=1 — только напечатать сообщения, ничего не отправлять
"""

import json
import os
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import escape

TZ = timezone(timedelta(hours=5))
SUMMARY_TIME = (22, 0)          # время вечерней сводки
REMIND_BEFORE = timedelta(hours=2)
DEADLINE_WINDOW_DAYS = 7

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def send(text: str):
    if os.environ.get("DRY_RUN"):
        print("----\n" + text)
        return
    req = urllib.request.Request(
        os.environ["MAKE_WEBHOOK_URL"],
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print("Отправлено:", r.status, text.splitlines()[0])


def days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def until(d: date, today: date) -> str:
    n = (d - today).days
    return "сегодня" if n == 0 else "завтра" if n == 1 else f"через {days_word(n)}"


def period_on(data, day: date, kind: str):
    for p in data["periods"]:
        if p["type"] == kind and p["start"] <= day.isoformat() <= p["end"]:
            return p
    return None


def is_study_day(data, day: date) -> bool:
    """Пары идут, если день попадает в период «Занятия» и не праздник."""
    has_calendar = any(p["type"] == "classes" for p in data["periods"])
    in_classes = period_on(data, day, "classes") is not None or not has_calendar
    return in_classes and period_on(data, day, "holiday") is None


def classes_on(data, day: date):
    if not is_study_day(data, day):
        return []
    wd = day.isoweekday()
    return sorted((c for c in data["classes"] if c["weekday"] == wd), key=lambda c: c["start"])


def class_line(c) -> str:
    room = c["room"] if c["room"] == "Онлайн" else f"ауд. {c['room']}"
    kind = f" ({escape(c['lesson_type'])})" if c.get("lesson_type") else ""
    return f"• <b>{c['start']}–{c['end']}</b> {escape(c['subject'])}{kind}, {escape(room)}"


def build_summary(data, now: datetime) -> str:
    today = now.date()
    tomorrow = today + timedelta(days=1)
    lines = [f"🌙 <b>Завтра, {WEEKDAYS[tomorrow.weekday()]} {tomorrow:%d.%m}</b>", ""]

    holiday = period_on(data, tomorrow, "holiday")
    classes = classes_on(data, tomorrow)
    lines.append("📚 <b>Пары</b>")
    if holiday:
        lines.append(f"Выходной: {escape(holiday['title'])} 🎉")
    elif classes:
        lines += [class_line(c) for c in classes]
    else:
        lines.append("Пар нет 🎉")

    lines += ["", f"⏰ <b>Дедлайны на {DEADLINE_WINDOW_DAYS} дней</b>"]
    upcoming = []
    for d in data["deadlines"]:
        due = datetime.fromisoformat(d["date"])
        if now <= due <= now + timedelta(days=DEADLINE_WINDOW_DAYS):
            upcoming.append((due, d))
    if upcoming:
        for due, d in sorted(upcoming, key=lambda x: x[0]):
            lines.append(f"• {escape(d['title'])} — {escape(d['subject'])}, "
                         f"до {due:%d.%m %H:%M} ({until(due.date(), today)})")
    else:
        lines.append("Нет 👌")

    exams = []
    for e in data["exams"]:
        due = datetime.fromisoformat(e["date"])
        if due >= now:
            exams.append((due.date(), f"{escape(e['title'])} — {escape(e['subject'])}"))
    for p in data["periods"]:
        start = date.fromisoformat(p["start"])
        if p["type"] == "session" and start >= today:
            end = date.fromisoformat(p["end"])
            exams.append((start, f"Сессия ({start:%d.%m}–{end:%d.%m})"))
    if exams:
        lines += ["", "🎓 <b>Экзамены</b>"]
        for d, title in sorted(exams)[:4]:
            lines.append(f"• {title} — {until(d, today)}")
    return "\n".join(lines)


STATE_FILE = "sent.json"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"summary": "", "reminders": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def human_delta(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} ч {m} мин"
    return f"{h} ч" if h else f"{m} мин"


def due_reminders(data, now: datetime, state):
    """Пары, для которых момент «за 2 часа» уже наступил, а пара ещё не началась.
    Так напоминание придёт, даже если GitHub пропустил нужный запуск."""
    out = []
    today = now.date()
    for c in classes_on(data, today):
        h, m = map(int, c["start"].split(":"))
        start = datetime(today.year, today.month, today.day, h, m, tzinfo=TZ)
        key = f"{today.isoformat()}-{c['start']}"
        if start - REMIND_BEFORE <= now < start and key not in state["reminders"]:
            left = int((start - now).total_seconds() // 60)
            out.append((key, f"🔔 <b>Через {human_delta(left)} пара</b>\n{class_line(c)}\n"
                             f"👤 {escape(c.get('teacher', ''))}"))
    return out


def main():
    with open("data.json", encoding="utf-8") as f:
        data = json.load(f)
    now = datetime.now(TZ)
    mode = os.environ.get("MODE", "auto")

    if mode == "test":
        send("✅ <b>Бот работает!</b>\nСводка будет приходить в 22:00, напоминания — за 2 часа до пар.\n\n"
             + build_summary(data, now))
        return
    if mode == "summary":
        send(build_summary(data, now))
        return

    # auto: отправляем всё, что «должно было» уйти и ещё не ушло
    state = load_state()
    changed = False
    today = now.date().isoformat()
    if (now.hour, now.minute) >= SUMMARY_TIME and state.get("summary") != today:
        send(build_summary(data, now))
        state["summary"] = today
        changed = True
    for key, msg in due_reminders(data, now, state):
        send(msg)
        state["reminders"].append(key)
        changed = True
    # старые отметки о напоминаниях не нужны
    state["reminders"] = [k for k in state["reminders"] if k[:10] >= today]
    if changed:
        save_state(state)
    print(f"{now:%H:%M}: проверка завершена, отправлено: {'да' if changed else 'нет'}")


if __name__ == "__main__":
    main()
