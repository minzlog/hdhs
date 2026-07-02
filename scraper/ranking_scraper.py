# -*- coding: utf-8 -*-
"""
홈쇼핑 인기 랭킹(주간) 수집기 - 홈쇼핑모아 DataHub(datahub.hsmoa.com) 경유

== 실행 주기 / 순위변동 추적 ==
매일 1회 실행한다. 저장 파일은 여전히 "그 주(월요일 기준) 1개"이지만
(data/ranking/{weekStart}.json), 매일 덮어쓰기 전에 기존 파일을 먼저 읽어서
직전 실행 시점의 pdid별 순위를 기억해두고, 새로 수집한 순위와 비교해
rank_change를 계산한다.
  rank_change > 0 : 순위 상승 (숫자가 작아짐, 예: 5위 -> 2위 => +3)
  rank_change < 0 : 순위 하락
  rank_change = null : 그 주 들어 이 카테고리에 처음 등장 (신규 진입, 또는
                       그 주의 첫 실행이라 비교 대상 자체가 없음)
월요일 첫 실행은 비교 대상이 없어 전부 null(NEW)로 뜨는 게 정상이다.

== 1단계: 랭킹 목록 수집 ==
datahub.hsmoa.com의 내부 API를 그대로 사용한다(브라우저 Network 탭에서
캡처 확인됨, 로그인 없이도 200 응답):

  GET https://datahub.hsmoa.com/next-api/insights/ranking?limit=100&time_range=week[&category1=카테고리명]

- "전체" 카테고리는 category1 파라미터를 생략한다.
- 나머지 17개 카테고리는 category1에 한글 카테고리명을 그대로 넣는다
  (requests가 알아서 URL 인코딩).
- revenue/sales_count 등은 로그인(유료 플랜) 전용이라 응답에서 항상 null로
  온다 - 저장하지 않는다. rank/product/broadcast/tier/badge/sales_ratio만
  사용한다.
- time_range=week가 정확히 "고정 캘린더 주"인지 "최근 7일 롤링"인지는
  hsmoa 쪽 공식 문서가 없어 단정할 수 없다. 매일 실행해도 문제없도록
  설계했으니 어느 쪽이든 상관없다.

== 2단계: 바로가기(구매) 링크 enrichment ==
랭킹 API 응답에는 실제 구매 링크가 없다. 대신 상품 상세페이지
  GET https://datahub.hsmoa.com/product/{pdid}
가 SSR이라, 응답 HTML 안에 그대로
  <a target="_blank" rel="noopener noreferrer" href="실제채널구매링크">
형태로 박혀 있다. 채널마다(cjmall -> display.cjonstyle.com,
gsshop -> with.gsshop.com/alia/... 등) 링크 형식이 완전히 달라서
패턴을 코드로 암기하지 않고, 매번 실제 상세페이지를 요청해서 그대로
긁어온다.

같은 상품(pdid)은 여러 카테고리 랭킹에 중복 등장하거나 날짜가 바뀌어도
계속 인기 상품일 수 있으므로, link_cache.json에 한번 조회한 pdid->link를
계속 누적 캐싱해서 불필요한 재요청을 줄인다. 매일 실행해도 캐시 덕분에
신규 진입 상품의 pdid만 새로 조회하면 되므로 갈수록 빨라진다.

== 저장 구조 ==
data/ranking/
├── {weekStart}.json      예: 2026-06-29.json (월요일 날짜 기준, 매일 덮어씀)
│   {
│     "weekStart": "2026-06-29", "weekEnd": "2026-07-05",
│     "collectedAt": "2026-07-02T15:50:49+09:00",
│     "categories": {
│       "전체": [ {rank, pdid, tier, badge, sales_ratio, rank_change,
│                  product:{name,brand,image,price,sale_price,
│                           channel,category1,category2,category3},
│                  broadcast:{channel_name,start_time,end_time},
│                  link:"실제 구매 링크"}, ... ],
│       "의류": [...], "식품": [...], ...
│     }
│   }
└── link_cache.json        {pdid: link, ...} 누적 캐시

드라마/예능 탭(data/dramavariety/{weekStart}.json)과 동일하게 주(월요일)
단위 스냅샷 파일로 관리해서, 프론트엔드에서 같은 주간 탐색 UI를 재사용한다.

== 사용법 ==
  pip install requests
  python ranking_scraper.py
"""

