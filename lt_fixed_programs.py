# -*- coding: utf-8 -*-
"""
lt_fixed_programs.py
롯데홈쇼핑 고정 편성 프로그램(진행자 쇼) 목록을 자동으로 발견하며 수집한다.

== 핵심 아이디어 (그래프 크롤링) ==
롯데 메인페이지의 "롯데홈쇼핑 대표 프로그램" 캐러셀은 JS 클릭으로만
각 프로그램의 conts_no를 알 수 있어 정적으로 추출이 안 된다.

반면 각 프로그램 상세페이지
  https://www.lotteimall.com/contstmpl/viewContsTmplDetail.lotte?conts_no={N}
안의 "같이 보면 좋은 콘텐츠"(conts_tmpl_recomm_info) 섹션에는
다른 프로그램들의 썸네일이 나열되는데, 그 이미지 파일명이
  {conts_no}_10_{타임스탬프}.(jpg|png)
형식으로 항상 진짜 conts_no를 그대로 노출한다.
(주의: 이 섹션의 <a href> 값은 JS 미실행 상태에서는 신뢰할 수 없고,
 이미지 파일명만 신뢰할 수 있는 소스다.)

따라서 시작 conts_no 하나에서 출발해 추천 섹션에 나온 conts_no를
계속 새로 발견하며 BFS로 펼쳐나가면, 신규 프로그램이 생기거나
없어져도 사람이 매번 ID를 수동으로 넣지 않고 전체 프로그램 목록을
자동으로 따라갈 수 있다.

== 각 프로그램 페이지에서 함께 추출하는 정보 ==
  - 프로그램명 (h3 또는 og:title 등에서)
  - 편성 시각 (방송 중인 슬라이드의 시간 표기, pgm_top 영역)
  - 이번 방송 소개 상품 (live_goods_info 섹션: 상품명/가격/이미지/링크)

== 출력 ==
homeshopping/fixed_programs/LT.json
{
  "company": "LT",
  "collectedAt": "...",
  "programs": [
    {
      "conts_no": "1147",
      "title": "최희의 희트템",
      "schedule_raw": "목 18:30",
      "upcoming_products": [
        {"name": "...", "price": 84150, "image": "...", "link": "..."},
        ...
      ],
      "discovered_from": ["1170", "73", ...]   # 어떤 프로그램들의 추천 섹션에서 발견됐는지 (참고용)
    },
    ...
  ]
}

== 사용법 ==
  pip install requests beautifulsoup4
  python lt_fixed_programs.py [시작 conts_no]
  (시작 conts_no를 안 주면 SEED_CONTS_NO 기본값 사용)
"""

import os
import re
import sys
import json
import time
import requests
from collections import deque
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))

# 최초 시작점. 이 프로그램이 사라지더라도 다른 아무 conts_no로 교체하면
# 추천 섹션을 타고 결국 전체 프로그램 그래프에 다시 도달한다.
SEED_CONTS_NO = "1147"

DETAIL_URL = "https://www.lotteimall.com/contstmpl/viewContsTmplDetail.lotte?conts_no={conts_no}"
OUTPUT_DIR = os.path.join("homeshopping", "fixed_programs")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "LT.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_DELAY_SEC = 0.6
MAX_PAGES = 60  # 무한 크롤링 방지용 안전장치


def parse_price(text: str):
    if not text:
        return None
    cleaned = re.sub(r'[^\d]', '', text)
    return int(cleaned) if cleaned else None


def fetch_page(conts_no: str) -> str:
    headers = {"User-Agent": UA, "Referer": "https://www.lotteimall.com/"}
    url = DETAIL_URL.format(conts_no=conts_no)
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.text


def extract_program_title(soup: BeautifulSoup) -> str:
    """롯데 상세페이지는 진행자명을 로고 이미지로만 표시하는 경우가 많아
    페이지 자체에서 텍스트 타이틀을 못 찾을 수 있다.
    (이 경우 호출부에서 다른 페이지의 추천 섹션이 이 프로그램을
     부른 이름(recomm_title)으로 보완한다.)"""
    og_title = soup.select_one('meta[property="og:title"]')
    if og_title and og_title.get("content"):
        content = og_title["content"].strip()
        if content and content not in ("롯데홈쇼핑", "[롯데홈쇼핑]"):
            return content
    return ""


def extract_schedule_text(soup: BeautifulSoup) -> str:
    """편성 시각 텍스트. main_area 안의 <p class="time">이 진짜 위치."""
    time_tag = soup.select_one(".main_area .time")
    if time_tag:
        text = time_tag.get_text(strip=True)
        if text:
            return text
    # 보조: '다가오는 방송 일정' 영역의 날짜 표기
    date_tag = soup.select_one(".preinfo .date")
    if date_tag:
        return date_tag.get_text(" ", strip=True)
    return ""


