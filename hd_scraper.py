# -*- coding: utf-8 -*-
"""
현대홈쇼핑(HD) 편성표 수집기

라이브방송(TV쇼핑) / 데이터방송(TV+샵) 편성을 공통 스키마로 변환해 저장한다.
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
REQUEST_DELAY = 0.4
DAYS_RANGE = range(-1, 6)  # 어제 ~ +5일

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def today_kst():
    return datetime.now(KST)


def parse_price(v):
    """가격을 원 단위 정수로 정규화."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def add_categories(programs):
    """카테고리 분류 및 상품명 정제."""
    if not programs:
        return programs
    pairs = [(p["brand"], p["product"]) for p in programs]
    categories = classify_batch(pairs)
    for p, cat in zip(programs, categories):
        p["category"] = cat
        p["product"] = clean_product_name(p["product"])
    return programs


def fetch_hyundai(date_compact, broad_param):
    """
    date_compact: 'YYYYMMDD'
    broad_param: 'etv'(라이브) | 'dtv'(데이터)
    """
    headers = {"User-Agent": UA, "Referer": "https://www.hmall.com/"}
    seen = {}
    for page in range(0, 8):
        url = (f"https://www.hmall.com/md/api/cache?url=/api/hf/dp/v1/main-tv-new/tv-list"
               f"&brodDt={date_compact}&brodPrrgPage={page}&brodType={broad_param}&deviceInfo=pc")
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                continue
            items = r.json().get("respData", {}).get("broadItemList", []) or []
            for it in items:
                key = (it.get("brodStrtDtm"), it.get("slitmCd"))
                if key[0] is None:
                    continue
                slitm = it.get("slitmCd")
                seen[key] = {
                    "start": it.get("brodStrtDtm", ""),
                    "end": it.get("brodEndDtm", ""),
                    "brand": it.get("brndNm", "") or "",
                    "product": it.get("convertedSlitmNm") or it.get("slitmNm") or "",
                    "price": parse_price(it.get("sellPrc")),
                    "link": f"https://www.hmall.com/md/pda/itemPtc?slitmCd={slitm}&preview=true" if slitm else "",
                }
        except Exception as e:
            print(f"    [HD] page {page} 오류: {e}")
        time.sleep(0.15)

    programs = sorted(seen.values(), key=lambda x: x["start"])

    # 데이터방송(dtv) 한정 시간 조정 로직 추가
    if broad_param == "dtv" and programs:
        for i in range(len(programs) - 1):
            programs[i]["end"] = programs[i + 1]["start"]
        
        # 마지막 방송 종료 시각 보정 (ex: 01:19 -> 01:20 / 23:59 -> 24:00)
        last_p = programs[-1]
        if last_p["end"]:
            try:
                h, m = map(int, last_p["end"].split(":"))
                m += 1
                if m >= 60:
                    m = 0
                    h += 1
                if h >= 24:
                    last_p["end"] = "24:00"
                else:
                    last_p["end"] = f"{h:02d}:{m:02d}"
            except Exception:
                pass

    return programs


BROADCASTS = [
    ("live", "etv"),
    ("data", "dtv"),
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

    for broadcast, broad_param in BROADCASTS:
        sub_dir = os.path.join(OUTPUT_DIR, f"HD_{broadcast}")
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
                print(f"[HD_{broadcast}] {date_dash}: 이미 기록됨, 건너뜀")
                continue

            print(f"[HD_{broadcast}] {date_dash} 수집 중...")
            programs = fetch_hyundai(date_compact, broad_param)
            programs = add_categories(programs)

            if is_past and not programs:
                print(f"  -> 0개 (과거, 기존값 유지)")
                time.sleep(REQUEST_DELAY)
                continue

            days[date_dash] = programs
            print(f"  -> {len(programs)}개編成")
            time.sleep(REQUEST_DELAY)

        for ym, days in month_data.items():
            if not days:
                continue
            out_path = os.path.join(sub_dir, f"{ym}.json")
            sorted_days = {k: days[k] for k in sorted(days)}
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({
                    "company": "HD", "broadcast": broadcast,
                    "month": ym, "days": sorted_days,
                }, f, ensure_ascii=False, indent=2)
            print(f"  저장: {out_path} ({len(sorted_days)}일)")

    print("\n완료.")


if __name__ == "__main__":
    main()