import os
import re
import json
import time
import requests
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
OUTPUT_DIR = "data/ranking"
LINK_CACHE_PATH = os.path.join(OUTPUT_DIR, "link_cache.json")

RANKING_URL = "https://datahub.hsmoa.com/next-api/insights/ranking"
PRODUCT_URL_TMPL = "https://datahub.hsmoa.com/product/{pdid}"
LIMIT = 100
TIME_RANGE = "week"

CATEGORY_REQUEST_DELAY = 0.6   # 카테고리(18개) 호출 사이 대기
DETAIL_REQUEST_DELAY = 0.4     # 상품 상세페이지(바로가기 링크) 호출 사이 대기

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.5 Safari/605.1.15")

RANKING_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://datahub.hsmoa.com/ranking",
}

DETAIL_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://datahub.hsmoa.com/ranking",
}

# 랭킹 페이지 카테고리 필터 버튼 순서 그대로 (전체 포함 18개)
CATEGORIES = [
    "전체", "의류", "식품", "잡화", "뷰티", "스포츠/레저", "생필품/주방",
    "가구/인테리어", "가전", "건강", "서비스/금융", "반려동물", "출산/육아",
    "자동차/공구", "취미", "문화/컨텐츠", "디지털", "컴퓨터",
]

# 상세페이지 <a> 태그의 속성 순서가 채널/카드마다 제각각이라(예: href가
# target보다 먼저 오는 경우가 실제로 훨씬 많았음), 속성 순서를 가정하지 않고
# 태그 하나를 통째로 잡은 뒤 그 안에서 target="_blank" 여부와 href를 따로 검사한다.
TAG_RE = re.compile(r'<a\b([^>]*)>')
HREF_ATTR_RE = re.compile(r'href="([^"]+)"')


def now_kst() -> datetime:
    return datetime.now(KST)