def extract_recommend_titles(soup: BeautifulSoup) -> dict:
    """'같이 보면 좋은 콘텐츠' 섹션에서 {conts_no: 그 페이지가 부르는 이름} 매핑을 만든다.
    자기 자신을 가리키는 항목(이미지 파일명이 현재 페이지일 경우)은 자연히
    이 함수 결과에 포함되지 않으므로 별도 처리는 필요 없다."""
    section = soup.select_one(".conts_tmpl_recomm_info")
    titles = {}
    if not section:
        return titles
    for li in section.select("li.swiper_slide"):
        img = li.select_one(".img_wrap.big img")
        title_tag = li.select_one(".recomm_title")
        if not img or not title_tag:
            continue
        src = img.get("src", "") or img.get("data-src", "")
        m = re.search(r'/(\d+)_10_\d+\.(?:jpg|jpeg|png)', src)
        if m:
            titles[m.group(1)] = title_tag.get_text(strip=True)
    return titles


def extract_upcoming_products(soup: BeautifulSoup, base_url: str) -> list:
    """live_goods_info(이번 방송 소개 상품) 섹션에서 상품 카드 추출."""
    products = []
    container = soup.select_one(".live_goods_info")
    if not container:
        return products

    for li in container.select("li.swiper_slide"):
        title_tag = li.select_one(".product_title")
        price_tag = li.select_one(".product_price .price")
        img_tag = li.select_one(".img_wrap img")
        link_tag = li.select_one("a[href]")

        if not title_tag:
            continue

        link = None
        if link_tag and link_tag.get("href"):
            link = link_tag["href"].split("#")[0]  # '#none' 등 앵커 제거

        products.append({
            "name": title_tag.get_text(strip=True),
            "price": parse_price(price_tag.get_text(strip=True)) if price_tag else None,
            "image": img_tag.get("src") if img_tag else None,
            "link": link,
        })

    return products


def crawl_program(conts_no: str) -> dict:
    html = fetch_page(conts_no)
    soup = BeautifulSoup(html, "html.parser")

    title = extract_program_title(soup)
    schedule_raw = extract_schedule_text(soup)
    upcoming_products = extract_upcoming_products(soup, DETAIL_URL.format(conts_no=conts_no))
    recommend_titles = extract_recommend_titles(soup)  # {other_conts_no: title}
    recommended = list(recommend_titles.keys())

    return {
        "conts_no": conts_no,
        "title": title,
        "schedule_raw": schedule_raw,
        "upcoming_products": upcoming_products,
        "_recommended_conts_nos": recommended,
        "_recommend_titles": recommend_titles,
    }


def discover_all_programs(seed: str) -> list:
    """seed에서 출발해 추천 섹션을 타고 들어가며 BFS로 전체 프로그램을 발견한다.
    프로그램 자체 페이지에 텍스트 타이틀이 없는 경우가 많아(로고 이미지로만 표시),
    다른 페이지의 추천 섹션이 그 프로그램을 부른 이름으로 보완한다."""
    visited = set()
    queue = deque([seed])
    discovered_from = {}  # conts_no -> 발견된 출처 conts_no 집합
    title_hints = {}      # conts_no -> 다른 페이지에서 본 이름
    raw_results = {}      # conts_no -> crawl_program 결과

    while queue and len(visited) < MAX_PAGES:
        conts_no = queue.popleft()
        if conts_no in visited:
            continue
        visited.add(conts_no)

        print(f"  [LT] conts_no={conts_no} 수집 중...")
        try:
            program = crawl_program(conts_no)
        except Exception as e:
            print(f"    [실패] conts_no={conts_no}: {e}")
            continue

        raw_results[conts_no] = program
        recommend_titles = program.pop("_recommend_titles", {})
        recommended = program.pop("_recommended_conts_nos", [])

        for next_no, seen_title in recommend_titles.items():
            if next_no not in title_hints and seen_title:
                title_hints[next_no] = seen_title
            if next_no not in visited:
                discovered_from.setdefault(next_no, set()).add(conts_no)
                queue.append(next_no)

        time.sleep(REQUEST_DELAY_SEC)

    results = []
    for conts_no, program in raw_results.items():
        if not program["title"]:
            program["title"] = title_hints.get(conts_no, "")
        program["discovered_from"] = sorted(discovered_from.get(conts_no, []))
        results.append(program)

    return results


def main():
    seed = sys.argv[1] if len(sys.argv) > 1 else SEED_CONTS_NO
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"[LT] conts_no={seed} 에서 출발해 프로그램 그래프 탐색 시작...")
    programs = discover_all_programs(seed)

    payload = {
        "company": "LT",
        "collectedAt": datetime.now(KST).isoformat(),
        "seed_conts_no": seed,
        "programs": programs,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n총 {len(programs)}개 프로그램 발견, 저장: {OUTPUT_PATH}")
    for p in programs:
        print(f"  conts_no={p['conts_no']} - {p['title']} | {p['schedule_raw']} "
              f"(소개상품 {len(p['upcoming_products'])}개)")


if __name__ == "__main__":
    main()
