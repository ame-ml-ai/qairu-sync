"""
QAIRU sync: входит в campus.qairu.edu.kz, собирает расписание, задания с дедлайнами
и академический календарь (занятия, сессия, праздники) и сохраняет всё в data.json.
Сообщения в Telegram отправляет отдельный скрипт notify.py.

Переменные окружения (в GitHub Actions задаются как Secrets):
  QAIRU_LOGIN, QAIRU_PASSWORD, MAKE_WEBHOOK_URL (для уведомления, если синхронизация упала)
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from html import escape

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = "https://campus.qairu.edu.kz"
TZ = timezone(timedelta(hours=5))  # Казахстан, UTC+5
NOW = datetime.now(TZ)

MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
EXAM_WORDS = re.compile(r"midterm|endterm|final|exam|экзамен|рубежн", re.I)


def month_num(word: str) -> int:
    word = word.lower()
    # длинные префиксы проверяем первыми, чтобы «март» не путался с «ма(й/я)»
    for prefix in sorted(MONTHS, key=len, reverse=True):
        if word.startswith(prefix):
            return MONTHS[prefix]
    raise ValueError(f"Неизвестный месяц: {word}")


def academic_year_start() -> int:
    """Учебный год начинается в сентябре."""
    return NOW.year if NOW.month >= 9 else NOW.year - 1


def make_date(day: int, month: int, hh: int = 0, mm: int = 0) -> datetime:
    year = academic_year_start() if month >= 9 else academic_year_start() + 1
    return datetime(year, month, day, hh, mm, tzinfo=TZ)


def goto(page, path: str, tries: int = 4):
    """Сайт иногда отвечает 503, поэтому повторяем с паузой."""
    for attempt in range(1, tries + 1):
        resp = page.goto(BASE + path, wait_until="networkidle", timeout=60_000)
        if resp and resp.status < 500:
            return
        print(f"{path}: статус {resp.status if resp else '?'}, попытка {attempt}/{tries}")
        time.sleep(10 * attempt)
    raise RuntimeError(f"Не удалось открыть {path}")


def login(page):
    goto(page, "/ru/login")
    page.fill('input[name="username"]', os.environ["QAIRU_LOGIN"])
    page.fill('input[name="password"]', os.environ["QAIRU_PASSWORD"])
    page.click('button[type="submit"]')
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=30_000)
    except PWTimeout:
        raise RuntimeError("Вход не удался: проверь QAIRU_LOGIN / QAIRU_PASSWORD")
    print("Вход выполнен")


# ---------- Расписание ----------

LEAVES_JS = """
cell => [...cell.querySelectorAll('*')]
  .filter(e => e.children.length === 0 && e.innerText && e.innerText.trim())
  .map(e => e.innerText.trim())
