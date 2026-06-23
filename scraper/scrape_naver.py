"""
scrape_naver.py
네이버 "방영중한국드라마" / "방영중한국예능" 검색 위젯을 수집해
데이터의 실제 발생일(ratingDate)이 속한 주의 월요일 날짜('YYYY-MM-DD.json')를 추적하여
해당 날짜 파일에 정밀 누적(Merge) 적재한다. (독립 실행형 통합 버전)
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# 가장 안정적이고 깨끗한 네이버 PC 공식 검색 URL 표준 규격
DRAMA_URL = "https://search.naver.com/search.naver?where=nexearch&sm=top_hty&fbm=0&ie=utf8&query=%EB%B0%A9%EC%98%81%EC%A4%91%ED%95%9C%EA%B5%AD%EB%93%9C%EB%9D%BC%EB%A7%88"
VARIETY_URL = "https://search.naver.com/search.naver?where=nexearch&sm=top_hty&fbm=0&ie=utf8&query=%EB%B0%A9%EC%98%81%EC%A4%91%ED%95%9C%EA%B5%AD%EC%98%88%EB%8A%A5"

# 시청률 커트라인 기준 (드라마 5.0%, 예능 1.0%)
MIN_RATING_DRAMA = 5.0
MIN_RATING_VARIETY = 1.0
KST = timezone(timedelta(hours=9))

DAY_ORDER = ["월", "화", "수", "목", "금", "토", "일"]
DAY_INDEX = {d: i for i, d in enumerate(DAY_ORDER)}
DEBUG = False


def to_local_date_str(d):
    return f"{d.getFullYear()}-{String(d.getMonth() + 1).padStart(2, '0')}-{String(d.getDate()).padStart(2, '0')}"


def monday_of(date_obj):
    """기준 날짜가 속한 주의 월요일 날짜 객체를 반환합니다."""
    return date_obj - timedelta(days=date_obj.weekday())


# ==========================================
#              내장형 파서 로직
# ==========================================

def expand_days(day_token: str):
    days = []
    clean_token = day_token.replace(" ", "").strip()
    for part in [p.strip() for p in clean_token.split(",")]:
        if not part:
            continue
        if "~" in part:
            try:
                start, end = [p.strip() for p in part.split("~")]
                si, ei = DAY_INDEX[start], DAY_INDEX[end]
                days.extend(DAY_ORDER[si:ei + 1])
            except KeyError:
                continue
        else:
            if part in DAY_INDEX:
                days.append(part)
    return days


def parse_schedule_text(schedule_text: str):
    groups = re.findall(r'\(([^)]+)\)\s*((?:오전|오후)\s*\d{1,2}:\d{2})', schedule_text)
    results = []
    for day_token, time_token in groups:
        expanded = expand_days(day_token)
        if expanded:
            results.append({"days": expanded, "time": time_token.strip()})
    return results


def parse_card(li, category: str, base_url: str = ""):
    title_tag = li.select_one('strong.title a')
    if not title_tag:
        return []
    title = title_tag.get_text(strip=True)
    link = title_tag.get('href', '')
    if base_url and link:
        link = urljoin(base_url, link)

    info_txt = li.select_one('div.main_info span.info_txt')
    if not info_txt:
        return []
    broadcaster_tag = info_txt.select_one('a.broadcaster')
    channel = broadcaster_tag.get_text(strip=True) if broadcaster_tag else ""
    full = info_txt.get_text(strip=True)
    schedule_text = full.replace(channel, "", 1).strip()
    slots = parse_schedule_text(schedule_text)
    if not slots:
        return []

    sub_info = li.select_one('div.sub_info span.info_txt')
    rating = None
    rating_date = None
    if sub_info:
        num_txt = sub_info.select_one('span.num_txt')
        if num_txt:
            try:
                rating = float(num_txt.get_text(strip=True).replace('%', ''))
            except ValueError:
                rating = None
        m = re.search(r'\(([\d.]+)\)', sub_info.get_text(strip=True))
        if m:
            rating_date = m.group(1).rstrip('.')

    if rating is None:
        return []

    programs = []
    for slot in slots:
        programs.append({
            "id": f"{category}_{title}_{channel}_{'-'.join(slot['days'])}_{slot['time']}",
            "category": category,
            "channel": channel,
            "title": title,
            "days": slot["days"],
            "time": slot["time"],
            "rating": rating,
            "ratingDate": rating_date,
            "link": link,
        })
    return programs


def parse_cards_from_html(html: str, category: str, min_rating: float = 5.0, base_url: str = ""):
    soup = BeautifulSoup(html, 'lxml')
    results = []
    for li in soup.select('li.info_box'):
        for p in parse_card(li, category, base_url=base_url):
            if p["rating"] >= min_rating:
                results.append(p)
    return results


def dedupe_programs(programs: list):
    seen = set()
    out = []
    for p in programs:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        out.append(p)
    return out


# ==========================================
#            페이지 전환 감지 헬퍼
# ==========================================

VISIBLE_SIG_JS = """
    () => Array.from(document.querySelectorAll('li.info_box'))
        .filter(el => el.offsetParent !== null)
        .map(el => {
            const titleEl = el.querySelector('strong.title');
            return titleEl ? titleEl.innerText.trim() : '';
        })
        .filter(t => t.length > 0)
        .join('|')
