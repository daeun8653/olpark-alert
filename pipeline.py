import csv
import glob
import json
import os
import re
import sys
import time
import warnings
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests

warnings.filterwarnings("ignore")

KST = ZoneInfo("Asia/Seoul")
LIST_URL = "http://www.kopis.or.kr/openApi/restful/pblprfr"
DETAIL_URL = "http://www.kopis.or.kr/openApi/restful/pblprfr/{}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,*/*",
}
PLACE_KEYWORDS = ["올림픽", "KSPO", "핸드볼", "우리금융아트홀", "티켓링크 라이브 아레나"]
VENUE_LEVELS = [
    ("KSPO DOME", "높음"),
    ("핸드볼", "높음"),
    ("티켓링크 라이브 아레나", "높음"),
    ("올림픽홀", "보통"),
    ("우리금융아트홀", "낮음"),
    ("뮤즈라이브", "낮음"),
]
ALERT_LEVELS = {"높음", "보통"}
LEVEL_ORDER = {"높음": 0, "보통": 1, "낮음": 2}
DAYS = ["월", "화", "수", "목", "금", "토", "일"]
BEFORE_SHOW = timedelta(hours=2)
AFTER_SHOW = timedelta(hours=1)
DEFAULT_RUNTIME = timedelta(minutes=150)
CHECK_DAYS = 120

FACILITY = "올림픽수영장"
TRANSIT_TIP = "올림픽공원역(5·9호선) 등 대중교통 이용을 권해요."

COURSES_FILE = "courses.csv"
PROFILE_FILE = "parking_profile.csv"
ALERTS_FILE = "alerts.csv"
DOCS_DIR = "docs"


# ---------- KOPIS ----------
def kopis_get(url, params, retries=3):
    for attempt in range(1, retries + 1):
        resp = requests.get(url, params=params, headers=HEADERS, timeout=60)
        if resp.status_code == 200:
            try:
                return ET.fromstring(resp.content)
            except ET.ParseError:
                print("  KOPIS 응답을 읽지 못했어요:", resp.text[:200])
                return None
        print(f"  KOPIS 요청 막힘 ({resp.status_code}), 60초 후 재시도 {attempt}/{retries}")
        time.sleep(60)
    return None


def fetch_shows(kopis_key, start):
    shows = {}
    cur = start
    end_all = start + timedelta(days=CHECK_DAYS)
    while cur <= end_all:
        win_end = cur + timedelta(days=13)
        page = 1
        while True:
            root = kopis_get(LIST_URL, {
                "service": kopis_key,
                "stdate": cur.strftime("%Y%m%d"),
                "eddate": win_end.strftime("%Y%m%d"),
                "cpage": page,
                "rows": 100,
                "signgucode": "11",
            })
            if root is None:
                return shows
            items = root.findall("db")
            for it in items:
                place = it.findtext("fcltynm") or ""
                if any(k in place for k in PLACE_KEYWORDS):
                    shows[it.findtext("mt20id")] = it.findtext("prfnm")
            if len(items) < 100:
                break
            page += 1
            time.sleep(2)
        print(f"  {cur} ~ {win_end} 확인 (올림픽공원 누적 {len(shows)}건)")
        cur = win_end + timedelta(days=1)
        time.sleep(2)
    return shows


def fetch_details(kopis_key, shows):
    details = []
    for show_id in shows:
        root = kopis_get(DETAIL_URL.format(show_id), {"service": kopis_key})
        db = root.find("db") if root is not None else None
        if db is None:
            continue
        details.append({
            "id": show_id,
            "name": db.findtext("prfnm") or "",
            "from": db.findtext("prfpdfrom") or "",
            "to": db.findtext("prfpdto") or "",
            "place": db.findtext("fcltynm") or "",
            "schedule": db.findtext("dtguidance") or "",
            "runtime": db.findtext("prfruntime") or "",
        })
        time.sleep(2)
    return details


# ---------- 데이터 읽기 ----------
def read_csv_any(path):
    for enc in ("utf-8-sig", "cp949"):
        try:
            with open(path, encoding=enc) as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path} 파일을 읽지 못했어요")


def load_profile():
    profile = {}
    if not os.path.exists(PROFILE_FILE):
        print("  parking_profile.csv가 없어서 혼잡 배율은 빼고 진행해요")
        return profile
    for r in read_csv_any(PROFILE_FILE):
        profile[(r["day_type"], int(r["hour"]))] = float(r["ratio"])
    return profile


