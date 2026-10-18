"""
Готовит ответы на команды бота (/today, /tomorrow, /week, /deadlines, /exams)
на несколько дней вперёд. Make по команде берёт готовый текст для текущей даты.

Формат: {"help": "...", "d": {"today": {"d20261018": "..."}, "tomorrow": {...}, ...}}
"""

import hashlib
import json
from datetime import date, datetime, timedelta
from html import escape

from notify import (TZ, WEEKDAYS, build_summary, class_line, classes_on,
                    period_on, until)

DAYS_AHEAD = 9  # ответы на 9 дней: хватает, даже если синхронизация пару дней не работает

HELP = (
    "🤖 <b>Команды Напоминалки</b>\n\n"
    "/today — пары и дедлайны на сегодня\n"
    "/tomorrow — сводка на завтра\n"
    "/week — расписание на 7 дней\n"
    "/deadlines — все предстоящие дедлайны\n"
    "/exams — экзамены и сессии\n"
    "/help — этот список\n\n"
    "Сводка приходит сама в 22:00, напоминания — за 2 часа до пар."
)


def day_classes_block(data, day: date) -> list:
    holiday = period_on(data, day, "holiday")
    if holiday:
        return [f"Выходной: {escape(holiday['title'])} 🎉"]
    classes = classes_on(data, day)
    return [class_line(c) for c in classes] or ["Пар нет 🎉"]


def deadline_items(data, start: datetime, end: datetime = None):
    items = []
    for d in data["deadlines"]:
        due = datetime.fromisoformat(d["date"])
        if due >= start and (end is None or due <= end):
            items.append((due, d))
    return sorted(items, key=lambda x: x[0])


def deadline_line(due, d, today: date) -> str:
    return (f"• {escape(d['title'])} — {escape(d['subject'])}, "
            f"до {due:%d.%m %H:%M} ({until(due.date(), today)})")


def render_today(data, day: date) -> str:
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    lines = [f"📅 <b>Сегодня, {WEEKDAYS[day.weekday()]} {day:%d.%m}</b>", "", "📚 <b>Пары</b>"]
    lines += day_classes_block(data, day)
    soon = deadline_items(data, start, start + timedelta(days=2))
    lines += ["", "⏰ <b>Дедлайны сегодня и завтра</b>"]
    lines += [deadline_line(due, d, day) for due, d in soon] or ["Нет 👌"]
    return "\n".join(lines)


def render_week(data, day: date) -> str:
    lines = [f"🗓 <b>Неделя {day:%d.%m}–{day + timedelta(days=6):%d.%m}</b>"]
    for i in range(7):
        d = day + timedelta(days=i)
        lines += ["", f"<b>{WEEKDAYS[d.weekday()].capitalize()} {d:%d.%m}</b>"]
        lines += day_classes_block(data, d)
    return "\n".join(lines)


def render_deadlines(data, day: date) -> str:
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    items = deadline_items(data, start)
    lines = ["⏰ <b>Все предстоящие дедлайны</b>", ""]
    lines += [deadline_line(due, d, day) for due, d in items] or ["Дедлайнов нет 👌"]
    return "\n".join(lines)


def render_exams(data, day: date) -> str:
    exams = []
    for e in data["exams"]:
        d = datetime.fromisoformat(e["date"]).date()
        if d >= day:
            exams.append((d, f"{escape(e['title'])} — {escape(e['subject'])} ({d:%d.%m})"))
    for p in data["periods"]:
        start, end = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
        if p["type"] == "session" and end >= day:
            exams.append((start, f"Сессия {start:%d.%m}–{end:%d.%m}"))
    lines = ["🎓 <b>Экзамены и сессии</b>", ""]
    for d, title in sorted(exams):
        when = "идёт сейчас" if d <= day else until(d, day)
        lines.append(f"• {title} — {when}")
    if len(lines) == 2:
        lines.append("Ничего не запланировано")
    return "\n".join(lines)


def build_bot_payload(data, now: datetime) -> str:
    out = {"help": HELP, "d": {k: {} for k in ("today", "tomorrow", "week", "deadlines", "exams")}}
    for i in range(DAYS_AHEAD):
        day = now.date() + timedelta(days=i)
        key = f"d{day:%Y%m%d}"
        midnight = datetime(day.year, day.month, day.day, tzinfo=TZ)
        out["d"]["today"][key] = render_today(data, day)
        out["d"]["tomorrow"][key] = build_summary(data, midnight)
        out["d"]["week"][key] = render_week(data, day)
        out["d"]["deadlines"][key] = render_deadlines(data, day)
        out["d"]["exams"][key] = render_exams(data, day)
    return json.dumps(out, ensure_ascii=False)


def payload_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