def to_date_str(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def monday_of(d: datetime) -> datetime:
    return d - timedelta(days=d.weekday())


def fetch_ranking(category: str) -> list:
    """카테고리 1개의 주간 랭킹 top100을 가져온다. 실패 시 빈 리스트."""
    params = {"limit": LIMIT, "time_range": TIME_RANGE}
    if category != "전체":
        params["category1"] = category

    try:
        r = requests.get(RANKING_URL, params=params, headers=RANKING_HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"    [ranking:{category}] HTTP {r.status_code} 오류")
            return []
        data = r.json()
        items = data.get("items", []) or []

        cleaned = []
        for it in items:
            product = it.get("product") or {}
            broadcast = it.get("broadcast") or {}
            if not product.get("name"):
                # 상품 정보가 비어있는 항목(가끔 등장)은 스킵
                continue
            cleaned.append({
                "rank": it.get("rank"),
                "pdid": it.get("pdid", ""),
                "tier": it.get("tier", ""),
                "badge": it.get("badge", ""),
                "sales_ratio": it.get("sales_ratio"),
                "product": {
                    "name": product.get("name", ""),
                    "brand": product.get("brand", ""),
                    "image": product.get("image", ""),
                    "price": product.get("price"),
                    "sale_price": product.get("sale_price"),
                    "channel": product.get("channel", ""),
                    "category1": product.get("category1", ""),
                    "category2": product.get("category2", ""),
                    "category3": product.get("category3", ""),
                },
                "broadcast": {
                    "channel_name": broadcast.get("channel_name", ""),
                    "start_time": broadcast.get("start_time", ""),
                    "end_time": broadcast.get("end_time", ""),
                },
                "link": "",  # 2단계에서 채움
            })
        return cleaned

    except Exception as e:
        print(f"    [ranking:{category}] 오류: {e}")
        return []


def fetch_outbound_link(pdid: str) -> str:
    """상품 상세페이지에서 '바로가기' 실제 구매 링크를 추출한다. 실패 시 빈 문자열.

    상세페이지에는 target="_blank" 달린 <a> 태그가 여러 개 있다(진짜 구매
    버튼 1개 + 하단 '관련상품' 카드 여러 개). 관련상품 카드는 href가
    "/product/{다른pdid}" 같은 사이트 내부 상대경로이고, 진짜 구매 버튼만
    실채널(cjmall/gsshop/...) 도메인의 절대 URL(http로 시작)이므로 이걸로
    구분해서 첫 번째로 매치되는 절대 URL을 채택한다."""
    url = PRODUCT_URL_TMPL.format(pdid=pdid)
    try:
        r = requests.get(url, headers=DETAIL_HEADERS, timeout=12)
        if r.status_code != 200:
            return ""
        for attrs in TAG_RE.findall(r.text):
            if 'target="_blank"' not in attrs:
                continue
            m = HREF_ATTR_RE.search(attrs)
            if not m:
                continue
            href = m.group(1)
            if href.startswith("http"):
                return href.replace("&amp;", "&")
        return ""
    except Exception as e:
        print(f"      [link] {pdid} 오류: {e}")
        return ""


def load_previous_snapshot(week_start: str) -> dict:
    """오늘 덮어쓰기 전, 그 주 파일이 이미 있으면 카테고리별 데이터를 읽어온다.
    (같은 주의 어제/직전 실행 결과 - 순위변동 계산용)"""
    path = os.path.join(OUTPUT_DIR, f"{week_start}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f).get("categories", {})
        except Exception:
            return {}
    return {}


def build_rank_lookup(prev_categories: dict) -> dict:
    """{카테고리: {pdid: 순위}} 형태로 변환 (비교하기 쉽게)."""
    lookup = {}
    for cat, items in prev_categories.items():
        lookup[cat] = {it.get("pdid"): it.get("rank") for it in items if it.get("pdid")}
    return lookup


def apply_rank_change(categories_data: dict, prev_lookup: dict):
    """직전 순위와 비교해 각 아이템에 rank_change를 채운다.
    양수=순위 상승, 음수=순위 하락, None=이 주 첫 등장(또는 그 주 첫 실행)."""
    for cat, items in categories_data.items():
        cat_prev = prev_lookup.get(cat, {})
        for it in items:
            prev_rank = cat_prev.get(it["pdid"])
            if prev_rank is not None and it["rank"] is not None:
                it["rank_change"] = prev_rank - it["rank"]
            else:
                it["rank_change"] = None


def load_link_cache() -> dict:
    if os.path.exists(LINK_CACHE_PATH):
        try:
            with open(LINK_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_link_cache(cache: dict):
    with open(LINK_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    base = now_kst()
    week_start_dt = monday_of(base)
    week_end_dt = week_start_dt + timedelta(days=6)
    week_start = to_date_str(week_start_dt)
    week_end = to_date_str(week_end_dt)

    print(f"[랭킹] {week_start} ~ {week_end} 주간 수집 시작")

    prev_lookup = build_rank_lookup(load_previous_snapshot(week_start))

    categories_data = {}
    all_pdids = set()

    for cat in CATEGORIES:
        print(f"  [ranking] {cat} 수집 중...")
        items = fetch_ranking(cat)
        categories_data[cat] = items
        for it in items:
            if it["pdid"]:
                all_pdids.add(it["pdid"])
        print(f"    -> {len(items)}개")
        time.sleep(CATEGORY_REQUEST_DELAY)

    # 2단계: 바로가기 링크 enrichment (캐시 활용)
    link_cache = load_link_cache()
    new_pdids = [p for p in sorted(all_pdids) if p not in link_cache]
    print(f"  [link] 전체 고유 상품 {len(all_pdids)}개 / 신규 조회 {len(new_pdids)}개")

    for i, pdid in enumerate(new_pdids, 1):
        link = fetch_outbound_link(pdid)
        link_cache[pdid] = link
        if i % 20 == 0:
            print(f"    ... {i}/{len(new_pdids)}")
        time.sleep(DETAIL_REQUEST_DELAY)

    save_link_cache(link_cache)

    for cat, items in categories_data.items():
        for it in items:
            it["link"] = link_cache.get(it["pdid"], "")

    apply_rank_change(categories_data, prev_lookup)

    out_path = os.path.join(OUTPUT_DIR, f"{week_start}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "weekStart": week_start,
            "weekEnd": week_end,
            "collectedAt": base.isoformat(),
            "categories": categories_data,
        }, f, ensure_ascii=False, indent=2)

    total = sum(len(v) for v in categories_data.values())
    print(f"\n완료. 저장: {out_path} (카테고리 {len(categories_data)}개, 총 {total}건)")


if __name__ == "__main__":
    main()
