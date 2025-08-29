import os
import sqlite3
from fastapi import FastAPI, Request, Header
import uvicorn
import datetime

DB_PATH = "users.db"
app = FastAPI()

# ---------------------
# DB 초기화
# ---------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        kakao_id TEXT PRIMARY KEY,
        grade INTEGER,
        class INTEGER
    )""")
    conn.commit()
    conn.close()

init_db()

# ---------------------
# DB 조회/저장 함수
# ---------------------
def get_user(kakao_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT grade, class FROM users WHERE kakao_id=?", (kakao_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"grade": row[0], "class": row[1]}
    return None

def set_user(kakao_id, grade, clas):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO users (kakao_id, grade, class) VALUES (?, ?, ?)",
                (kakao_id, grade, clas))
    conn.commit()
    conn.close()

# ---------------------
# 데이터 가져오기 (예시)
# ---------------------
def fetch_timetable(grade, clas, date=None):
    # 실제 컴시간알리미 사이트에서 크롤링하면 이 부분 교체
    today = date or datetime.date.today().strftime("%Y-%m-%d")
    timetable = f"{today} {grade}학년 {clas}반 시간표:\n1교시: 수학\n2교시: 국어\n3교시: 영어\n4교시: 과학"
    return timetable

def fetch_meal(date=None):
    # 실제 급식 사이트 크롤링 시 교체
    date_str = date or datetime.date.today().strftime("%Y-%m-%d")
    meal = f"{date_str} 급식:\n- 기장밥\n- 소고기 뭇국\n- 감자샐러드"
    return meal

def fetch_calendar():
    # 실제 학사일정 사이트 크롤링 시 교체
    events = "- 9/1 개학\n- 9/10 모의고사 (예시)"
    return events

# ---------------------
# 카카오톡 간단 텍스트 반환
# ---------------------
def kakao_simple_text(text, quick_replies=None):
    payload = {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}]
        }
    }
    if quick_replies:
        payload["template"]["quickReplies"] = quick_replies
    return payload

# ---------------------
# Webhook
# ---------------------
@app.post("/webhook")
async def webhook(request: Request, x_kakao_signature: str = Header(None)):
    body = await request.json()
    print("Received:", body)  # Render 로그에서 확인

    # 사용자 ID 가져오기
    user_id = body.get("userRequest", {}).get("user", {}).get("id") \
              or body.get("userRequest", {}).get("user", {}).get("userId")
    text = body.get("userRequest", {}).get("utterance", "").strip()

    if not user_id:
        return kakao_simple_text("사용자 ID를 확인할 수 없습니다.")

    user = get_user(user_id)
    # 학년/반 미등록 시 등록 유도
    if not user:
        qr = [
            {"action":"message","label":"학년/반 입력 (예: 2 8)","messageText":"학년/반 2 8"},
            {"action":"message","label":"학년변경","messageText":"학년변경"}
        ]
        return kakao_simple_text("안녕하세요! 사용하실 학년과 반을 입력해주세요. 예: `2 8`", quick_replies=qr)

    # 학년/반 변경
    if text.startswith("학년변경") or text.startswith("학년/반"):
        parts = text.split()
        if len(parts) >= 3:
            try:
                g = int(parts[1])
                c = int(parts[2])
                set_user(user_id, g, c)
                return kakao_simple_text(f"학년/반을 {g}학년 {c}반으로 변경했습니다.")
            except:
                return kakao_simple_text("학년/반 포맷이 잘못되었습니다. 예: `학년변경 2 8`")
        else:
            qr = [
                {"action":"message","label":"2학년 8반","messageText":"학년/반 2 8"},
                {"action":"message","label":"1학년 1반","messageText":"학년/반 1 1"}
            ]
            return kakao_simple_text("변경할 학년과 반을 입력해주세요. 예: `학년/반 2 8`", quick_replies=qr)

    # 학년/반 등록
    if text.startswith("학년/반") or (len(text.split())==2 and all(s.isdigit() for s in text.split())):
        parts = text.split()
        if parts[0]=="학년/반":
            parts = parts[1:]
        if len(parts)==2 and parts[0].isdigit() and parts[1].isdigit():
            g = int(parts[0]); c = int(parts[1])
            set_user(user_id, g, c)
            return kakao_simple_text(f"등록되었습니다: {g}학년 {c}반. 원하시면 '시간표', '급식', '학사일정'을 물어보세요.")

    # ---------------------
    # 질문 처리
    # ---------------------
    grade, clas = user["grade"], user["class"]

    if "시간표" in text:
        # "오늘 시간표", "내일 시간표" 등 처리
        if "내일" in text:
            date = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            date = datetime.date.today().strftime("%Y-%m-%d")
        tt = fetch_timetable(grade, clas, date)
        return kakao_simple_text(tt)

    if "급식" in text:
        # "오늘 급식", "9월3일 급식" 등 처리
        date = None
        import re
        m = re.search(r'(\d{1,2})[/-](\d{1,2})', text)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            year = datetime.date.today().year
            date = datetime.date(year, month, day).strftime("%Y-%m-%d")
        meal = fetch_meal(date)
        return kakao_simple_text(밥)

    if "학사" in text or "일정" in text:
        cal = fetch_calendar()
        return kakao_simple_text(cal)

    return kakao_simple_text(
        "무엇을 도와드릴까요?\n가능한 명령: `오늘 시간표`, `내일 시간표`, `오늘 급식`, `학사일정`, `학년변경 2 8`"
    )

# ---------------------
# 실행
# ---------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
