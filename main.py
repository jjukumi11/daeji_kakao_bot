# main.py
import os
import re
import sqlite3
import datetime as dt
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, Header
import uvicorn

# ====== 환경설정 ======
PORT = int(os.environ.get("PORT", 8000))
DB_PATH = "users.db"

# 컴시간알리미 (pycomcigan)
try:
    from pycomcigan import TimeTable
except Exception:
    from pycomcigan import Timetable as TimeTable

app = FastAPI()

# ====== DB ======
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        kakao_id TEXT PRIMARY KEY,
        grade INTEGER,
        class INTEGER
    )
    """)
    conn.commit()
    conn.close()

def get_user(kakao_id: str) -> Optional[Dict]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT grade, class FROM users WHERE kakao_id=?", (kakao_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"grade": row[0], "class": row[1]}
    return None

def set_user(kakao_id: str, grade: int, clas: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO users (kakao_id, grade, class) VALUES (?, ?, ?)",
        (kakao_id, grade, clas),
    )
    conn.commit()
    conn.close()

init_db()

# ====== Kakao 응답 유틸 ======
def kakao_simple_text(text: str, quick_replies: Optional[List[Dict]] = None) -> Dict:
    payload = {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]},
    }
    if quick_replies:
        payload["template"]["quickReplies"] = quick_replies
    return payload

def qr_default() -> List[Dict]:
    return [
        {"action": "message", "label": "오늘 시간표", "messageText": "오늘 시간표"},
        {"action": "message", "label": "내일 시간표", "messageText": "내일 시간표"},
        {"action": "message", "label": "오늘 급식", "messageText": "오늘 급식"},
        {"action": "message", "label": "이번 주 학사일정", "messageText": "이번 주 학사일정"},
        {"action": "message", "label": "학년/반 변경", "messageText": "학년변경 2 8"},
    ]

# ====== 날짜 파싱 ======
def parse_korean_date(text: str, base: Optional[dt.date] = None) -> Optional[dt.date]:
    if base is None:
        kst_now = dt.datetime.utcnow() + dt.timedelta(hours=9)
        base = kst_now.date()

    t = text.strip()
    if "오늘" in t:
        return base
    if "내일" in t:
        return base + dt.timedelta(days=1)

    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", t)
    if m:
        try:
            return dt.date(base.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})\b", t)
    if m:
        try:
            return dt.date(base.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    m = re.search(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b", t)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None

# ====== 시간표 (컴시간알리미) ======
_COMCI_SCHOOL_NAME = "대지고등학교"

def fetch_timetable_text(grade: int, clas: int, target_date: dt.date) -> str:
    weekday = target_date.weekday()
    if weekday >= 5:
        return "주말에는 시간표가 없습니다."

    kst_now = dt.datetime.utcnow() + dt.timedelta(hours=9)
    today = kst_now.date()
    delta_weeks = ((target_date - today).days) // 7
    week_num = max(0, min(1, delta_weeks))

    try:
        tt = TimeTable(_COMCI_SCHOOL_NAME, week_num=week_num)
        weekday_map = {
            0: getattr(tt, "MONDAY", 0),
            1: getattr(tt, "TUESDAY", 1),
            2: getattr(tt, "WEDNESDAY", 2),
            3: getattr(tt, "THURSDAY", 3),
            4: getattr(tt, "FRIDAY", 4),
        }
        day_idx = weekday_map[weekday]

        g, c = int(grade), int(clas)
        try:
            day_list = tt.timetable[g][c][day_idx]
        except Exception:
            day_list = tt.timetable[g - 1][c - 1][day_idx]

        if not day_list:
            return "해당 학년/반 시간표가 없습니다."

        lines = []
        for i, subj in enumerate(day_list, start=1):
            s = str(subj).strip()
            if not s or s in ("None", "-", ""):
                continue  # 빈 교시는 건너뜀
            s = re.sub(r"^\d+\s*교시[:\s-]*", "", s)  # 접두어 제거
            lines.append(f"{i}교시: {s}")
        if not lines:
            return "해당 날짜에 수업이 없습니다."
        return "\n".join(lines)
    except Exception as e:
        return f"시간표 불러오기 실패: {e}"

# ====== 급식 ======
_KC_SCHOOL_CODE = "B000012547"

def fetch_meal_text(target_date: dt.date) -> str:
    yyyymm = target_date.strftime("%Y%m")
    url = f"https://school.koreacharts.com/school/meals/{_KC_SCHOOL_CODE}/{yyyymm}"

    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        table = soup.find("table")
        if not table:
            return "급식 정보를 찾을 수 없습니다."

        rows = table.find_all("tr")
        data = {}
        for tr in rows[1:]:
            tds = [td.get_text(" ", strip=True) for td in tr.find_all(["td","th"])]
            if len(tds) < 3:
                continue
            date_txt, _, menu_txt = tds[0], tds[1], tds[2]
            d = None
            m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", date_txt)
            if m:
                y, mth, dday = int(m.group(1)), int(m.group(2)), int(m.group(3))
                d = dt.date(y, mth, dday)
            else:
                m = re.search(r"(\d{1,2})[./-](\d{1,2})", date_txt)
                if m:
                    y = target_date.year
                    mth, dday = int(m.group(1)), int(m.group(2))
                    d = dt.date(y, mth, dday)
            if d:
                data[d] = menu_txt.replace("\\n", "\n")
        menu = data.get(target_date)
        if not menu:
            return "해당 날짜의 급식 정보가 없습니다."
        lines = [line.strip(" ・-·•") for line in re.split(r"[\n;]", menu) if line.strip()]
        return "\n".join(lines)
    except Exception as e:
        return f"급식 불러오기 실패: {e}"

# ====== 학사일정 ======
_SI_SCHOOL_IDF = "3515b280-22fd-4371-b105-999760a53e44"
_SI_BASE = "https://www.schoolinfo.go.kr/ei/ss/Pneiss_b01_s0.do"

def _get_schoolinfo_month_html(target_date: dt.date) -> str:
    params = {"SHL_IDF_CD": _SI_SCHOOL_IDF}
    r = requests.get(_SI_BASE, params=params, timeout=10)
    r.raise_for_status()
    return r.text

def fetch_calendar_items(target_date_from: dt.date, target_date_to: dt.date) -> List[str]:
    try:
        html = _get_schoolinfo_month_html(target_date_from)
        text = BeautifulSoup(html, "lxml").get_text("\n", strip=True)
        pattern = re.compile(r"(\d{2})\.\s*(\d{2})\s*[월화수목금토일]\s*-\s*([^\n\r]+)")
        items = []
        for m in pattern.finditer(text):
            mm, dd, title = int(m.group(1)), int(m.group(2)), m.group(3).strip()
            try:
                d = dt.date(target_date_from.year, mm, dd)
            except ValueError:
                continue
            if target_date_from <= d <= target_date_to:
                items.append(f"{d.strftime('%m/%d(%a)')} - {title}")
        return items
    except Exception as e:
        return [f"학사일정 불러오기 실패: {e}"]

def format_week_range(day: dt.date) -> (dt.date, dt.date):
    start = day - dt.timedelta(days=day.weekday())
    end = start + dt.timedelta(days=6)
    return start, end

# ====== 웹훅 ======
@app.post("/webhook")
async def webhook(request: Request, x_kakao_signature: str = Header(None)):
    body = await request.json()
    print("Received:", body)

    user_id = None
    text = ""
    try:
        user_id = body.get("userRequest", {}).get("user", {}).get("id")
        text = body.get("userRequest", {}).get("utterance", "").strip()
    except Exception:
        return kakao_simple_text("요청 파싱 실패", qr_default())

    if not user_id:
        return kakao_simple_text("사용자 ID를 확인할 수 없습니다.", qr_default())

    user = get_user(user_id)
    t = text.replace("학년반", "학년/반")

    # 학년/반 설정
    if t.startswith("학년변경") or t.startswith("학년/반"):
        parts = t.split()
        if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            g, c = int(parts[1]), int(parts[2])
            set_user(user_id, g, c)
            return kakao_simple_text(f"학년/반을 {g}학년 {c}반으로 설정했습니다.\n원하시는 기능을 선택하세요.", qr_default())
        else:
            return kakao_simple_text("변경할 학년과 반을 입력해주세요. 예: `학년변경 2 8`", qr_default())

    # 미등록 시 등록
    if (not user) and re.fullmatch(r"\s*\d+\s+\d+\s*", t):
        g, c = map(int, t.split())
        set_user(user_id, g, c)
        return kakao_simple_text(f"등록되었습니다: {g}학년 {c}반.\n이제 '오늘 시간표', '오늘 급식', '이번 주 학사일정' 등을 물어보세요.", qr_default())

    if not user:
        return kakao_simple_text("안녕하세요! 사용하실 학년과 반을 입력해주세요. 예: `2 8`", qr_default())

    # 기능 분기
    if "시간표" in t:
        target = parse_korean_date(t) or (dt.datetime.utcnow() + dt.timedelta(hours=9)).date()
        tt_text = fetch_timetable_text(user["grade"], user["class"], target)
        return kakao_simple_text(f"{user['grade']}학년 {user['class']}반 {target.strftime('%m/%d(%a)')} 시간표\n{tt_text}", qr_default())

    if "급식" in t:
        target = parse_korean_date(t) or (dt.datetime.utcnow() + dt.timedelta(hours=9)).date()
        meal_text = fetch_meal_text(target)
        return kakao_simple_text(f"{target.strftime('%Y-%m-%d')} 급식\n{meal_text}", qr_default())

    if "학사" in t or "일정" in t:
        today = (dt.datetime.utcnow() + dt.timedelta(hours=9)).date()
        if "이번 주" in t:
            start, end = format_week_range(today)
            items = fetch_calendar_items(start, end)
            if not items:
                items = ["이번 주 학사일정이 없습니다."]
            return kakao_simple_text("이번 주 학사일정\n" + "\n".join(items), qr_default())
        month_start = today.replace(day=1)
        next_month = (month_start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        month_end = next_month - dt.timedelta(days=1)
        items = fetch_calendar_items(month_start, month_end)
        if not items:
            items = ["이번 달 학사일정이 없습니다."]
        return kakao_simple_text("이번 달 학사일정\n" + "\n".join(items), qr_default())

    return kakao_simple_text(
        "무엇을 도와드릴까요?\n가능한 명령: `오늘 시간표`, `내일 시간표`, `오늘 급식`, `9월3일 급식`, `이번 주 학사일정`, `이번 달 학사일정`, `학년변경 2 8`",
        qr_default()
    )

# ====== 로컬 실행 ======
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