"""

PAGING_TEXT_JS = """
    () => {
        const el = document.querySelector('.cm_paging_area._kgs_page')
            || document.querySelector('.cm_paging_area')
            || document.querySelector('[class*="paging"]');
        return el ? el.innerText.replace(/\\s+/g, ' ').trim() : null;
    }
"""


def visible_signature(page):
    return page.evaluate(VISIBLE_SIG_JS)


def read_paging_text(page):
    try:
        return page.evaluate(PAGING_TEXT_JS)
    except Exception:
        return None


def parse_current_total(paging_text):
    if not paging_text:
        return None, None
    m = re.search(r'현재\s*(\d+)\s*전체\s*(\d+)', paging_text)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def click_next_and_wait(page, before_paging_text, before_visible_sig, timeout_s=12):
    next_btn = page.query_selector("a.pg_next._next")
    if not next_btn:
        return False

    aria_disabled = next_btn.get_attribute("aria-disabled")
    classes = next_btn.get_attribute("class") or ""
    if aria_disabled == "true" or "on" not in classes.split():
        return False

    try:
        next_btn.scroll_into_view_if_needed(timeout=3000)
        page.wait_for_timeout(200)
        next_btn.click(timeout=5000)
    except Exception:
        try:
            next_btn.click(force=True, timeout=5000)
        except Exception:
            return False

    steps = int(timeout_s / 0.5)
    for _ in range(steps):
        page.wait_for_timeout(500)

        after_paging_text = read_paging_text(page)
        cur, tot = parse_current_total(after_paging_text)
        before_cur, before_tot = parse_current_total(before_paging_text)
        if cur is not None and before_cur is not None and cur != before_cur:
            return True

        after_visible_sig = visible_signature(page)
        if after_visible_sig and before_visible_sig and after_visible_sig != before_visible_sig:
            return True

    return False


def click_all_days_tab(page, category):
    try:
        target_selector = "div.cm_tap_area ul > li > a > span.menu._text"
        page.wait_for_selector(target_selector, timeout=10000)
        tabs = page.query_selector_all(target_selector)
        
        for tab in tabs:
            if tab.inner_text().strip() == "전체":
                tab.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(200)
                tab.click(force=True)
                print(f"  [{category}] 🚀 '전체' 요일 탭 하이재킹 성공!")
                page.wait_for_timeout(2000)
                return True
    except Exception as e:
        print(f"  [{category}] '전체' 탭 저격 실패 (기본 화면으로 계속 진행): {e}")
    return False


# ---------- 데이터 수집 함수 ----------

def fetch_drama(page):
    page.goto(DRAMA_URL, wait_until="networkidle", timeout=30000)
    click_all_days_tab(page, "drama")
    html = page.content()
    return parse_cards_from_html(html, "drama", min_rating=MIN_RATING_DRAMA, base_url=DRAMA_URL)


def fetch_variety(page, max_pages: int = 30):
    page.goto(VARIETY_URL, wait_until="networkidle", timeout=30000)
    click_all_days_tab(page, "variety")

    all_programs = []
    page_num = 1
    max_retries_per_page = 3

    while page_num <= max_pages:
        page.wait_for_timeout(800)
        paging_text = read_paging_text(page)
        cur, tot = parse_current_total(paging_text)
        html = page.content()

        programs = parse_cards_from_html(html, "variety", min_rating=MIN_RATING_VARIETY, base_url=VARIETY_URL)
        all_programs.extend(programs)

        print(f"  [variety] page {page_num} (네이버 표시: 현재{cur}/전체{tot}) 수집 중...")

        if cur is not None and tot is not None and cur >= tot:
            break

        before_paging_text = paging_text
        before_visible_sig = visible_signature(page)

        advanced = False
        for attempt in range(1, max_retries_per_page + 1):
            advanced = click_next_and_wait(page, before_paging_text, before_visible_sig)
            if advanced:
                break
            page.wait_for_timeout(1000)

        if not advanced:
            break
        page_num += 1

    return all_programs


# ---------- 🎯 핵심 수정: 'YYYY-MM-DD.json' 기준 누적 적재 분배 처리기 ----------

def dispatch_and_merge_by_matching_week(out_dir: str, programs: list):
    """
    수집된 각 프로그램들의 ratingDate를 계산하여, 
    기존 index.html이 요구하는 본연의 월요일 날짜('YYYY-MM-DD.json') 파일에 중복 없이 정밀 누적 머지합니다.
    """
    current_year = datetime.now(KST).year
    buckets = {}
    
    # 1. ratingDate 기반 해당 주차의 월요일(YYYY-MM-DD) 명칭 산출 및 버킷 분류
    for p in programs:
        r_date_str = p.get("ratingDate")
        if not r_date_str:
            target_monday = monday_of(datetime.now(KST).date())
        else:
            try:
                month, day = map(int, r_date_str.split('.'))
                actual_date = datetime(current_year, month, day).date()
                target_monday = monday_of(actual_date)
            except Exception:
                target_monday = monday_of(datetime.now(KST).date())
                
        file_name_key = target_monday.isoformat() # 'YYYY-MM-DD'
        if file_name_key not in buckets:
            buckets[file_name_key] = []
        buckets[file_name_key].append(p)

    # 2. 산출된 YYYY-MM-DD.json 파일별 복원 및 완전 누적 실행
    for file_date, p_list in buckets.items():
        file_path = os.path.join(out_dir, f"{file_date}.json")
        week_start_date = datetime.fromisoformat(file_date).date()
        week_end = (week_start_date + timedelta(days=6)).isoformat()
        
        if os.path.exists(file_path):
            try:
                with open(file_path, encoding="utf-8") as f:
                    existing_data = json.load(f)
                existing_programs = existing_data.get("programs", [])
            except Exception:
                existing_programs = []
        else:
            existing_programs = []
            
        # 고유 식별자(ID) 맵을 활용해 동일 프로그램 적재 시 완전 중복 제거 및 누적 보장
        by_id = {p["id"]: p for p in existing_programs}
        for p in p_list:
            by_id[p["id"]] = p
            
        merged_payload = {
            "weekStart": file_date,
            "weekEnd": week_end,
            "collectedAt": datetime.now(KST).isoformat(),
            "programs": dedupe_programs(list(by_id.values())),
        }
        
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(merged_payload, f, ensure_ascii=False, indent=2)
            
        print(f"  [Merge Success] {file_date}.json 파일에 {len(merged_payload['programs'])}개 데이터 누적 완료!")


def main():
    global DEBUG
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="../data/dramavariety")
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args()
    DEBUG = args.debug

    CURRENT_FILE_DIR = os.path.dirname(os.path.abspath(__file__))
    final_out_dir = os.path.isabs(args.out_dir) and args.out_dir or os.path.normpath(os.path.join(CURRENT_FILE_DIR, args.out_dir))
    os.makedirs(final_out_dir, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headful, args=["--disable-dev-shm-usage"])
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            locale="ko-KR"
        )
        page.set_default_timeout(25000)

        print("collecting drama...")
        drama_programs = fetch_drama(page)
        print(f"  drama raw count: {len(drama_programs)}")

        print("collecting variety...")
        variety_programs = fetch_variety(page, max_pages=args.max_pages)
        print(f"  variety raw count: {len(variety_programs)}")

        browser.close()

    all_raw_programs = drama_programs + variety_programs
    dispatch_and_merge_by_matching_week(final_out_dir, all_raw_programs)


if __name__ == "__main__":
    main()
