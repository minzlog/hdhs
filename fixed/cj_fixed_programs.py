# -*- coding: utf-8 -*-
"""
cj_fixed_programs.py
CJ온스타일 모바일 메인페이지가 내부적으로 호출하는 모듈 콘텐츠 API
(newContList)를 직접 호출해서, "CJ온스타일 대표프로그램"(DMTV03 모듈)
13개 슬롯 전체를 pgmCd/편성텍스트/썸네일/소개상품까지 한 번에 수집한다.

(업데이트 - 2026-06, 전면 재작성)
이전 버전들은 메인페이지 마크업(.pgm_tab_section)을 정적 requests로
읽거나, Playwright로 13개 슬롯을 하나씩 클릭하며 펼친 패널을 긁고,
그래도 pgmCd를 모르니 별도 schedule API(최근 14일치)에서 모은
(요일,시각) 출현 패턴과 대조해 추론하는 식으로 동작했다.

실제로 Playwright network 캡처로 확인한 결과, 메인페이지가 로딩 시
호출하는 다음 API 한 번이 13개 슬롯 데이터를 전부(pgmCd 포함) 담고
있다는 게 확인되어, 이 방식으로 완전히 단순화한다:

  GET https://display.cjonstyle.com/c/rest/module/newContList
      ?id=004389&cjEmpYn=false&type=H&pmType=M&employeeDiscountRate=0

  result.moduleContApiTupleList[] 중
  cateModuleApiTuple.dpModuleCd == "DMTV03" ("기획PGM 게이트" = 대표프로그램)
  인 모듈의 cateContApiTupleList[] 가 슬롯 13개:
    - pgmCd       : 채널 고정 프로그램 코드 (더 이상 schedule API로 추론할 필요 없음)
    - pgmNm / contVal : 프로그램명
    - bdTxtDtm    : 편성 텍스트 (예: "목20:45 / 토10:20")
    - contImgFileUrl1/2/3 : 썸네일 이미지 3종 (1=배경, 2=배경(큰size), 3=프로그램 썸네일)
    - pgmLinkUrl  : https://display.cjonstyle.com/m/pgmShop/{pgmCd}
    - bnrLinkUrl  : 배너 클릭 링크
    - subCateContApiTupleList[] : 다음 방송 소개상품 (보통 2개)
        - itemInfo.itemPriceInfo.displayItemName / salePrice
        - itemInfo.itemImgUrl / itemLinkUrl

id=004389는 "TV" 홈탭의 hmtabMenuId. 쿠키/로그인 없이 호출 가능함을
확인했다 (User-Agent만 모바일로 지정하면 충분).

newContList 응답 자체에는 dpModuleCd가 "DMTV03" 하나만 있다고 보장되진
않으므로(운영 중 메인 구성이 바뀌면 모듈이 빠지거나 이름이 바뀔 수 있음)
모듈을 못 찾으면 빈 리스트를 반환하고 경고만 남긴다
(daily-scrape.yml에 continue-on-error가 걸려있어 전체 파이프라인은 안전).

== 출력 ==
homeshopping/fixed_programs/CJ.json
{
  "company": "CJ",
  "collectedAt": "...",
  "programs": [
    {
      "pgm_cd": "563049",
      "title": "동가게",
      "schedule_raw": "목20:45 / 토10:20",
      "thumbnail": "https://image.cjonstyle.net/.../659ff4....png",
      "pgmshop_link": "https://display.cjonstyle.com/m/pgmShop/563049",
      "upcoming_products": [
        {
          "name": "[동가게 PICK] 리파니 파비올라 숄더백",
          "price": 369000,
          "image": "https://itemimage.cjonstyle.net/...jpg",
          "link": "https://display.cjonstyle.com/m/item/2084733780?channelCode=30002002"
        },
        ...
      ]
    },
    ...
  ]
}

== 사용법 ==
  pip install requests
  python cj_fixed_programs.py
"""

import os
import re
import json
import requests
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

NEW_CONT_LIST_URL = "https://display.cjonstyle.com/c/rest/module/newContList"
NEW_CONT_LIST_PARAMS = {
    "id": "004389",       # TV 홈탭 hmtabMenuId
    "cjEmpYn": "false",
    "type": "H",
    "pmType": "M",
    "employeeDiscountRate": "0",
}
MAIN_PAGE_URL = "https://display.cjonstyle.com/m/homeTab/main?hmtabMenuId=004389"