"""


def parse_class(leaves):
    if not leaves:
        return None
    subject = leaves[0]
    lesson_type = next((l for l in leaves if re.match(r"(Лекция|Практика|Лаборатор|Семинар)", l)), "")
    room_idx = next((i for i, l in enumerate(leaves) if l.startswith("ауд.") or l == "Онлайн"), None)
    room = leaves[room_idx].replace("ауд.", "").strip() if room_idx is not None else ""
    teacher = leaves[room_idx + 1] if room_idx is not None and room_idx + 1 < len(leaves) else ""
    return {"subject": subject, "lesson_type": lesson_type, "room": room, "teacher": teacher}


def scrape_schedule(page):
    goto(page, "/ru/schedule")
    page.wait_for_selector('main [role="row"]', timeout=30_000)
    slots = []  # (weekday, start, end, info)
    for row in page.locator('main [role="row"]').all():
        header = row.locator('[role="rowheader"]')
        if header.count() == 0:
            continue
        times = re.findall(r"\d{2}:\d{2}", header.inner_text())
        if len(times) < 2:
            continue
        cells = row.locator('[role="cell"], [role="gridcell"]').all()
        for weekday, cell in enumerate(cells, start=1):
            info = parse_class(cell.evaluate(LEAVES_JS))
            if info:
                slots.append((weekday, times[0], times[1], info))

    # Склеиваем соседние пары одного предмета в один блок
    slots.sort(key=lambda s: (s[0], s[1]))
    blocks = []
    for wd, start, end, info in slots:
        prev = blocks[-1] if blocks else None
        if (prev and prev["weekday"] == wd and prev["subject"] == info["subject"]
                and prev["lesson_type"] == info["lesson_type"]
                and minutes(start) - minutes(prev["end"]) <= 30):
            prev["end"] = end
        else:
            blocks.append({"kind": "class", "weekday": wd, "start": start, "end": end, **info})

    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    counters = {}
    for b in blocks:
        d = days[b["weekday"] - 1]
        counters[d] = counters.get(d, 0) + 1
        b["key"] = f"class-{d}-{counters[d]}"
    print(f"Занятий (блоков): {len(blocks)}")
    return blocks


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# ---------- Задания ----------

DONE_STATUSES = {"Оценено", "На проверке", "Сдано", "Проверено"}
SUBMITTED_RE = re.compile(
    r"Ваша работа.{0,40}?(на проверке|оценено|сдано|проверено)|Отправлено\s+\d{1,2}\s+[а-яё]+", re.I | re.S)
DUE_RE = re.compile(r"до\s+(\d{1,2})\s+([а-яё]+)(?:,\s*(\d{1,2}):(\d{2}))?", re.I)


def scrape_assignments(page):
    goto(page, "/ru/assignments")
    page.wait_for_selector('main a[href*="/assignments/"]', timeout=30_000)
    cards = []
    for a in page.locator('main a[href*="/assignments/"]').all():
        # статус («На проверке», «Оценено»…) лежит рядом со ссылкой, поэтому берём текст родителя
        card_text = a.evaluate("el => el.parentElement.innerText")
        lines = [l.strip() for l in card_text.split("\n") if l.strip()]
        href = a.get_attribute("href")
        if lines and href:
            cards.append({"href": href, "lines": lines})

    records = []
    for c in cards:
        title = c["lines"][0]
        course = c["lines"][1] if len(c["lines"]) > 1 else ""
        status = c["lines"][2] if len(c["lines"]) > 2 else ""
        if status in DONE_STATUSES:
            continue  # работа уже сдана, напоминать не нужно
        goto(page, c["href"])
        detail = page.locator("main").inner_text()
        if SUBMITTED_RE.search(detail):
            continue  # на странице задания видно, что работа сдана
        m = DUE_RE.search(detail)
        if not m:
            continue
        day, mon, hh, mm = m.groups()
        due = make_date(int(day), month_num(mon), int(hh or 23), int(mm or 59))
        if due < NOW - timedelta(days=1):
            continue  # дедлайн уже прошёл
        records.append({
            "key": "a-" + c["href"].rstrip("/").split("/")[-1][:8],
            "kind": "exam" if EXAM_WORDS.search(title) else "deadline",
            "title": title,
            "subject": course,
            "status": status,
            "date": due.isoformat(),
        })
    print(f"Актуальных заданий с дедлайном: {len(records)}")
    return records


# ---------- Академический календарь ----------

# «7 сентября – 14 ноября», «16–28 ноября», «16 декабря», «30 ноября – 12 декабря»
SP = r"[ \u00a0]"  # только пробелы, без переводов строки
RANGE = r"(\d{1,2})(?:" + SP + r"+([а-яё]+))?(?:" + SP + r"*[–-]" + SP + r"*(\d{1,2}))?" + SP + r"+([а-яё]+)"
LABELLED_RE = re.compile(r"(Занятия|Сессия)\s*\n\s*" + RANGE, re.I)
HOLIDAY_RE = re.compile(RANGE + r"\s*\n\s*([^\n]+)", re.I)


def to_range(d1, mon1, d2, mon2):
    start = make_date(int(d1), month_num(mon1 or mon2))
    end = make_date(int(d2 or d1), month_num(mon2))
    return start.date().isoformat(), end.date().isoformat()


def scrape_calendar(page):
    goto(page, "/ru/academic-calendar")
    text = page.locator("main").inner_text()
    periods = []
    for m in LABELLED_RE.finditer(text):
        label, *rng = m.groups()
        start, end = to_range(*rng)
        periods.append({"type": "classes" if label.lower() == "занятия" else "session",
                        "title": label.capitalize(), "start": start, "end": end})
    # Праздники: блоки после «ПРАЗДНИКИ И КАНИКУЛЫ» до следующего триместра
    for block in re.split(r"ПРАЗДНИКИ И КАНИКУЛЫ", text, flags=re.I)[1:]:
        block = re.split(r"\n\d\s*триместр", block)[0]
        for m in HOLIDAY_RE.finditer(block):
            *rng, title = m.groups()
            start, end = to_range(*rng)
            periods.append({"type": "holiday", "title": title.strip(), "start": start, "end": end})
    print(f"Периодов в календаре: {len(periods)}")
    return periods


# ---------- Уведомления кабинета ----------

def fetch_notifications(page):
    """Колокольчик в кабинете: JSON {items: [{id, title, message, createdAt, ...}]}."""
    resp = page.request.get(BASE + "/api/notifications?lang=ru", timeout=60_000)
    if not resp.ok:
        print("Уведомления недоступны:", resp.status)
        return None
    items = resp.json().get("items", [])
    print(f"Уведомлений в кабинете: {len(items)}")
    return items


def send_new_notifications(items, last_id):
    """Отправляет уведомления новее last_id. Возвращает новый last_id."""
    if items is None:
        return last_id
    max_id = max((n["id"] for n in items), default=last_id or 0)
    if last_id is None:
        # Первый запуск: не заваливаем старыми, шлём только непрочитанные (до 5)
        fresh = [n for n in items if not n.get("read")][:5]
    else:
        fresh = [n for n in items if n["id"] > last_id]
    for n in sorted(fresh, key=lambda x: x["id"])[-10:]:
        created = datetime.fromisoformat(n["createdAt"][:19]).replace(tzinfo=timezone.utc).astimezone(TZ)
        text = (f"🔔 <b>{escape(n.get('title') or 'Уведомление')}</b>  <i>{created:%d.%m %H:%M}</i>\n"
                f"{escape((n.get('message') or '')[:800])}\n"
                f'<a href="{BASE}/ru">Открыть кабинет</a>')
        post_message(text)
    print(f"Отправлено новых уведомлений: {len(fresh)}")
    return max(max_id, last_id or 0)


def post_message(text: str):
    url = os.environ.get("MAKE_WEBHOOK_URL")
    if url:
        requests.post(url, json={"text": text}, timeout=30).raise_for_status()


def notify_failure(message: str):
    url = os.environ.get("MAKE_WEBHOOK_URL")
    if url:
        try:
            requests.post(url, json={"text": f"⚠️ <b>Синхронизация с campus.qairu не удалась</b>\n{message}"}, timeout=30)
        except Exception as e:  # noqa: BLE001
            print("Не удалось отправить уведомление об ошибке:", e)


def main():
    previous = {}
    if os.path.exists("data.json"):
        with open("data.json", encoding="utf-8") as f:
            previous = json.load(f)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(locale="ru-RU", timezone_id="Asia/Almaty")
            login(page)
            classes = scrape_schedule(page)
            tasks = scrape_assignments(page)
            periods = scrape_calendar(page)
            notifications = fetch_notifications(page)
            browser.close()
        if not classes:
            raise RuntimeError("Расписание пустое, data.json не обновлён")
    except Exception as e:
        notify_failure(str(e)[:300])
        raise

    last_id = send_new_notifications(notifications, previous.get("last_notification_id"))

    data = {
        "last_notification_id": last_id,
        "synced_at": NOW.isoformat(),
        "classes": classes,
        "deadlines": [t for t in tasks if t["kind"] == "deadline"],
        "exams": [t for t in tasks if t["kind"] == "exam"],
        "periods": periods,
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"data.json сохранён: {len(classes)} занятий, {len(tasks)} заданий, {len(periods)} периодов")


if __name__ == "__main__":
    main()
