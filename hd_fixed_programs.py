# -*- coding: utf-8 -*-
"""
hd_fixed_programs.py
현대Hmall "현대홈쇼핑의 대표 프로그램" 섹션에서
고정 편성 프로그램(진행자 쇼) 목록과 다음 방송 소개 상품을 수집한다.

== 수집 대상 ==
https://www.hmall.com/md/dpa/searchSpexSectItem?sectId=3109281&dispTrtyNmCd=home_eventicon_2&dispOrdg=6
이 페이지는 Next.js로 렌더링되지만, 응답 HTML 안의
<script id="__NEXT_DATA__"> 태그에 풀 데이터가 JSON으로 박혀있어
별도 API 호출 없이 페이지 하나만 받으면 충분하다.

데이터 위치:
  __NEXT_DATA__.props.pageProps.data.holiInfo.pgmShowList
  각 원소가 프로그램 1개:
    - spexSectId   : 프로그램 고유ID (커뮤니티/상세페이지 링크에 사용)
    - spexSectNm   : 프로그램명 (예: "왕영은의 톡투게더")
    - brodTitl     : 정식 방송 타이틀 (예: "왕영은의 톡 투게더")
    - evntDesc     : 한 줄 소개 (#태그 형태)
    - dispImflNm   : 대표 이미지 경로 (image.hmall.com 기준 상대경로)
    - connUrl      : 프로그램 상세("커뮤니티") 페이지 링크
    - sectLbl      : 편성 텍스트 (예: "매주 토요일 08시 20분")
    - itemList     : 다음 방송에서 소개될 상품 목록 (보통 2개)
        - slitmCd      : 상품코드
        - slitmNm      : 상품명
        - sellPrc      : 판매가
        - orglImgNm    : 상품 이미지 파일명

"인기프로그램"(spexSectNm == "인기프로그램")은 개별 진행자 쇼가 아니라
여러 프로그램 상품을 모아 보여주는 모음 섹션이므로 기본적으로 제외한다.

== 출력 ==
homeshopping/fixed_programs/HD.json
{
  "company": "HD",
  "collectedAt": "2026-06-25T23:30:00+09:00",
  "programs": [
    {
      "title": "왕영은의 톡투게더",
      "broadcast_title": "왕영은의 톡 투게더",
      "schedule_raw": "매주 토요일 08시 20분",
      "day": "토",
      "time": "08:20",
      "desc": "#토요일 아침의 행복 #역시 왕톡이니까!",
      "thumbnail": "https://image.hmall.com/MH/HM005_SL/.../disp2683513150553.png",
      "detail_link": "https://www.hmall.com/md/dpa/pgmComm?sectId=2683513",
      "upcoming_products": [
        {
          "name": "[왕톡단독] 폴바셋 프리미엄 아이스크림 NEW 패키지 총 24개",
          "price": 69900,
          "image": "https://image.hmall.com/.../2236429018_0.jpg",
          "link": "https://www.hmall.com/md/pda/itemPtc?slitmCd=2236429018"
        },
        ...
      ]
    },
    ...
  ]
}

== 사용법 ==
  pip install requests
  python hd_fixed_programs.py
"""

import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

HD_PAGE_URL = (
    "https://www.hmall.com/md/dpa/searchSpexSectItem"
    "?sectId=3109281&dispTrtyNmCd=home_eventicon_2&dispOrdg=6"
)
IMAGE_BASE = "https://image.hmall.com/"
OUTPUT_DIR = os.path.join("homeshopping", "fixed_programs")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "HD.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

EXCLUDE_TITLES = {"인기프로그램"}  # 개별 진행자 쇼가 아닌 모음 섹션은 제외

DAY_MAP = {
    "월요일": "월", "화요일": "화", "수요일": "수", "목요일": "목",
    "금요일": "금", "토요일": "토", "일요일": "일",
}


def parse_schedule_text(text: str) -> dict:
    """'매주 토요일 08시 20분' 또는 '매주 수요일 19시 30분 | 토요일 11시 20분' 같은
    문구에서 첫 번째 요일/시각을 day/time으로, 전체를 schedule_raw로 보존한다.
    (요일이 여러 개인 경우는 schedule_raw에서 전체 패턴을 확인할 수 있다.)
    """
    result = {"day": None, "time": None}
    for kr, abbr in DAY_MAP.items():
        if kr in text:
            result["day"] = abbr
            break

    m = re.search(r'(\d{1,2})\s*시\s*(\d{1,2})?\s*분?', text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        result["time"] = f"{hour:02d}:{minute:02d}"

    return result


def to_image_url(path: str) -> str:
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return IMAGE_BASE + path.lstrip("/")


def extract_next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError(
            "__NEXT_DATA__ 스크립트 태그를 찾을 수 없음. "
            "페이지 구조가 바뀌었을 가능성이 있음."
        )
    return json.loads(m.group(1))


def fetch_hd_programs(include_popular: bool = False) -> list:
    headers = {"User-Agent": UA, "Referer": "https://www.hmall.com/"}
    resp = requests.get(HD_PAGE_URL, headers=headers, timeout=15)
    resp.raise_for_status()

    next_data = extract_next_data(resp.text)

    try:
        pgm_list = next_data["props"]["pageProps"]["data"]["holiInfo"]["pgmShowList"]
    except (KeyError, TypeError) as e:
        raise RuntimeError(f"pgmShowList 경로를 찾을 수 없음: {e}")

    programs = []
    for pgm in pgm_list:
        title = pgm.get("spexSectNm", "")
        if not include_popular and title in EXCLUDE_TITLES:
            continue

        schedule_raw = pgm.get("sectLbl", "") or ""
        schedule = parse_schedule_text(schedule_raw)

        upcoming_products = []
        for item in pgm.get("itemList", []) or []:
            slitm_cd = item.get("slitmCd", "")
            link = (
                f"https://www.hmall.com/md/pda/itemPtc?slitmCd={slitm_cd}"
                if slitm_cd else None
            )
            upcoming_products.append({
                "name": item.get("slitmNm"),
                "price": item.get("sellPrc"),
                "image": to_image_url(item.get("orglImgNm", "")),
                "link": link,
            })

        programs.append({
            "title": title,
            "broadcast_title": pgm.get("brodTitl"),
            "schedule_raw": schedule_raw,
            "day": schedule["day"],
            "time": schedule["time"],
            "desc": pgm.get("evntDesc", ""),
            "thumbnail": to_image_url(pgm.get("dispImflNm", "")),
            "detail_link": pgm.get("connUrl"),
            "upcoming_products": upcoming_products,
        })

    return programs


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("[HD] 고정 프로그램 수집 중...")
    try:
        programs = fetch_hd_programs()
    except Exception as e:
        print(f"  [실패] {e}")
        return

    payload = {
        "company": "HD",
        "collectedAt": datetime.now(KST).isoformat(),
        "programs": programs,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"  -> {len(programs)}개 프로그램 저장: {OUTPUT_PATH}")
    for p in programs:
        day_time = f"{p['day']} {p['time']}" if p['time'] else p['schedule_raw']
        print(f"    {day_time} - {p['title']} (소개상품 {len(p['upcoming_products'])}개)")


if __name__ == "__main__":
    main()
