# -*- coding: utf-8 -*-
"""
롯데홈쇼핑(LT) 편성표 수집기

라이브TV(라이브방송) / 원TV(데이터방송) 편성을 공통 스키마로 변환해 저장한다.

== 저장 구조 ==
homeshopping/
├── LT_live/{YYYY-MM}.json   라이브TV
└── LT_data/{YYYY-MM}.json   원TV(데이터방송)

== 공통 스키마 (월 파일 안에 날짜별 누적) ==
{
  "company": "LT", "broadcast": "live", "month": "2026-06",
  "days": {
    "2026-06-22": [
      {"start":"08:00","end":"09:59","brand":"미우미우","product":"하프문 숄더백",
       "price":39000,"link":"https://..."}
    ]
  }
}

== 수집 정책 ==
오늘 기준 -1일 ~ +5일(7일)을 매번 수집.
과거(오늘 이전) 날짜가 이미 기록돼 있으면 다시 안 건드리고 보존, 오늘+미래만 갱신.

== 사용법 ==
  pip install requests
  python lt_scraper.py
"""

# -*- coding: utf-8 -*-
"""
롯데홈쇼핑(LT) 편성표 수집기 (중복 제거 및 시간 충돌 방지 버전)
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from categorize import classify_batch
from clean_product import clean_product_name

KST = timezone(timedelta(hours=9))
OUTPUT_DIR = "homeshopping"
REQUEST_DELAY = 0.5
DAYS_RANGE = range(-1, 6)  # 어제 ~ +5일

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

def today_kst():
    return datetime.now(KST)

def parse_price(v):
    if v is None: return 0
    if isinstance(v, (int, float)): return int(v)
    s = str(v).replace(",", "").strip()
    return int(float(s)) if s else 0

def time_to_min(t):
    """시간 문자열을 분 단위로 변환 (24:00 대응)"""
    if t == "24:00": return 24 * 60
    h, m = map(int, t.split(':'))
    return h * 60 + m

def is_overlapping(start1, end1, start2, end2):
    """두 방송 시간대가 겹치는지 판단하는 인터벌 로직"""
    s1, e1 = time_to_min(start1), time_to_min(end1)
    s2, e2 = time_to_min(start2), time_to_min(end2)
    # 두 구간의 최대 시작값 < 최소 종료값 이면 겹침으로 간주
    return max(s1, s2) < min(e1, e2)

def add_categories(programs):
    if not programs: return programs
    pairs = [(p["brand"], p["product"]) for p in programs]
    categories = classify_batch(pairs)
    for p, cat in zip(programs, categories):
        p["category"] = cat
        p["product"] = clean_product_name(p["product"])
    return programs

def fetch_lotte(date_compact, date_dash, endpoint):
    headers = {"User-Agent": UA, "Referer": "https://www.lotteimall.com/main/viewMain.lotte"}
    url = f"https://www.lotteimall.com/main/{endpoint}.lotte?bdDate={date_compact}&date={date_dash}"
    programs = []
    
    try:
        r = requests.get(url, headers=headers, timeout=12)
        r.raise_for_status()
        prods = r.json().get("body", {}).get("prod", []) or []
        
        for p in prods:
            new_p = {
                "start": p.get("stime", ""),
                "end": p.get("etime", ""),
                "brand": p.get("brand", "") or "",
                "product": p.get("name", "") or "",
                "price": parse_price(p.get("price_disc")),
                "link": f"https://www.lotteimall.com{p.get('linkInfo', '')}" if p.get('linkInfo', '').startswith("/") else p.get('linkInfo', ''),
            }
            
            # [중복 방지 로직] 시간대가 겹치는 방송이 기존에 있다면 덮어씌움
            conflict_idx = -1
            for i, existing_p in enumerate(programs):
                if is_overlapping(new_p["start"], new_p["end"], existing_p["start"], existing_p["end"]):
                    conflict_idx = i
                    break
            
            if conflict_idx != -1:
                # 겹치는 슬롯이 있으면 최신 데이터(new_p)로 업데이트
                programs[conflict_idx] = new_p
            else:
                programs.append(new_p)
                
    except Exception as e:
        print(f"    [LT] 오류: {e}")
        
    programs.sort(key=lambda x: x["start"])
    return programs

BROADCASTS = [
    ("live", "scheduleLive"),
    ("data", "scheduleOne"),
]

def load_month(sub_dir, ym):
    path = os.path.join(sub_dir, f"{ym}.json")
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8")).get("days", {})
        except Exception:
            return {}
    return {}

def main():
    base = today_kst()
    today_str = base.strftime("%Y-%m-%d")

    for broadcast, endpoint in BROADCASTS:
        sub_dir = os.path.join(OUTPUT_DIR, f"LT_{broadcast}")
        os.makedirs(sub_dir, exist_ok=True)
        month_data = {}

        for offset in DAYS_RANGE:
            d = base + timedelta(days=offset)
            date_compact = d.strftime("%Y%m%d")
            date_dash = d.strftime("%Y-%m-%d")
            ym = d.strftime("%Y-%m")
            
            if ym not in month_data:
                month_data[ym] = load_month(sub_dir, ym)
            days = month_data[ym]

            # 과거 데이터는 무시하지 않고 새로 수집하여 덮어쓰도록 함 (중복 정리 목적)
            print(f"[LT_{broadcast}] {date_dash} 수집/정제 중...")
            programs = fetch_lotte(date_compact, date_dash, endpoint)
            programs = add_categories(programs)

            if date_dash < today_str and not programs:
                continue

            days[date_dash] = programs
            time.sleep(REQUEST_DELAY)

        for ym, days in month_data.items():
            if not days: continue
            out_path = os.path.join(sub_dir, f"{ym}.json")
            sorted_days = {k: days[k] for k in sorted(days)}
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({
                    "company": "LT", "broadcast": broadcast,
                    "month": ym, "days": sorted_days,
                }, f, ensure_ascii=False, indent=2)
            print(f"  저장 완료: {out_path}")

if __name__ == "__main__":
    main()
