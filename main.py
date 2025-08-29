import os
import sqlite3
from fastapi import FastAPI, Request, Header
import uvicorn
import datetime

DB_PATH = "users.db"
app = FastAPI()

# -------------------------------
# DB 초기화
# -------------------------------
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

# -------------------------------
# 사용자 DB 조회/저장
# -------------------------------
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

# -------------------------------
# 예시 데이터 가져오기
# -------------------------------
def fetch_timetable(grade, clas):
    return f"{grade}학년 {clas}반 시간표 예시: 월: 국어, 수학 / 화: 영어, 과학"

def fetch_meal(date_str):
    return f"{date_str} 급식 예시: 떡볶이, 김밥, 샐러드"

def fetch_calendar():
    return "- 9/1 개학\n- 9/10 모의고사 (예시)"

# -------------------------------
# 카톡 JSON 생성
# -------------------------------
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

# -------------------------------
# 웹훅
# -------------------------------
@app.post("/webhook")
async def webhook(request: Request, x_kakao_signature: str = Header(None)):
    body = await request.json()
    print("Received:", body)

    try:
        user_id = body.get("userRequest", {}).get("user", {}).get("id") or body.get("userRequest", {}).get("user", {}).get("userId")
        text = body.get("userRequest", {}).get("utterance", "")
    except:
        return kakao_simple_text("요청 파싱 실패")
    if not user_id:
        return kakao_simple_text("사용자 ID를 확인할 수 없습니다.")

    user = get_user(user_id)
    if not user:
        qr = [
            {"action":"message","label":"학년/반 입력 (예: 2 8)","messageText":"학년/반 2 8"},
            {"action":"message","label":"학년변경","messageText":"학년변경"}
        ]
        return kakao_simple_text("안녕하세요! 학년과 반을 입력해주세요. 예: `2 8`", quick_replies=qr)

    txt = text.strip()
    # 학년/반 변경
    if txt.startswith("학년변경") or txt.startswith("학년/반"):
        parts = txt.split()
        if len(parts) >= 3:
            try:
                g = int(parts[1]); c = int(parts[2])
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
    if txt.startswith("학년/반") or (len(txt.split())==2 and all(s.isdigit() for s in txt.split())):
        parts = txt.split()
        if parts[0]=="학년/반":
            parts = parts[1:]
        if len(parts)==2 and parts[0].isdigit() and parts[1].isdigit():
            g = int(parts[0]); c = int(parts[1])
            set_user(user_id, g, c)
            return kakao_simple_text(f"등록되었습니다: {g}학년 {c}반. 원하시면 '시간표', '급식', '학사일정'을 물어보세요.")

    # 시간표/급식/학사일정 처리
    if "시간표" in txt:
        tt = fetch_timetable(user["grade"], user["class"])
        return kakao_simple_text(f"{user['grade']}학년 {user['class']}반의 시간표:\n{tt}")
    if "급식" in txt or "오늘 급식" in txt:
        date_str = datetime.date.today().isoformat()
        meal = fetch_meal(date_str)
        return kakao_simple_text(f"{date_str} 급식:\n{meal}")
    if "학사" in txt or "학사일정" in txt or "일정" in txt:
        cal = fetch_calendar()
        return kakao_simple_text(f"최근 학사일정:\n{cal}")

    return kakao_simple_text("무엇을 도와드릴까요?\n가능한 명령: `시간표`, `급식`, `학사일정`, `학년변경 2 8`")

# -------------------------------
# 서버 실행
# -------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