def to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def load_courses():
    parsed = []
    for row in read_csv_any(COURSES_FILE):
        times = re.findall(r"\d{1,2}:\d{2}", row.get("시작종료시간") or row.get("수강시간") or "")
        days = re.findall(r"[월화수목금토일]", row.get("요일") or row.get("수강요일") or "")
        if len(times) < 2 or not days:
            continue
        try:
            capacity = int(float(row.get("정원") or 0))
        except ValueError:
            capacity = 0
        parsed.append({
            "name": (row.get("강좌명") or "").strip(),
            "start": to_minutes(times[0]),
            "end": to_minutes(times[1]),
            "days": set(days),
            "days_text": "".join(d for d in DAYS if d in days),
            "month": row.get("시작월") or row.get("수강월") or "",
            "time_text": f"{times[0]}~{times[1]}",
            "capacity": capacity,
        })
    latest = max(c["month"] for c in parsed)
    return [c for c in parsed if c["month"] == latest]


# ---------- 겹침 계산 ----------
def venue_level(place):
    for keyword, level in VENUE_LEVELS:
        if keyword in place:
            return level
    return "낮음"


def parse_runtime(s):
    hours = re.search(r"(\d+)\s*시간", s or "")
    minutes = re.search(r"(\d+)\s*분", s or "")
    total = (int(hours.group(1)) * 60 if hours else 0) + (int(minutes.group(1)) if minutes else 0)
    return timedelta(minutes=total) if total else DEFAULT_RUNTIME


def parse_schedule(text):
    result = {}
    for day_part, time_part in re.findall(r"([^()]+)\(([^)]*)\)", text or ""):
        times = re.findall(r"\d{1,2}:\d{2}", time_part)
        if not times:
            continue
        found = re.findall(r"([월화수목금토일])요일", day_part)
        if "~" in day_part and len(found) >= 2:
            days = DAYS[DAYS.index(found[0]):DAYS.index(found[-1]) + 1]
        else:
            days = found
        for d in days:
            result.setdefault(d, []).extend(times)
    return result


