# -*- coding: utf-8 -*-
"""
기타 홈쇼핑 7개사 편성표 수집기 (라방바 데이터랩 API 경유)

gs_scraper.py와 동일한 방식으로 라방바(ecomm-data.com)의 공개 API를
사용한다. GS 전용 스크립트와 분리한 이유는, 이 7개사는 대부분 GS처럼
"라이브/마이샵" 같은 서브 채널 구분이 없고 회사당 platform_id가 1개씩만
존재하기 때문이다 (단, NS홈쇼핑은 예외 - 아래 참고).

대상 7개사 (프론트 표시명):
  공영쇼핑   -> 공영
  홈앤쇼핑   -> 홈앤
  KT알파쇼핑 -> K쇼핑
  신세계쇼핑 -> 신세계
  NS홈쇼핑   -> NS   (+ 샵플러스 서브채널도 함께 수집)
  쇼핑엔티   -> 쇼핑엔티
  SK스토아   -> SK스토아

탭 구성: 프론트엔드에서 '기타' 탭 하나로 묶어서 보여주되, 데이터 저장은
회사별로 분리한다(GS_live / GS_data처럼).

== platform_id (브라우저 Network 탭에서 실제 요청 캡처로 검증됨) ==
  공영쇼핑      hs_gongyoung
  홈앤쇼핑      hs_hnsmall
  KT알파쇼핑    hs_kshop
  신세계쇼핑    hs_shinsegae
  NS홈쇼핑      hs_nsmall
  NS홈쇼핑 샵플러스  hs_nsmallshopplus   (NS의 서브채널, GS의 마이샵과 동일한 패턴)
  쇼핑엔티      hs_shopntmall
  SK스토아      hs_skstoa

== 2단계 수집 (link/price 보강) ==
GS와 동일. 1단계 list_hs 응답의 hsshow_id로 report/hsshow/{id} 상세
페이지를 호출해 __NEXT_DATA__ 안의 상품 정보(item_url, price)를 가져온다.
상품이 여러 개여도 대표 1개(items[0])만 사용.

== 브랜드 추출 / 카테고리 분류 ==
GS와 동일하게 categorize.resolve_display_brand_batch() /
classify_batch()를 그대로 사용한다 (기준 변경 없음, 요청사항 반영).

== 저장 구조 ==
homeshopping/
├── PUBLIC_live/{YYYY-MM}.json      공영쇼핑
├── HNS_live/{YYYY-MM}.json         홈앤쇼핑
├── KTALPHA_live/{YYYY-MM}.json     KT알파쇼핑
├── SHINSEGAE_live/{YYYY-MM}.json   신세계쇼핑
├── NS_live/{YYYY-MM}.json          NS홈쇼핑 (본채널)
├── NS_plus/{YYYY-MM}.json          NS홈쇼핑 샵플러스
├── SHOPPINGNT_live/{YYYY-MM}.json  쇼핑엔티
└── SKSTOA_live/{YYYY-MM}.json      SK스토아

(디렉토리 네이밍을 GS/HD/LT/CJ와 동일한 패턴({COMPANY}_{broadcast})으로
 맞춰서 프론트엔드 로더 로직을 그대로 재사용할 수 있게 한다. NS만
 broadcast가 "live"/"plus" 두 개이고, 나머지 6개사는 "live" 하나뿐.)

== 공통 스키마 (4사와 동일 + lavangba_category 추가) ==
{
  "company": "PUBLIC", "broadcast": "live", "month": "2026-06",
  "days": {
    "2026-06-22": [
      {"start":"...","end":"...","brand":"","product":"...",
       "price":0,"link":"","category":"...","lavangba_category":"..."}
    ]
  }
}
- category: 자체 분류 모델(TF-IDF+로지스틱회귀) 결과 (기존 기준, 변경 없음)
- lavangba_category: 라방바 list_hs 응답의 item["cat"]["cat_name"]을
  그대로 저장 (라방바 화면 "분류" 컬럼과 동일 값). 자체 모델을 대체하지
  않고 검증/비교용으로 별도 보관. 없으면 빈 문자열.

== 프론트엔드 탭/표시명 매핑 (참고용, 이 스크립트가 직접 쓰진 않음) ==
company 코드 -> 탭 내 표시 라벨
  PUBLIC      -> 공영
  HNS         -> 홈앤
  KTALPHA     -> K쇼핑
  SHINSEGAE   -> 신세계
  NS          -> NS          (NS_live + NS_plus 둘 다 NS 라벨로 묶어서 표시)
  SHOPPINGNT  -> 쇼핑엔티
  SKSTOA      -> SK스토아
프론트는 위 7개 company를 묶어서 '기타' 탭 하나로 렌더링하면 됨.

== 사용법 ==
  pip install requests
  python etc_scraper.py
"""

import os
import re
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from categorize import classify_batch, resolve_display_brand_batch
from clean_product import clean_product_name

KST = timezone(timedelta(hours=9))
OUTPUT_DIR = "homeshopping"
REQUEST_DELAY = 1.0         # 1단계(list_hs) 호출 사이 대기
DETAIL_REQUEST_DELAY = 0.4  # 2단계(report/hsshow 상세) 호출 사이 대기
DAYS_RANGE = range(-1, 6)   # 어제 ~ +5일

