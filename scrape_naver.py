"""
scrape_naver.py
네이버 "방영중한국드라마" / "방영중한국예능" 검색 위젯을 수집해
주차별(월~일) JSON 스냅샷으로 저장한다.

사용법:
    python3 scrape_naver.py --out-dir ../data/dramavariety

동작:
  1. 드라마 위젯: 단순 페이지 로드만으로 1~2페이지 데이터가 DOM에 모두 포함되어
     있는 것이 확인됨 (li.info_box 셀렉터로 한 번에 전부 추출 가능).
  2. 예능 위젯: 페이지가 다수(확인 시점 기준 24p)이며 "다음" 버튼이 AJAX로
     동작하므로, Playwright로 다음 버튼을 끝까지 클릭하며 각 페이지의
     li.info_box를 누적 수집한다.
  3. 시청률 5% 이상만 필터링.
  4. 결과를 weekStart(해당 주 월요일, YYYY-MM-DD) 기준 JSON 파일로 저장.
     기존 파일이 있으면 동일 weekStart 내에서 program id로 병합(merge)한다.
"""
import argparse
import json
import os
import sys
import re
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(__file__))
from naver_parser import parse_cards_from_html, dedupe_programs

DRAMA_URL = (
    "https://search.naver.com/search.naver?where=nexearch&sm=tab_etc&qvt=0"
    "&query=%EB%B0%A9%EC%98%81%EC%A4%91%ED%95%9C%EA%B5%AD%EB%93%9C%EB%9D%BC%EB%A7%88"
)
VARIETY_URL = (
    "https://search.naver.com/search.naver?sm=tab_hty.top&where=nexearch&ssc=tab.nx.all"
    "&query=%EB%B0%A9%EC%98%81%EC%A4%91%ED%95%9C%EA%B5%AD%EC%98%88%EB%8A%A5"
)

MIN_RATING = 5.0
KST = timezone(timedelta(hours=9))


def monday_of(date_obj):
    return date_obj - timedelta(days=date_obj.weekday())


def fetch_drama(page):
    """드라마 위젯: 한 번 로드로 전체(보이는+숨김 ul.list_info) 카드 수집"""
    page.goto(DRAMA_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_selector("li.info_box", timeout=15000)
    html = page.content()
    programs = parse_cards_from_html(html, "drama", min_rating=MIN_RATING)
    return dedupe_programs(programs)


def fetch_variety(page, max_pages: int = 30):
    """예능 위젯: '다음' 버튼을 끝까지 클릭하며 각 페이지 카드 수집"""
    page.goto(VARIETY_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_selector("li.info_box", timeout=15000)

    all_programs = []
    seen_page_signatures = set()

    for page_num in range(1, max_pages + 1):
        html = page.content()
        programs = parse_cards_from_html(html, "variety", min_rating=MIN_RATING)
        all_programs.extend(programs)
        print(f"  [variety] page {page_num}: {len(programs)} programs >= {MIN_RATING}%")

        next_btn = page.query_selector("a.pg_next._next")
        if not next_btn:
            print("  [variety] no next button found, stopping")
            break
        aria_disabled = next_btn.get_attribute("aria-disabled")
        classes = next_btn.get_attribute("class") or ""
        if aria_disabled == "true" or "on" not in classes.split():
            print(f"  [variety] reached last page at page {page_num}")
            break

        next_btn.click()
        # 다음 페이지 카드가 갱신될 때까지 대기 (li.info_box 내용이 바뀔 시간 확보)
        page.wait_for_timeout(800)
        page.wait_for_selector("li.info_box", timeout=15000)

    return dedupe_programs(all_programs)


def merge_into_week_file(out_path: str, new_programs: list, week_start: str, week_end: str):
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            existing = json.load(f)
        existing_programs = existing.get("programs", [])
    else:
        existing_programs = []

    by_id = {p["id"]: p for p in existing_programs}
    for p in new_programs:
        by_id[p["id"]] = p  # 최신 수집 데이터로 덮어쓰기 (시청률 갱신 등)

    merged = {
        "weekStart": week_start,
        "weekEnd": week_end,
        "collectedAt": datetime.now(KST).isoformat(),
        "programs": list(by_id.values()),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="../data/dramavariety")
    parser.add_argument("--max-pages", type=int, default=30, help="예능 위젯 최대 순회 페이지 수 (안전장치)")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    today = datetime.now(KST).date()
    week_start_date = monday_of(today)
    week_end_date = week_start_date + timedelta(days=6)
    week_start = week_start_date.isoformat()
    week_end = week_end_date.isoformat()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            locale="ko-KR",
        )

        print("collecting drama...")
        drama_programs = fetch_drama(page)
        print(f"  drama total: {len(drama_programs)} programs >= {MIN_RATING}%")

        print("collecting variety...")
        variety_programs = fetch_variety(page, max_pages=args.max_pages)
        print(f"  variety total: {len(variety_programs)} programs >= {MIN_RATING}%")

        browser.close()

    all_programs = drama_programs + variety_programs
    out_path = os.path.join(args.out_dir, f"{week_start}.json")
    merged = merge_into_week_file(out_path, all_programs, week_start, week_end)

    print(f"saved {len(merged['programs'])} programs -> {out_path}")


if __name__ == "__main__":
    main()
