# -*- coding: utf-8 -*-
"""
CJ온스타일(CJ) 편성표 수집기

TV LIVE(라이브방송) / TV+(데이터방송) 편성을 공통 스키마로 변환해 저장한다.

== 저장 구조 ==
homeshopping/
├── CJ_live/{YYYY-MM}.json   TV LIVE
└── CJ_data/{YYYY-MM}.json   TV+(데이터방송)

== 공통 스키마 (월 파일 안에 날짜별 누적) ==
{
  "company": "CJ", "broadcast": "live", "month": "2026-06",
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

== 브랜드 추출 ==
CJ API의 brandName은 거의 항상 비어있고(None), 상품 상세페이지(/p/item/{itemCd})는
JS로 렌더링되는 SPA라 requests로는 브랜드를 가져올 수 없다(별도 상세 REST API 미확인,
HTML에 데이터 없음 - require.js 로더만 내려옴). 그래서 상세페이지 파싱은 포기하고,
HD/LT처럼 brand 필드를 화면에 분리해서 보여주기 위해 categorize.resolve_display_brand()로
상품명(itemNm)에서 학습 데이터 브랜드 사전과 매칭해 브랜드를 추론한다.
추론에 실패하면(사전에 없는 브랜드, 또는 진짜 노브랜드 상품) brand는 빈 문자열로
저장되고, 프론트엔드는 빈 브랜드를 표시하지 않는다(HD/LT와 동일 처리 방식).

== 사용법 ==
  pip install requests
  python cj_scraper.py
"""
import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from categorize import classify_batch, resolve_display_brand_batch
from clean_product import clean_product_name

# 설정값
KST = timezone(timedelta(hours=9))
OUTPUT_DIR = "homeshopping"
REQUEST_DELAY = 0.8 
DAYS_RANGE = range(-1, 6)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

def today_kst():
    return datetime.now(KST)

def parse_price(v):
    if v is None: return 0
    if isinstance(v, (int, float)): return int(v)
    s = str(v).replace(",", "").strip()
    if not s: return 0
    try: return int(float(s))
    except ValueError: return 0

def add_categories(programs):
    """
    1) 브랜드 보강: brand가 비어있으면 product에서 추론해 화면 표시용 brand로 채움
       (HD/LT처럼 브랜드가 별도로 보이도록 - resolve_display_brand가 추론 실패시
        빈 문자열을 반환하므로, 추론 안 되는 진짜 노브랜드 상품은 그대로 빈 값 유지)
    2) 분류: 원본 상품명 + (보강된) 브랜드로 카테고리 예측
       (분류 모델은 원본 패턴으로 학습됐으므로 정제 전 텍스트를 사용)
    3) 분류가 끝난 뒤 product 필드를 화면 표시용으로 정제
    """
    if not programs: return programs

    raw_pairs = [(p["brand"], p["product"]) for p in programs]

    # 1) 화면 표시용 브랜드 보강 (브랜드 없으면 상품명에서 추론)
    display_brands = resolve_display_brand_batch(raw_pairs)
    for p, db in zip(programs, display_brands):
        if not p["brand"] and db:
            p["brand"] = db

    # 2) 분류 (보강된 brand + 원본 product 사용 - 추론된 브랜드가 있으면 분류 정확도 향상)
    pairs_for_model = [(p["brand"], p["product"]) for p in programs]
    categories = classify_batch(pairs_for_model)

    # 3) 정제
    for p, cat in zip(programs, categories):
        p["category"] = cat
        p["product"] = clean_product_name(p["product"])
    return programs

def fetch_cj(date_compact, broad_param):
    headers = {"User-Agent": UA, "Referer": "https://display.cjonstyle.com/p/tv/tvSchedule"}
    url = (f"https://display.cjonstyle.com/c/rest/tv/tvSchedule"
           f"?bdDt={date_compact}&isMobile=false&broadType={broad_param}&isEmployee=false")
    programs = []
    try:
        r = requests.get(url, headers=headers, timeout=12)
        r.raise_for_status()
        prog_list = r.json().get("result", {}).get("programList", []) or []
        for pg in prog_list:
            start_ms = pg.get("bdStrDtm")
            end_ms = pg.get("bdEndDtm")
            if not start_ms or not end_ms: continue
            
            start_str = datetime.fromtimestamp(start_ms / 1000, tz=KST).strftime("%H:%M")
            end_str = datetime.fromtimestamp(end_ms / 1000, tz=KST).strftime("%H:%M")

            items = pg.get("itemList", []) or []
            first = items[0] if items else {}
            item_cd = first.get("itemCd", "")
            chn_cd = first.get("chnCd", "")

            # API가 제공하는 brandName이 있으면 그대로 쓰고, 없으면 빈 문자열로 둔다.
            # (상세페이지는 JS SPA라 requests로 파싱 불가하고, pgmNm/itemNm을
            #  brand로 대신 쓰면 실제 브랜드가 아닌 값이 들어가 오히려 부정확함.
            #  빈 문자열로 두면 add_categories()가 상품명에서 브랜드를 추론해 채운다.)
            brand_name = first.get("brandName") or ""

            link = (f"https://display.cjonstyle.com/p/item/{item_cd}?channelCode={chn_cd}"
                    if item_cd else "")
            
            programs.append({
                "start": start_str,
                "end": end_str,
                "brand": brand_name,
                "product": first.get("itemNm", "") or "",
                "price": parse_price(first.get("salePrice")),
                "link": link,
            })
    except Exception as e:
        print(f"    [CJ] 오류: {e}")
    programs.sort(key=lambda x: x["start"])
    return programs

BROADCASTS = [("live", "live"), ("data", "plus")]

def load_month(sub_dir, ym):
    path = os.path.join(sub_dir, f"{ym}.json")
    if os.path.exists(path):
        try: return json.load(open(path, encoding="utf-8")).get("days", {})
        except: return {}
    return {}

def main():
    base = today_kst()
    today_str = base.strftime("%Y-%m-%d")

    for broadcast, broad_param in BROADCASTS:
        sub_dir = os.path.join(OUTPUT_DIR, f"CJ_{broadcast}")
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

            is_past = date_dash < today_str
            if is_past and days.get(date_dash):
                print(f"[CJ_{broadcast}] {date_dash}: 이미 기록됨, 건너뜀")
                continue

            print(f"[CJ_{broadcast}] {date_dash} 수집 중...")
            programs = fetch_cj(date_compact, broad_param)
            programs = add_categories(programs)

            if is_past and not programs:
                print(f"  -> 0개 (과거, 기존값 유지)")
                time.sleep(REQUEST_DELAY)
                continue

            days[date_dash] = programs
            print(f"  -> {len(programs)}개 편성")
            time.sleep(REQUEST_DELAY)

        for ym, days in month_data.items():
            if not days: continue
            out_path = os.path.join(sub_dir, f"{ym}.json")
            sorted_days = {k: days[k] for k in sorted(days)}
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({"company": "CJ", "broadcast": broadcast, "month": ym, "days": sorted_days}, 
                          f, ensure_ascii=False, indent=2)
            print(f"  저장: {out_path}")
    print("\n완료.")

if __name__ == "__main__":
    main()