LAVANGBA_URL = "https://live.ecomm-data.com/api/schedule/list_hs"
DETAIL_URL_TMPL = "https://live.ecomm-data.com/report/hsshow/{hsshow_id}"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ============================================================
# platform_id - 브라우저 Network 탭에서 실제 list_hs 요청을 캡처해
# 검증된 값. {company: {broadcast: platform_id}} 구조.
# NS만 본채널(live)/샵플러스(plus) 두 서브채널을 갖고, 나머지 6개사는
# "live" 서브채널 하나뿐 (GS의 live/data 구조와 동일한 패턴).
# ============================================================
PLATFORM_ID = {
    "PUBLIC":     {"live": "hs_gongyoung"},      # 공영쇼핑
    "HNS":        {"live": "hs_hnsmall"},        # 홈앤쇼핑
    "KTALPHA":    {"live": "hs_kshop"},          # KT알파쇼핑
    "SHINSEGAE":  {"live": "hs_shinsegae"},      # 신세계쇼핑
    "NS":         {"live": "hs_nsmall",          # NS홈쇼핑 (본채널)
                    "plus": "hs_nsmallshopplus"},# NS홈쇼핑 샵플러스
    "SHOPPINGNT": {"live": "hs_shopntmall"},     # 쇼핑엔티
    "SKSTOA":     {"live": "hs_skstoa"},         # SK스토아
}

# 프론트엔드 '기타' 탭에 표시할 라벨 (요청하신 표시명 그대로)
COMPANY_LABEL = {
    "PUBLIC":     "공영",
    "HNS":        "홈앤",
    "KTALPHA":    "K쇼핑",
    "SHINSEGAE":  "신세계",
    "NS":         "NS",
    "SHOPPINGNT": "쇼핑엔티",
    "SKSTOA":     "SK스토아",
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
    GS와 동일한 브랜드 보강 + 분류 + 정제 로직 (기준 변경 없음).
    1) product(hsshow_title) 텍스트에서 브랜드 사전 매칭으로 brand 보강
    2) 원본 product + 보강된 brand로 카테고리 분류
    3) 분류 후 product를 화면 표시용으로 정제
    """
    if not programs:
        return programs

    raw_pairs = [(p["brand"], p["product"]) for p in programs]

    display_brands = resolve_display_brand_batch(raw_pairs)
    for p, db in zip(programs, display_brands):
        if not p["brand"] and db:
            p["brand"] = db

    pairs_for_model = [(p["brand"], p["product"]) for p in programs]
    categories = classify_batch(pairs_for_model)

    for p, cat in zip(programs, categories):
        p["category"] = cat
        p["product"] = clean_product_name(p["product"])
    return programs


def fetch_detail(hsshow_id: str) -> tuple:
    """
    report/hsshow/{hsshow_id} 상세 페이지에서 __NEXT_DATA__를 파싱해
    상품 링크/가격을 추출. (link, price) 반환, 실패 시 ("", 0).
    gs_scraper.py의 fetch_gs_detail()과 동일한 로직.
    """
    headers = {"User-Agent": UA, "Referer": "https://live.ecomm-data.com/schedule/hs"}
    url = DETAIL_URL_TMPL.format(hsshow_id=hsshow_id)
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code != 200:
            return "", 0

        m = NEXT_DATA_RE.search(r.text)
        if not m:
            return "", 0

        data = json.loads(m.group(1))
        products = (
            data.get("props", {})
            .get("pageProps", {})
            .get("ss_data", {})
            .get("products", {})
        )
        items = products.get("items") or []
        if not items:
            return "", 0

        first = items[0]
        link = first.get("item_url", "") or ""
        price = first.get("price") or first.get("price_sales") or 0
        return link, int(price) if price else 0

    except Exception as e:
        print(f"      [detail] {hsshow_id} 오류: {e}")
        return "", 0


def fetch_company(date_obj: datetime, platform_id: str, log_label: str) -> list:
    """라방바 API를 통해 1개 platform_id(회사 또는 서브채널) 1일치
    편성표 수집. 1단계 list_hs로 목록 -> 2단계 상세페이지로 link/price
    보강 (gs_scraper.py의 fetch_gs()와 동일 구조)."""
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/json",
        "Origin": "https://live.ecomm-data.com",
        "Referer": "https://live.ecomm-data.com/schedule/hs",
    }
    date_yy = date_obj.strftime("%y%m%d")
    payload = {
        "date": date_yy,
        "type": None,
        "platform": [platform_id],
        "cid": None,
    }

    try:
        r = requests.post(LAVANGBA_URL, json=payload, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"    [{log_label}] HTTP {r.status_code} 오류")
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
                "brand": "",
                "product": item.get("hsshow_title", "") or "",
                "price": 0,
                "link": "",
                "category": "",                            # add_categories에서 채움 (자체 모델)
                "lavangba_category": (item.get("cat") or {}).get("cat_name", "") or "",  # 라방바 자체 제공 분류 (참고/검증용)
                "_hsshow_id": item.get("hsshow_id", ""),
            })

        programs.sort(key=lambda x: x["start"])

        for p in programs:
            hsshow_id = p.pop("_hsshow_id", "")
            if not hsshow_id:
                continue
            link, price = fetch_detail(hsshow_id)
            p["link"] = link
            p["price"] = price
            time.sleep(DETAIL_REQUEST_DELAY)

        return programs

    except Exception as e:
        print(f"    [{log_label}] 오류: {e}")
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

    for company, broadcasts in PLATFORM_ID.items():
        for broadcast, platform_id in broadcasts.items():
            log_label = f"{company}_{broadcast}"
            sub_dir = os.path.join(OUTPUT_DIR, log_label)
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
                    print(f"[{log_label}] {date_dash}: 이미 기록됨, 건너뜀")
                    continue

                print(f"[{log_label}] {date_dash} 수집 중...")
                programs = fetch_company(d, platform_id, log_label)
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
                        "company": company, "broadcast": broadcast,
                        "month": ym, "days": sorted_days,
                    }, f, ensure_ascii=False, indent=2)
                print(f"  저장: {out_path} ({len(sorted_days)}일)")

    print("\n완료.")


if __name__ == "__main__":
    main()