def compute_alerts(details, courses, start, profile):
    grouped = defaultdict(list)
    for show in details:
        level = venue_level(show["place"])
        if level not in ALERT_LEVELS:
            continue
        schedule = parse_schedule(show["schedule"])
        if not schedule:
            continue
        runtime = parse_runtime(show["runtime"])
        try:
            s_date = datetime.strptime(show["from"], "%Y.%m.%d").date()
            e_date = datetime.strptime(show["to"], "%Y.%m.%d").date()
        except ValueError:
            continue
        for offset in range(CHECK_DAYS):
            day = start + timedelta(days=offset)
            if day < s_date or day > e_date:
                continue
            weekday = DAYS[day.weekday()]
            for show_time in schedule.get(weekday, []):
                show_start = datetime.combine(day, datetime.strptime(show_time, "%H:%M").time())
                busy_from = show_start - BEFORE_SHOW
                busy_to = show_start + runtime + AFTER_SHOW
                from_m = busy_from.hour * 60 + busy_from.minute
                to_m = busy_to.hour * 60 + busy_to.minute if busy_to.date() == day else 24 * 60
                for c in courses:
                    if weekday in c["days"] and c["start"] < to_m and c["end"] > from_m:
                        grouped[(day, c["name"])].append({
                            "course": c, "show": show["name"], "place": show["place"],
                            "start": show_time, "level": level,
                            "busy": f"{busy_from:%H:%M}~{busy_to:%H:%M}",
                        })

    alerts = []
    for (day, course_name), items in grouped.items():
        top = sorted(items, key=lambda x: LEVEL_ORDER[x["level"]])[0]
        c = top["course"]
        day_type = "주말" if day.weekday() >= 5 else "평일"
        ratio = profile.get((day_type, c["start"] // 60))
        alerts.append({
            "date": day.isoformat(),
            "weekday": DAYS[day.weekday()],
            "course": course_name,
            "course_time": c["time_text"],
            "capacity": c["capacity"],
            "level": top["level"],
            "show": top["show"],
            "place": top["place"],
            "show_start": top["start"],
            "busy_window": top["busy"],
            "ratio": round(ratio, 1) if ratio else "",
            "facility": FACILITY,
            "transit": TRANSIT_TIP,
        })
    alerts.sort(key=lambda a: (a["date"], a["course_time"], a["course"]))
    return alerts


def save_outputs(alerts, courses):
    if alerts:
        with open(ALERTS_FILE, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(alerts[0].keys()))
            writer.writeheader()
            writer.writerows(alerts)
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(os.path.join(DOCS_DIR, "alerts.json"), "w", encoding="utf-8") as f:
        json.dump(alerts, f, ensure_ascii=False)
    unique = {}
    for c in courses:
        unique[c["name"]] = {
            "name": c["name"], "time": c["time_text"],
            "days": c["days_text"], "capacity": c["capacity"],
        }
    course_list = sorted(unique.values(), key=lambda c: (c["time"], c["name"]))
    with open(os.path.join(DOCS_DIR, "courses.json"), "w", encoding="utf-8") as f:
        json.dump(course_list, f, ensure_ascii=False)


# ---------- 알림 ----------
def build_message(a):
    title = f"{a['date']}({a['weekday']}) 공연 겹침 알림"
    ratio_text = f" 이 시간대 주차 차량이 평소의 약 {a['ratio']}배예요." if a.get("ratio") else ""
    body = (
        f"{a['course_time']} {a['course']} 시간에 {a['place']}에서 "
        f"{a['show_start']} '{a['show']}' 공연이 있어요.{ratio_text} "
        f"혼잡 예상 {a['busy_window']}. {a['transit']}"
    )
    return title, body


def firebase_credentials():
    from firebase_admin import credentials
    env_key = os.environ.get("FIREBASE_KEY")
    if env_key:
        return credentials.Certificate(json.loads(env_key))
    files = glob.glob("serviceAccountKey*.json*") + glob.glob("*adminsdk*.json")
    if not files:
        sys.exit("Firebase 키를 찾지 못했어요")
    return credentials.Certificate(files[0])


def send_notifications(alerts, mode, today):
    import firebase_admin
    from firebase_admin import firestore, messaging

    firebase_admin.initialize_app(firebase_credentials())
    db = firestore.client()
    today_s = today.isoformat()
    tomorrow_s = (today + timedelta(days=1)).isoformat()

    sent, removed, subs = 0, 0, 0
    for doc in db.collection("subscribers").stream():
        subs += 1
        sub = doc.to_dict()
        timing = sub.get("timing", "evening")
        courses = set(sub.get("courses", []))
        mine = [a for a in alerts if a["course"] in courses]

        if mode == "test":
            picks = [a for a in mine if a["date"] >= today_s][:1]
        elif mode == "evening":
            picks = [a for a in mine if a["date"] == tomorrow_s] if timing == "evening" else []
        elif mode == "morning":
            picks = [a for a in mine if a["date"] == today_s] if timing == "morning" else []
        else:
            picks = []

        for a in picks:
            title, body = build_message(a)
            try:
                messaging.send(messaging.Message(data={"title": title, "body": body}, token=sub.get("token")))
                sent += 1
                print(f"  보냄: {title} → {a['course']}")
            except Exception as e:
                if "NotRegistered" in str(e) or "not found" in str(e).lower():
                    doc.reference.delete()
                    removed += 1
                    break
                print(f"  전송 실패 ({doc.id}): {e}")
    print(f"구독자 {subs}명, 보낸 알림 {sent}건, 지운 무효 구독 {removed}건")


# ---------- 실행 ----------
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "evening"
    if mode == "daily":
        mode = "evening"
    today = datetime.now(KST).date()
    print(f"[{today}] 모드: {mode}")

    kopis_key = os.environ.get("KOPIS_KEY") or input("KOPIS 서비스키: ").strip()

    print("1) KOPIS 공연 목록 수집")
    shows = fetch_shows(kopis_key, today)
    print(f"   올림픽공원 공연 {len(shows)}건")

    print("2) 공연 상세 조회")
    details = fetch_details(kopis_key, shows)

    print("3) 강좌와 겹침 계산")
    courses = load_courses()
    alerts = compute_alerts(details, courses, today, load_profile())
    save_outputs(alerts, courses)
    print(f"   알림 대상 {len(alerts)}건, 웹 데이터 갱신 완료")

    if mode == "nosend":
        print("전송은 건너뛰었어요")
        return
    print("4) 알림 전송")
    send_notifications(alerts, mode, today)


if __name__ == "__main__":
    main()