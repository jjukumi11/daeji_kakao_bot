import os
import re
import sqlite3
import datetime as dt
from typing import Dict, List, Optional

import httpx
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

# ====== DB 초기화 ======
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            kakao_id TEXT PRIMARY KEY,
            grade INTEGER,
            class INTEGER
        )
        """)
init_db()

def get_user(kakao_id: str) -> Optional[Dict]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT grade, class FROM users WHERE kakao_id=?",
            (kakao_id,)
        ).fetchone()
        if row:
            return {"grade": row[0], "class": row[1]}
    return None

def set_user(kakao_id: str, grade: int, clas: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (kakao_id, grade, class) VALUES (?, ?, ?)",
            (kakao_id, grade, clas)
        )

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
        {"action": "message", "label": "이번 달 학사일정", "messageText": "이번 달 학사일정"},
        {"action": "message", "label": "학년/반 변경", "messageText": "학년/반 변경"},
    ]

# ====== 날짜 파싱 ======
def parse_korean_date(text: str, base: Optional[dt.date] = None) -> Optional[dt.date]:
    if base is None:
        base = (dt.datetime.utcnow() + dt.timedelta(hours=9)).date()

    t = text.strip()
    if "오늘" in t:
        return base
    if "내일" in t:
        return base + dt.timedelta(days=1)

    patterns = [
        r"(\d{1,2})\s*월\s*(\d{1,2})\s*일",
        r"\b(\d{1,2})[./-](\d{1,2})\b",
        r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b",
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            try:
                if len(m.groups()) == 2:
                    return dt.date(base.year, int(m.group(1)), int(m.group(2)))
                elif len(m.groups()) == 3:
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
        day_idx = weekday  # 월~금: 0~4
        try:
            day_list = tt.timetable[grade-1][clas-1][day_idx]
        except IndexError:
            return "해당 학년/반 시간표가 없습니다."

        if not day_list:
            return "해당 날짜에 수업이 없습니다."

        lines = []
        for i, subj in enumerate(day_list, start=1):
            s = str(subj).strip()
            if s and s not in ("None", "-", "", "()", "빈"):
                s = re.sub(r"^\d+\s*교시[:\s-]*", "", s)
                lines.append(f"{i}교시: {s}")

        return "\n".join(lines) if lines else "해당 날짜에 수업이 없습니다."

    except Exception as e:
        return f"시간표 불러오기 실패: {e}"

# ====== 급식 (코리아차트) ======
_KC_SCHOOL_CODE = "B000012547"

async def fetch_meal_text(target_date: dt.date) -> str:
    yearmonth = target_date.strftime("%Y%m")
    url = f"https://school.koreacharts.com/school/meals/{_KC_SCHOOL_CODE}/{yearmonth}.html"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        target_day = str(int(target_date.strftime("%d")))
        for row in soup.select("tr"):
            cols = row.find_all("td", class_="text-center")
            if len(cols) >= 3 and cols[0].get_text(strip=True) == target_day:
                menu_text = cols[2].get_text(" ", strip=True)
                return f"{target_date.strftime('%Y-%m-%d')} 급식\n{menu_text}"

        return f"{target_date.strftime('%Y-%m-%d')} 급식 정보가 없습니다."

    except Exception as e:
        return f"급식 불러오기 실패: {e}"

# ====== 학사일정 ======
ACADEMIC_SCHEDULE: List[Dict[str, str]] = [
    {"date": "2025-09-01", "event": "학부모 상담주간(3)"},
    {"date": "2025-09-02", "event": ""},
    {"date": "2025-09-03", "event": "전국연합(1,2)/대수능 모의평가(3)/목요일 시간표"},
    {"date": "2025-09-04", "event": ""},
    {"date": "2025-09-05", "event": "학부모 수업 공개의 날(3)"},
    {"date": "2025-09-06", "event": ""},
    {"date": "2025-09-07", "event": ""},
    {"date": "2025-09-08", "event": ""},
    {"date": "2025-09-09", "event": ""},
    {"date": "2025-09-10", "event": ""},
    {"date": "2025-09-11", "event": ""},
    {"date": "2025-09-12", "event": ""},
    {"date": "2025-09-13", "event": ""},
    {"date": "2025-09-14", "event": ""},
    {"date": "2025-09-15", "event": ""},
    {"date": "2025-09-16", "event": ""},
    {"date": "2025-09-17", "event": ""},
    {"date": "2025-09-18", "event": ""},
    {"date": "2025-09-19", "event": ""},
    {"date": "2025-09-20", "event": ""},
    {"date": "2025-09-21", "event": ""},
    {"date": "2025-09-22", "event": ""},
    {"date": "2025-09-23", "event": ""},
    {"date": "2025-09-24", "event": ""},
    {"date": "2025-09-25", "event": "1차 지필평가(3)"},
    {"date": "2025-09-26", "event": "1차 지필평가(3)"},
    {"date": "2025-09-27", "event": ""},
    {"date": "2025-09-28", "event": ""},
    {"date": "2025-09-29", "event": "1차 지필평가(3)"},
    {"date": "2025-09-30", "event": "1차 지필평가(3)"},
    {"date": "2025-10-01", "event": "1차 지필평가(3)"},
    {"date": "2025-10-02", "event": ""},
    {"date": "2025-10-03", "event": "개천절"},
    {"date": "2025-10-04", "event": ""},
    {"date": "2025-10-05", "event": ""},
    {"date": "2025-10-06", "event": "추석"},
    {"date": "2025-10-07", "event": "추석연휴"},
    {"date": "2025-10-08", "event": "대체공휴일"},
    {"date": "2025-10-09", "event": "한글날"},
    {"date": "2025-10-10", "event": "재량휴업일"},
    {"date": "2025-10-11", "event": ""},
    {"date": "2025-10-12", "event": ""},
    {"date": "2025-10-13", "event": ""},
    {"date": "2025-10-14", "event": "전국연합(1,2,3)"},
    {"date": "2025-10-15", "event": ""},
    {"date": "2025-10-16", "event": ""},
    {"date": "2025-10-17", "event": ""},
    {"date": "2025-10-18", "event": ""},
    {"date": "2025-10-19", "event": ""},
    {"date": "2025-10-20", "event": "1차 지필평가(1,2)"},
    {"date": "2025-10-21", "event": "1차 지필평가(1,2)"},
    {"date": "2025-10-22", "event": "1차 지필평가(1,2)"},
    {"date": "2025-10-23", "event": "1차 지필평가(1,2)"},
    {"date": "2025-10-24", "event": "(성적이의신청기간)"},
    {"date": "2025-10-25", "event": ""},
    {"date": "2025-10-26", "event": ""},
    {"date": "2025-10-27", "event": "(성적이의신청기간)"},
    {"date": "2025-10-28", "event": "(성적이의신청기간)/목요일 시간표"},
    {"date": "2025-10-29", "event": ""},
    {"date": "2025-10-30", "event": ""},
    {"date": "2025-10-31", "event": ""},
    {"date": "2025-11-01", "event": "♡제작자 생일♡(2학년 8반 21번 사물함에 선물 두고 가세요)"},
    {"date": "2025-11-02", "event": ""},
    {"date": "2025-11-03", "event": "학부모 상담주간(1,2)"},
    {"date": "2025-11-04", "event": ""},
    {"date": "2025-11-05", "event": ""},
    {"date": "2025-11-06", "event": ""},
    {"date": "2025-11-07", "event": "학부모 수업 공개의 날(1,2)"},
    {"date": "2025-11-08", "event": ""},
    {"date": "2025-11-09", "event": ""},
    {"date": "2025-11-10", "event": ""},
    {"date": "2025-11-11", "event": ""},
    {"date": "2025-11-12", "event": ""},
    {"date": "2025-11-13", "event": "대학수학능력시험(재량휴업일)-모르는건 3번!"},
    {"date": "2025-11-14", "event": ""},
    {"date": "2025-11-15", "event": ""},
    {"date": "2025-11-16", "event": ""},
    {"date": "2025-11-17", "event": "2차 지필평가(3)"},
    {"date": "2025-11-18", "event": "2차 지필평가(3)"},
    {"date": "2025-11-19", "event": ""},
    {"date": "2025-11-20", "event": ""},
    {"date": "2025-11-21", "event": ""},
    {"date": "2025-11-22", "event": ""},
    {"date": "2025-11-23", "event": ""},
    {"date": "2025-11-24", "event": ""},
    {"date": "2025-11-25", "event": ""},
    {"date": "2025-11-26", "event": ""},
    {"date": "2025-11-27", "event": ""},
    {"date": "2025-11-28", "event": "축제/동아리 발표회"},
    {"date": "2025-11-29", "event": ""},
    {"date": "2025-11-30", "event": ""},
    {"date": "2025-12-01", "event": ""},
    {"date": "2025-12-02", "event": ""},
    {"date": "2025-12-03", "event": ""},
    {"date": "2025-12-04", "event": ""},
    {"date": "2025-12-05", "event": ""},
    {"date": "2025-12-06", "event": ""},
    {"date": "2025-12-07", "event": ""},
    {"date": "2025-12-08", "event": ""},
    {"date": "2025-12-09", "event": ""},
    {"date": "2025-12-10", "event": ""},
    {"date": "2025-12-11", "event": ""},
    {"date": "2025-12-12", "event": ""},
    {"date": "2025-12-13", "event": ""},
    {"date": "2025-12-14", "event": ""},
    {"date": "2025-12-15", "event": ""},
    {"date": "2025-12-16", "event": ""},
    {"date": "2025-12-17", "event": ""},
    {"date": "2025-12-18", "event": "2차 지필평가(1,2)"},
    {"date": "2025-12-19", "event": "2차 지필평가(1,2)"},
    {"date": "2025-12-20", "event": ""},
    {"date": "2025-12-21", "event": ""},
    {"date": "2025-12-22", "event": "2차 지필평가(1,2)"},
    {"date": "2025-12-23", "event": "2차 지필평가(1,2)"},
    {"date": "2025-12-24", "event": "(성적이의신청기간)"},
    {"date": "2025-12-25", "event": "성탄절"},
    {"date": "2025-12-26", "event": "(성적이의신청기간)"},
    {"date": "2025-12-27", "event": ""},
    {"date": "2025-12-28", "event": ""},
    {"date": "2025-12-29", "event": "(성적이의신청기간)"},
    {"date": "2025-12-30", "event": ""},
    {"date": "2025-12-31", "event": ""},
]

def fetch_calendar_items(start: dt.date, end: dt.date) -> List[str]:
    items = []
    for row in ACADEMIC_SCHEDULE:
        try:
            d = dt.datetime.strptime(row["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if start <= d <= end:
            title = (row.get("event") or "").strip()
            items.append(f"{d.strftime('%m/%d(%a)')} - {title}" if title else f"{d.strftime('%m/%d(%a)')} - ")
    return sorted(items, key=lambda s: dt.datetime.strptime(s.split(" - ")[0], "%m/%d(%a)"))

def format_week_range(day: dt.date) -> (dt.date, dt.date):
    start = day - dt.timedelta(days=day.weekday())
    end = start + dt.timedelta(days=6)
    return start, end

# ====== 웹훅 ======
@app.post("/webhook")
async def webhook(request: Request, x_kakao_signature: str = Header(None)):
    body = await request.json()
    user_id = body.get("userRequest", {}).get("user", {}).get("id")
    text = body.get("userRequest", {}).get("utterance", "").strip()

    if not user_id:
        return kakao_simple_text("사용자 ID를 확인할 수 없습니다.", qr_default())

    # TODO: x_kakao_signature 검증 필요
    user = get_user(user_id)

    if text.startswith("학년/반 변경"):
        return kakao_simple_text("변경할 학년과 반을 입력해주세요. 예: 2 8 또는 2학년 8반", qr_default())

    m = re.match(r"(\d+)\s*학년\s*(\d+)반", text)
    if m:
        g, c = int(m.group(1)), int(m.group(2))
        set_user(user_id, g, c)
        return kakao_simple_text(f"학년/반을 {g}학년 {c}반으로 설정했습니다.", qr_default())

    if re.fullmatch(r"\d+\s+\d+", text):
        g, c = map(int, text.split())
        set_user(user_id, g, c)
        return kakao_simple_text(f"학년/반을 {g}학년 {c}반으로 설정했습니다.", qr_default())

    if not user:
        return kakao_simple_text("안녕하세요! 사용하실 학년과 반을 입력해주세요. 예: 2 8 또는 2학년 8반", qr_default())

    # 시간표
    if "시간표" in text:
        target = parse_korean_date(text) or (dt.datetime.utcnow() + dt.timedelta(hours=9)).date()
        tt_text = fetch_timetable_text(user["grade"], user["class"], target)
        return kakao_simple_text(f"{user['grade']}학년 {user['class']}반 {target.strftime('%m/%d(%a)')} 시간표\n{tt_text}", qr_default())

    # 급식
    if "급식" in text:
        target = parse_korean_date(text) or (dt.datetime.utcnow() + dt.timedelta(hours=9)).date()
        meal_text = await fetch_meal_text(target)
        return kakao_simple_text(meal_text, qr_default())

    # 학사일정
    if "학사" in text or "일정" in text:
        today = (dt.datetime.utcnow() + dt.timedelta(hours=9)).date()
        if "이번 주" in text:
            start, end = format_week_range(today)
            items = fetch_calendar_items(start, end)
            return kakao_simple_text("이번 주 학사일정\n" + ("\n".join(items) if items else "없음"), qr_default())

        # 기본: 이번 달
        month_start = today.replace(day=1)
        next_month = (month_start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        month_end = next_month - dt.timedelta(days=1)
        items = fetch_calendar_items(month_start, month_end)
        return kakao_simple_text("이번 달 학사일정\n" + ("\n".join(items) if items else "없음"), qr_default())

    return kakao_simple_text(
        "무엇을 도와드릴까요?\n가능한 명령: `오늘 시간표`, `내일 시간표`, `오늘 급식`, `9월3일 급식`, `이번 주 학사일정`, `이번 달 학사일정`, `학년/반 변경`",
        qr_default()
    )

# ====== 로컬 실행/헬스체크 ======
@app.get("/")
async def root():
    return {"status": "ok"}

@app.head("/")
async def root_head():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
