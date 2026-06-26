# -*- coding: utf-8 -*-
"""
gs_fixed_programs.py
GS SHOP 모바일 메인페이지의 "GS SHOP 대표 프로그램" 섹션에서
고정 편성 프로그램(진행자 쇼) 목록과, 각 프로그램이 다음 방송에서
소개할 상품(이미지+가격+바로가기 링크)까지 함께 수집한다.

== 수집 대상 ==
https://m.gsshop.com/index.gs 를 모바일 UA로 접속 후,
<section id="tv-sect-pgm2"> 안의 "GS SHOP 대표 프로그램" 슬라이드를 파싱.

각 슬라이드(프로그램 1개)에는 다음이 들어있음:
  - figure.ban-img > img        : 프로그램 대표 이미지
  - a.ban-link[href]             : 대표이미지를 눌렀을 때 가는 "프로그램 상세페이지" 링크
  - h3.ttl-lg                    : 프로그램명 (예: "소유진쇼")
  - sup.color-primary            : 편성 주기/요일/시각 텍스트 (예: "매주 금요일 저녁 8시 35분")
  - sub.desc-md                  : 한 줄 소개
  - article.prd-item (보통 2개)  : 다음 방송에서 소개될 상품 미리보기
      - figure.prd-img > img         : 상품 이미지
      - .prd-name                    : 상품명
      - .set-price strong            : 판매가
      - a.prd-link[href]             : 상품 "바로가기" 링크
      - .badge-txt.broadcast 등      : "6.26.(금) 20:35 방송" 같은 다음 방송 예정 표시 (없을 수도 있음)

PC(www.gsshop.com)에는 이 섹션이 노출되지 않아 모바일 페이지로 접근해야 함.
swiper 특성상 앞뒤로 동일 슬라이드가 중복 렌더링되므로
class="swiper-slide-duplicate"가 붙은 슬라이드는 제외하고 파싱한다.

이미지/링크는 상대경로로 오는 경우 urljoin으로 절대 URL로 변환한다.

== 출력 ==
homeshopping/fixed_programs/GS.json
{
  "company": "GS",
  "collectedAt": "2026-06-25T22:10:00+09:00",
  "programs": [
    {
      "title": "소유진쇼",
      "frequency": "매주",
      "day": "금",
      "time": "20:35",
      "schedule_raw": "매주 금요일 저녁 8시 35분",
      "desc": "금요일 밤 차원이 다른 쇼핑",
      "thumbnail": "https://...",       # 프로그램 대표 이미지
      "detail_link": "https://...",     # 대표이미지/타이틀 클릭 시 이동하는 상세페이지
      "upcoming_products": [
        {
          "name": "DREAME 포켓 초경량 올인원 드라이어 ...",
          "price": 259000,
          "image": "https://...",
          "link": "https://...",        # 상품 바로가기
          "broadcast_at": "6. 26.(금) 20:35 방송"
        },
        ...
      ]
    },
    ...
  ]
}

== 사용법 ==
  pip install requests beautifulsoup4
  python gs_fixed_programs.py
"""

import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))

GS_MOBILE_URL = "https://m.gsshop.com/index.gs"
OUTPUT_DIR = os.path.join("homeshopping", "fixed_programs")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "GS.json")

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

DAY_MAP = {
    "월요일": "월", "화요일": "화", "수요일": "수", "목요일": "목",
    "금요일": "금", "토요일": "토", "일요일": "일",
}