# "대표프로그램" 모듈을 식별하는 코드. 메인 구성이 바뀌면 이 목록에
# 추가하면 된다 (예전 dpDesc 텍스트는 "■  기획PGM 게이트"였음).
TARGET_MODULE_CODES = {"DMTV03"}

OUTPUT_DIR = os.path.join("homeshopping", "fixed_programs")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "CJ.json")

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
)
REQUEST_TIMEOUT_SEC = 15


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": MOBILE_UA,
        "Referer": MAIN_PAGE_URL,
        "Accept": "application/json, text/plain, */*",
    })
    return session


def to_https(url: str) -> str:
    """'//image.cjonstyle.net/...' 같은 프로토콜 상대 URL을 https로 보정."""
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def parse_price(value) -> int:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    cleaned = re.sub(r'[^\d]', '', str(value))
    return int(cleaned) if cleaned else None


def fetch_new_cont_list(session: requests.Session) -> dict:
    resp = session.get(NEW_CONT_LIST_URL, params=NEW_CONT_LIST_PARAMS, timeout=REQUEST_TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json()


def extract_upcoming_products(sub_list) -> list:
    products = []
    for sub in sub_list or []:
        item_info = sub.get("itemInfo") or {}
        price_info = item_info.get("itemPriceInfo") or {}
        name = price_info.get("displayItemName") or price_info.get("itemName")
        if not name:
            continue
        products.append({
            "name": name,
            "price": parse_price(price_info.get("salePrice") or price_info.get("customerPrice")),
            "image": to_https(item_info.get("itemImgUrl")),
            "link": item_info.get("itemLinkUrl"),
        })
    return products


def extract_fixed_programs(new_cont_list_json: dict) -> list:
    programs = []

    module_tuples = (
        new_cont_list_json.get("result", {}).get("moduleContApiTupleList", []) or []
    )

    target_modules = [
        t for t in module_tuples
        if (t.get("cateModuleApiTuple", {}) or {}).get("dpModuleCd") in TARGET_MODULE_CODES
    ]

    if not target_modules:
        available = sorted({
            (t.get("cateModuleApiTuple", {}) or {}).get("dpModuleCd")
            for t in module_tuples
        })
        print(f"  [CJ] [경고] 대표프로그램 모듈({TARGET_MODULE_CODES})을 찾지 못했습니다. "
              f"현재 응답에 있는 모듈 코드: {available}")
        return programs

    for module in target_modules:
        for cont in module.get("cateContApiTupleList", []) or []:
            pgm_cd = cont.get("pgmCd")
            title = (cont.get("pgmNm") or cont.get("contVal") or "").strip()
            if not pgm_cd or not title:
                continue

            schedule_raw = (cont.get("bdTxtDtm") or "").strip()

            # contImgFileUrl3 가 보통 프로그램 카드용 작은 썸네일, 1이 배너형 배경.
            thumbnail = to_https(
                cont.get("contImgFileUrl3") or cont.get("contImgFileUrl1") or ""
            )

            pgmshop_link = cont.get("pgmLinkUrl") or f"https://display.cjonstyle.com/m/pgmShop/{pgm_cd}"

            upcoming_products = extract_upcoming_products(cont.get("subCateContApiTupleList"))

            programs.append({
                "pgm_cd": pgm_cd,
                "title": title,
                "schedule_raw": schedule_raw,
                "thumbnail": thumbnail,
                "pgmshop_link": pgmshop_link,
                "upcoming_products": upcoming_products,
            })

    return programs


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("[CJ] newContList API에서 대표프로그램 13개 슬롯 수집 중...")
    session = make_session()

    try:
        data = fetch_new_cont_list(session)
    except Exception as e:
        print(f"  [실패] newContList 호출: {e}")
        data = {}

    programs = extract_fixed_programs(data)

    payload = {
        "company": "CJ",
        "collectedAt": datetime.now(KST).isoformat(),
        "programs": programs,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n총 {len(programs)}개 프로그램 발견, 저장: {OUTPUT_PATH}")
    for p in programs:
        print(f"  pgm_cd={p['pgm_cd']} - {p['title']} | {p['schedule_raw']} "
              f"(소개상품 {len(p['upcoming_products'])}개)")


if __name__ == "__main__":
    main()
