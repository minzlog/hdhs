# -*- coding: utf-8 -*-
"""
GS홈쇼핑 편성표 수집기 (라방바 데이터랩 API 경유)

GS 자체 서버(gsshop.com)는 클라우드/데이터센터 IP를 차단하여
GitHub Actions에서 직접 수집이 불가능하다.
대신 데이터 어그리게이터인 '라방바(ecomm-data.com)'의 공개 API를 사용한다.
이 API는 클라우드 IP 차단이 없어 GitHub Actions에서 정상 동작한다.

== 주의 ==
라방바는 방송 목록만 제공하며, "가격"과 "상품 링크"는 제공하지 않는다.
다른 3사(HD/LT/CJ)와 동일한 스키마를 맞추기 위해 해당 필드는 빈 값으로 둔다.
- price: 0 (프론트엔드에서 0이면 가격 표시를 생략)
- link: "" (프론트엔드에서 비어있으면 클릭 비활성)

== 브랜드 추출 ==
라방바 API는 brand 필드를 항상 빈 문자열로 준다(라방바가 브랜드/상품명을
분리해서 제공하지 않음). HD/LT처럼 화면에 브랜드를 따로 보여주기 위해
categorize.resolve_display_brand()로 product(hsshow_title) 텍스트에서
학습 데이터 브랜드 사전과 매칭해 브랜드를 추론한다("LG 통돌이 세탁기..."
-> brand="LG"). 추론에 실패하면(사전에 없는 브랜드, 진짜 노브랜드 상품)
brand는 빈 문자열로 남고, 프론트엔드는 빈 브랜드를 표시하지 않는다.

== 저장 구조 (3사와 동일) ==
homeshopping/
├── GS_live/{YYYY-MM}.json   GS SHOP (라이브)
└── GS_data/{YYYY-MM}.json   GS MY SHOP (데이터방송)

== 공통 스키마 (3사와 동일) ==
{
  "company": "GS", "broadcast": "live", "month": "2026-06",
  "days": {
    "2026-06-22": [
      {"start":"...","end":"...","brand":"","product":"...",
       "price":0,"link":"","category":"..."}
    ]
  }
}

== 사용법 ==
  pip install requests
  python gs_scraper.py
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from categorize import classify_batch, resolve_display_brand_batch
from clean_product import clean_product_name

KST = timezone(timedelta(hours=9))
OUTPUT_DIR = "homeshopping"
REQUEST_DELAY = 1.0  # 라방바 서버 부담을 줄이기 위해 1초 대기
DAYS_RANGE = range(-1, 6)  # 어제 ~ +5일

LAVANGBA_URL = "https://live.ecomm-data.com/api/schedule/list_hs"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 라방바 플랫폼 ID: 라이브(GS SHOP) / 데이터방송(GS MY SHOP)
PLATFORM_ID = {
    "live": "hs_gsshop",
    "data": "hs_gsshopmyshop",
}


def today_kst():
    return datetime.now(KST)


def fmt_time(raw: str) -> str:
    """'202606220100' -> 'HH:MM' (날짜 부분은 버리고 시간만 사용)"""
    if not raw or len(raw) < 12:
        return ""
    return f"{raw[8:10]}:{raw[10:12]}"


def add_categories(programs):
    """
    1) 브랜드 보강: 라방바는 brand를 항상 빈 문자열로 주므로, HD/LT처럼 화면에
       브랜드를 분리해서 보여주기 위해 product(hsshow_title)에서 학습 데이터
       브랜드 사전과 매칭해 추론한다. 추론 실패시(사전에 없는 브랜드, 진짜
       노브랜드 상품) brand는 그대로 빈 문자열로 남는다.
    2) 분류: 원본 상품명 + (보강된) 브랜드로 카테고리 예측
       (분류 모델은 원본 패턴으로 학습됐으므로 정제 전 텍스트를 사용 -
        브랜드가 채워지면 분류 확신도도 같이 올라간다)
    3) 분류가 끝난 뒤 product 필드를 화면 표시용으로 정제
    """
    if not programs:
        return programs

    raw_pairs = [(p["brand"], p["product"]) for p in programs]

    # 1) 화면 표시용 브랜드 보강
    display_brands = resolve_display_brand_batch(raw_pairs)
    for p, db in zip(programs, display_brands):
        if not p["brand"] and db:
            p["brand"] = db

    # 2) 분류 (보강된 brand + 원본 product)
    pairs_for_model = [(p["brand"], p["product"]) for p in programs]
    categories = classify_batch(pairs_for_model)

    # 3) 정제
    for p, cat in zip(programs, categories):
        p["category"] = cat
        p["product"] = clean_product_name(p["product"])
    return programs


def fetch_gs(date_obj: datetime, broadcast: str) -> list:
    """라방바 API를 통해 GS 편성표 1일치 수집. broadcast: 'live' | 'data'"""
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Origin": "https://live.ecomm-data.com",
        "Referer": "https://live.ecomm-data.com/schedule/hs",
    }
    date_yy = date_obj.strftime("%y%m%d")  # YYYY-MM-DD -> YYMMDD
    payload = {
        "date": date_yy,
        "type": None,
        "platform": [PLATFORM_ID[broadcast]],
        "cid": None,
    }

    try:
        r = requests.post(LAVANGBA_URL, json=payload, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"    [GS_{broadcast}] HTTP {r.status_code} 오류")
            return []

        data = r.json()
        programs = []
        for item in data.get("list", []) or []:
            start = fmt_time(item.get("hsshow_datetime_start", ""))
            end = fmt_time(item.get("hsshow_datetime_end", ""))
            if not start or not end:
                continue
            programs.append({
                "start": start,
                "end": end,
                "brand": "",                              # 라방바는 브랜드 별도 미제공
                "product": item.get("hsshow_title", "") or "",
                "price": 0,                                # 라방바는 가격 미제공
                "link": "",                                # 라방바는 링크 미제공
                "category": "",                            # add_categories에서 채움
            })

        programs.sort(key=lambda x: x["start"])
        return programs

    except Exception as e:
        print(f"    [GS_{broadcast}] 오류: {e}")
        return []


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

    for broadcast in ("live", "data"):
        sub_dir = os.path.join(OUTPUT_DIR, f"GS_{broadcast}")
        os.makedirs(sub_dir, exist_ok=True)
        month_data = {}

        for offset in DAYS_RANGE:
            d = base + timedelta(days=offset)
            date_dash = d.strftime("%Y-%m-%d")
            ym = d.strftime("%Y-%m")
            if ym not in month_data:
                month_data[ym] = load_month(sub_dir, ym)
            days = month_data[ym]

            is_past = date_dash < today_str
            if is_past and days.get(date_dash):
                print(f"[GS_{broadcast}] {date_dash}: 이미 기록됨, 건너뜀")
                continue

            print(f"[GS_{broadcast}] {date_dash} 수집 중...")
            programs = fetch_gs(d, broadcast)
            programs = add_categories(programs)

            if is_past and not programs:
                print(f"  -> 0개 (과거, 기존값 유지)")
                time.sleep(REQUEST_DELAY)
                continue

            days[date_dash] = programs
            print(f"  -> {len(programs)}개 편성")
            time.sleep(REQUEST_DELAY)

        for ym, days in month_data.items():
            if not days:
                continue
            out_path = os.path.join(sub_dir, f"{ym}.json")
            sorted_days = {k: days[k] for k in sorted(days)}
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({
                    "company": "GS", "broadcast": broadcast,
                    "month": ym, "days": sorted_days,
                }, f, ensure_ascii=False, indent=2)
            print(f"  저장: {out_path} ({len(sorted_days)}일)")

    print("\n완료.")


if __name__ == "__main__":
    main()