def parse_schedule_text(text: str) -> dict:
    """'매주 목요일 저녁 8시 45분' 같은 한글 편성 문구를 구조화한다."""
    result = {"frequency": "매주", "day": None, "time": None}

    if "격주" in text:
        result["frequency"] = "격주"

    for kr, abbr in DAY_MAP.items():
        if kr in text:
            result["day"] = abbr
            break

    m = re.search(r'(\d+)\s*시\s*(\d+)?\s*분?', text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        # "저녁"/"밤"이 붙고 12시간 표기면 오후로 보정. "오전"/"낮"은 그대로 둔다.
        if hour < 12 and ("저녁" in text or "밤" in text) and "오전" not in text:
            hour += 12
        result["time"] = f"{hour:02d}:{minute:02d}"

    return result


def parse_price(text: str):
    """'259,000' 같은 가격 텍스트를 정수로 변환. 실패하면 None."""
    if not text:
        return None
    cleaned = re.sub(r'[^\d]', '', text)
    return int(cleaned) if cleaned else None


def extract_upcoming_products(slide, base_url: str) -> list:
    """슬라이드 안에서 '다음 방송 소개 상품' 카드를 추출."""
    products = []
    for prd in slide.select("article.prd-item"):
        name_tag = prd.select_one(".prd-name")
        price_tag = prd.select_one(".set-price strong")
        img_tag = prd.select_one("figure.prd-img img")
        link_tag = prd.select_one("a.prd-link")
        broadcast_tag = prd.select_one(".badge-txt.broadcast, .desc-tag.broadcast")

        image = None
        if img_tag and img_tag.get("src"):
            image = urljoin(base_url, img_tag["src"])

        link = None
        if link_tag and link_tag.get("href"):
            link = urljoin(base_url, link_tag["href"])

        products.append({
            "name": name_tag.get_text(strip=True) if name_tag else None,
            "price": parse_price(price_tag.get_text(strip=True)) if price_tag else None,
            "image": image,
            "link": link,
            "broadcast_at": broadcast_tag.get_text(strip=True) if broadcast_tag else None,
        })
    return products


def fetch_gs_programs() -> list:
    headers = {"User-Agent": MOBILE_UA, "Referer": "https://m.gsshop.com/"}
    resp = requests.get(GS_MOBILE_URL, headers=headers, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    section = soup.find("section", id="tv-sect-pgm2")
    if not section:
        raise RuntimeError(
            "'GS SHOP 대표 프로그램' 섹션(id=tv-sect-pgm2)을 찾을 수 없음. "
            "페이지 구조가 바뀌었을 가능성이 있음."
        )

    slides = section.select(".swiper-slide")
    programs = []
    seen_titles = set()

    for slide in slides:
        if "swiper-slide-duplicate" in slide.get("class", []):
            continue  # swiper가 앞뒤로 복제해 둔 중복 슬라이드는 건너뜀

        title_tag = slide.select_one("h3.ttl-lg")
        if not title_tag:
            continue
        title = title_tag.get_text(strip=True)

        if title in seen_titles:
            continue  # 혹시 모를 추가 중복 방지
        seen_titles.add(title)

        sup_tag = slide.select_one("sup.color-primary")
        schedule_raw = sup_tag.get_text(strip=True) if sup_tag else ""
        schedule = parse_schedule_text(schedule_raw)

        sub_tag = slide.select_one("sub.desc-md")
        desc = sub_tag.get_text(" ", strip=True) if sub_tag else ""

        # 프로그램 대표 이미지 (figure.ban-img 안의 img)
        thumb_tag = slide.select_one("figure.ban-img img")
        thumbnail = None
        if thumb_tag and thumb_tag.get("src"):
            thumbnail = urljoin(GS_MOBILE_URL, thumb_tag["src"])

        # 대표이미지/타이틀을 눌렀을 때 가는 프로그램 상세페이지 링크
        detail_link_tag = slide.select_one("a.ban-link")
        detail_link = None
        if detail_link_tag and detail_link_tag.get("href"):
            detail_link = urljoin(GS_MOBILE_URL, detail_link_tag["href"])

        upcoming_products = extract_upcoming_products(slide, GS_MOBILE_URL)

        programs.append({
            "title": title,
            "frequency": schedule["frequency"],
            "day": schedule["day"],
            "time": schedule["time"],
            "schedule_raw": schedule_raw,
            "desc": desc,
            "thumbnail": thumbnail,
            "detail_link": detail_link,
            "upcoming_products": upcoming_products,
        })

    return programs


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("[GS] 고정 프로그램 수집 중...")
    try:
        programs = fetch_gs_programs()
    except Exception as e:
        print(f"  [실패] {e}")
        return

    payload = {
        "company": "GS",
        "collectedAt": datetime.now(KST).isoformat(),
        "programs": programs,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"  -> {len(programs)}개 프로그램 저장: {OUTPUT_PATH}")
    for p in programs:
        day_time = f"{p['day']} {p['time']}" if p['time'] else p['day']
        print(f"    [{p['frequency']}] {day_time} - {p['title']} (소개상품 {len(p['upcoming_products'])}개)")


if __name__ == "__main__":
    main()

