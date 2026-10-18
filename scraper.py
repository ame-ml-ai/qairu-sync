"""
QAIRU sync: входит в campus.qairu.edu.kz, собирает расписание, задания с дедлайнами
и даты сессии из академического календаря, и отправляет всё одним JSON в вебхук Make.

Переменные окружения (в GitHub Actions задаются как Secrets):
  QAIRU_LOGIN, QAIRU_PASSWORD, MAKE_WEBHOOK_URL
Для локального теста без отправки:  DRY_RUN=1 python scraper.py
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

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

DUE_RE = re.compile(r"до\s+(\d{1,2})\s+([а-яё]+)(?:,\s*(\d{1,2}):(\d{2}))?", re.I)


def scrape_assignments(page):
    goto(page, "/ru/assignments")
    page.wait_for_selector('main a[href*="/assignments/"]', timeout=30_000)
    cards = []
    for a in page.locator('main a[href*="/assignments/"]').all():
        lines = [l.strip() for l in a.inner_text().split("\n") if l.strip()]
        href = a.get_attribute("href")
        if lines and href:
            cards.append({"href": href, "lines": lines})

    records = []
    for c in cards:
        title = c["lines"][0]
        course = c["lines"][1] if len(c["lines"]) > 1 else ""
        status = c["lines"][2] if len(c["lines"]) > 2 else ""
        if status == "Оценено":
            continue  # уже сдано и проверено, напоминать не нужно
        goto(page, c["href"])
        m = DUE_RE.search(page.locator("main").inner_text())
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

RANGE_RE = re.compile(
    r"Сессия\s*\n\s*(\d{1,2})(?:\s+([а-яё]+))?\s*[–-]\s*(\d{1,2})\s+([а-яё]+)", re.I)


def scrape_calendar(page):
    goto(page, "/ru/academic-calendar")
    text = page.locator("main").inner_text()
    records = []
    for i, m in enumerate(RANGE_RE.finditer(text), start=1):
        d1, mon1, d2, mon2 = m.groups()
        start = make_date(int(d1), month_num(mon1 or mon2))
        end = make_date(int(d2), month_num(mon2))
        if end < NOW:
            continue
        records.append({
            "key": f"exam-session-t{i}",
            "kind": "exam",
            "title": f"Сессия {i} триместра ({start:%d.%m}–{end:%d.%m})",
            "date": start.isoformat(),
        })
    print(f"Будущих сессий: {len(records)}")
    return records


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="ru-RU", timezone_id="Asia/Almaty")
        login(page)
        records = scrape_schedule(page) + scrape_assignments(page) + scrape_calendar(page)
        browser.close()

    if not any(r["kind"] == "class" for r in records):
        # Защита: не затираем базу пустыми данными, если что-то пошло не так
        sys.exit("Расписание пустое, отправка отменена")

    payload = {"synced_at": NOW.isoformat(), "records": records}
    if os.environ.get("DRY_RUN"):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    r = requests.post(os.environ["MAKE_WEBHOOK_URL"], json=payload, timeout=60)
    r.raise_for_status()
    print(f"Отправлено в Make: {len(records)} записей, ответ: {r.text}")


if __name__ == "__main__":
    main()
