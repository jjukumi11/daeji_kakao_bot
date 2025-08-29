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
# pip install pycomcigan
try:
    from pycomcigan import TimeTable  # 최신 버전 기준 이름 (1.3+)
except Exception:  # 구버전 호환
    from pycomcigan import Timetable as TimeTable  # 1.0.x 호환

app = FastAPI()

# ====== DB ======
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            kakao_id TEXT PRIMARY KEY,
            grade INTEGER,
            class INTEGER
        )
        """
    )
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
    """
    '오늘', '내일', '9월3일', '09/03', '2025-09-03' 등에서 날짜 추출
    """
    if base is None:
        base = dt.date.today()

    t = text.strip()

    if "오늘" in t:
        return base
    if "내일" in t:
        return base + dt.timedelta(days=1)

    # 9월3일 / 09월 03일
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = base.year
        # 연도 넘어가는 케이스 간단 처리
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None

    # 09/03 혹은 9/3
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})\b", t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = base.year
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None

    # 2025-09-03
    m = re.search(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b", t)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None

    return None

# ====== 시간표 (컴시간알리미) ======
_COMCI_SCHOOL_NAME = "대지고등학교"

def fetch_timetable_text(grade: int, clas: int, target_date: dt.date) -> str:
    """
    target_date 기준 요일 시간표를 '1교시: 과목' 형태로 반환.
    월~금만 처리.
    """
    weekday = target_date.weekday()  # 0=Mon ... 6=Sun
    if weekday >= 5:
        return "주말에는 시간표가 없습니다."

    # week_num: 0=이번주, 1=다음주 ... (목~금에 '내일'이 주를 넘는 상황 보정)
    today = dt.date.today()
    delta_weeks = ((target_date - today).days) // 7
    week_num = max(0, min(1, delta_weeks))  # 간단 보정

    try:
        tt = TimeTable(_COMCI_SCHOOL_NAME, week_num=0 + week_num)
        # 라이브러리 요일 상수 대응
        weekday_map = {
            0: getattr(tt, "MONDAY", 0),
            1: getattr(tt, "TUESDAY", 1),
            2: getattr(tt, "WEDNESDAY", 2),
            3: getattr(tt, "THURSDAY", 3),
            4: getattr(tt, "FRIDAY", 4),
        }
        day_idx = weekday_map[weekday]

        # timetable[grade][class][day] -> ['국어','수학',...]
        # 라이브러리 구현 버전에 따라 1-index/0-index 차이가 있어 안전 처리
        g = int(grade)
        c = int(clas)
        day_list = None
        try:
            day_list = tt.timetable[g][c][day_idx]
        except Exception:
            # 혹시 0-index라면:
            day_list = tt.timetable[g - 1][c - 1][day_idx]

        if not day_list:
            return "해당 학년/반 시간표가 없습니다."

        lines = []
        for i, subj in enumerate(day_list, start=1):
            s = str(subj).strip()
            if not s or s == "None":
                s = "-"
            lines.append(f"{i}교시: {s}")
        return "\n".join(lines)
    except Exception as e:
        return f"시간표 불러오기 실패: {e}"

# ====== 급식 (school.koreacharts.com) ======
# 대지고등학교 학교코드: B000012547
# 예: https://school.koreacharts.com/school/meals/B000012547/202509
_KC_SCHOOL_CODE = "B000012547"

def fetch_meal_text(target_date: dt.date) -> str:
    yyyymm = target_date.strftime("%Y%m")
    url = f"https://school.koreacharts.com/school/meals/{_KC_SCHOOL_CODE}/{yyyymm}"

    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        # 표에서 '일자' / '급식 메뉴' 형태 파싱
        table = soup.find("table")
        if not table:
            return "급식 정보를 찾을 수 없습니다."

        rows = table.find_all("tr")
        # 헤더 인식
        # 보통: 일자 | 요일 | 급식 메뉴 | 알레르기
        data = {}
        for tr in rows[1:]:
            tds = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if len(tds) < 3:
                continue
            date_txt, _, menu_txt = tds[0], tds[1], tds[2]

            # 날짜 정규화 (e.g., 2025-09-03 or 09-03 등)
            # 페이지가 'YYYY-MM-DD' 혹은 '09-03' 등으로 올 수 있어 폭넓게 매칭
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

        # 메뉴 줄바꿈 정리
        lines = [line.strip(" ・-·•") for line in re.split(r"[\n;]", menu) if line.strip()]
        return "\n".join(lines)
    except Exception as e:
        return f"급식 불러오기 실패: {e}"

# ====== 학사일정 (학교알리미) ======
# 대지고등학교 학사일정: https://www.schoolinfo.go.kr/ei/ss/Pneiss_b01_s0.do?SHL_IDF_CD=3515b280-22fd-4371-b105-999760a53e44
_SI_SCHOOL_IDF = "3515b280-22fd-4371-b105-999760a53e44"
_SI_BASE = "https://www.schoolinfo.go.kr/ei/ss/Pneiss_b01_s0.do"

def _get_schoolinfo_month_html(target_date: dt.date) -> str:
    params = {"SHL_IDF_CD": _SI_SCHOOL_IDF}
    # 페이지가 월 이동을 쿼리로 제공하지 않더라도, 기본은 '현재/다음' 중심으로 출력됨.
    # 월 필터가 없다면 텍스트 파싱 후 월/일 매칭으로 걸러낸다.
    r = requests.get(_SI_BASE, params=params, timeout=10)
    r.raise_for_status()
    return r.text

def fetch_calendar_items(target_date_from: dt.date, target_date_to: dt.date) -> List[str]:
    """
    학교알리미 페이지 본문 텍스트에서 'MM. DD 요일 - 행사명' 패턴을 추출해
    지정 기간에 해당하는 항목만 반환.
    """
    try:
        html = _get_schoolinfo_month_html(target_date_from)
        text = BeautifulSoup(html, "lxml").get_text("\n", strip=True)

        # 예: "09. 03 화 - 전국연합학력평가"
        pattern = re.compile(r"(\d{2})\.\s*(\d{2})\s*[월화수목금토일]\s*-\s*([^\n\r]+)")
        items = []
        for m in pattern.finditer(text):
            mm = int(m.group(1))
            dd = int(m.group(2))
            title = m.group(3).strip()
            year = target_date_from.year
            try:
                d = dt.date(year, mm, dd)
            except ValueError:
                continue
            if target_date_from <= d <= target_date_to:
                items.append(f"{d.strftime('%m/%d(%a)')} - {title}")

        return items
    except Exception as e:
        return [f"학사일정 불러오기 실패: {e}"]

def format_week_range(day: dt.date) -> (dt.date, dt.date):
    start = day - dt.timedelta(days=day.weekday())  # 월
    end = start + dt.timedelta(days=6)  # 일
    return start, end

# ====== 웹훅 ======
@app.post("/webhook")
async def webhook(request: Request, x_kakao_signature: str = Header(None)):
    body = await request.json()
    print("Received:", body)

    # 카카오 사용자 ID & 발화
    user_id = None
    text = ""
    try:
        user_id = (
            body.get("userRequest", {})
            .get("user", {})
            .get("id")
            or body.get("userRequest", {})
            .get("user", {})
            .get("userId")
        )
        text = body.get("userRequest", {}).get("utterance", "").strip()
    except Exception:
        return kakao_simple_text("요청 파싱 실패", qr_default())

    if not user_id:
        return kakao_simple_text("사용자 ID를 확인할 수 없습니다.", qr_default())

    # 사용자 등록 여부
    user = get_user(user_id)

    # 학년/반 입력 흐름
    # 예: "학년/반 2 8", "학년변경 1 1", 혹은 그냥 "2 8"
    # 먼저 학년/반 설정/변경 명령 처리
    t = text.replace("학년반", "학년/반")
    if t.startswith("학년변경") or t.startswith("학년/반"):
        parts = t.split()
        if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            g, c = int(parts[1]), int(parts[2])
            set_user(user_id, g, c)
            return kakao_simple_text(
                f"학년/반을 {g}학년 {c}반으로 설정했습니다.\n원하시는 기능을 선택하세요.",
                qr_default(),
            )
        else:
            return kakao_simple_text(
                "변경할 학년과 반을 입력해주세요. 예: `학년변경 2 8`",
                [
                    {"action": "message", "label": "2학년 8반", "messageText": "학년변경 2 8"},
                    {"action": "message", "label": "1학년 1반", "messageText": "학년변경 1 1"},
                ],
            )

    # 아직 미등록인 경우, 두 숫자만 보내면 등록 처리 (예: "2 8")
    if (not user) and re.fullmatch(r"\s*\d+\s+\d+\s*", t):
        g, c = map(int, t.split())
        set_user(user_id, g, c)
        return kakao_simple_text(
            f"등록되었습니다: {g}학년 {c}반.\n이제 '오늘 시간표', '오늘 급식', '이번 주 학사일정' 등을 물어보세요.",
            qr_default(),
        )

    # 미등록 상태라면 등록 유도
    if not user:
        return kakao_simple_text(
            "안녕하세요! 사용하실 학년과 반을 입력해주세요. 예: `2 8`",
            [
                {"action": "message", "label": "2학년 8반", "messageText": "2 8"},
                {"action": "message", "label": "1학년 1반", "messageText": "1 1"},
                {"action": "message", "label": "학년/반 도움말", "messageText": "학년변경 2 8"},
            ],
        )

    # ----- 기능 분기 -----
    txt = t

    # 시간표
    if "시간표" in txt:
        # 기본 '오늘', '내일' 처리
        target = parse_korean_date(txt) or dt.date.today()
        tt_text = fetch_timetable_text(user["grade"], user["class"], target)
        return kakao_simple_text(
            f"{user['grade']}학년 {user['class']}반 {target.strftime('%m/%d(%a)')} 시간표\n{tt_text}",
            qr_default(),
        )

    # 급식
    if "급식" in txt:
        target = parse_korean_date(txt) or dt.date.today()
        meal_text = fetch_meal_text(target)
        return kakao_simple_text(
            f"{target.strftime('%Y-%m-%d')} 급식\n{meal_text}",
            qr_default(),
        )

    # 학사일정
    if "학사" in txt or "일정" in txt:
        today = dt.date.today()

        if "이번 주" in txt:
            start, end = format_week_range(today)
            items = fetch_calendar_items(start, end)
            if not items:
                items = ["이번 주 학사일정이 없습니다."]
            return kakao_simple_text(
                "이번 주 학사일정\n" + "\n".join(items),
                qr_default(),
            )

        # 기본: 이번 달
        month_start = today.replace(day=1)
        next_month = (month_start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        month_end = next_month - dt.timedelta(days=1)

        items = fetch_calendar_items(month_start, month_end)
        if not items:
            items = ["이번 달 학사일정이 없습니다."]
        return kakao_simple_text(
            "이번 달 학사일정\n" + "\n".join(items),
            qr_default(),
        )

    # 학년/반 숫자 재설정 시도 (예: '3 5')
    if re.fullmatch(r"\s*\d+\s+\d+\s*", txt):
        g, c = map(int, txt.split())
        set_user(user_id, g, c)
        return kakao_simple_text(f"학년/반을 {g}학년 {c}반으로 설정했습니다.", qr_default())

    # 기본 안내
    return kakao_simple_text(
        "무엇을 도와드릴까요?\n가능한 명령: `오늘 시간표`, `내일 시간표`, `오늘 급식`, `9월3일 급식`, `이번 주 학사일정`, `이번 달 학사일정`, `학년변경 2 8`",
        qr_default(),
    )

# ====== 로컬 실행 ======
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
