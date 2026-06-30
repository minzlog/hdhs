"""
scrape_naver.py
네이버 "방영중한국드라마" / "방영예능" 검색 위젯 수집
- '전체' 탭 URL 강제 추출 및 다이렉트 접속 (클릭 씹힘 완벽 방어)
- 드라마/예능 모두 다중 페이지(페이징) 끝까지 수집
- 수집된 모든 데이터를 무조건 '이번 주' 파일에 덮어쓰기/누적
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, date as date_cls, timedelta, timezone
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

DRAMA_URL = "https://search.naver.com/search.naver?where=nexearch&sm=top_hty&fbm=0&ie=utf8&query=%EB%B0%A9%EC%98%81%EC%A4%91%ED%95%9C%EA%B5%AD%EB%93%9C%EB%9D%BC%EB%A7%88"
# 유저가 제공한 '방영예능' URL 적용 완료
VARIETY_URL = "https://search.naver.com/search.naver?sm=tab_hty.top&where=nexearch&ssc=tab.nx.all&query=%EB%B0%A9%EC%98%81%EC%98%88%EB%8A%A5&oquery=&tqi=jBq1rlqpvCwssOj2YZG-498675&ackey=kwoadl9c"

MIN_RATING_DRAMA = 5.0
MIN_RATING_VARIETY = 1.0
KST = timezone(timedelta(hours=9))

DAY_ORDER = ["월", "화", "수", "목", "금", "토", "일"]
DAY_INDEX = {d: i for i, d in enumerate(DAY_ORDER)}
DEBUG = False


def monday_of(date_obj):
    return date_obj - timedelta(days=date_obj.weekday())


def resolve_rating_date(rating_date_str: str, today):
    """네이버가 주는 ratingDate("6.27" 형태, 연도 없음)를 실제 date로 추정한다.
    오늘(today) 기준으로 같은 해/작년 두 후보를 만들어, 오늘보다 미래가 아니면서
    가장 가까운(=가장 최근) 과거 날짜를 채택한다. 연말연초 경계(예: 1월에 12월
    데이터가 들어오는 경우)를 안전하게 처리하기 위함이다.
    파싱에 실패하면 None을 반환하고, 호출 쪽에서 오늘 날짜로 폴백한다."""
    if not rating_date_str:
        return None
    m = re.match(r'^(\d{1,2})\.(\d{1,2})$', rating_date_str.strip())
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))

    candidates = []
    for year in (today.year, today.year - 1):
        try:
            candidates.append(date_cls(year, month, day))
        except ValueError:
            continue

    # 오늘보다 미래인 후보는 제외 (시청률은 과거 방송 기준이므로 미래일 수 없음)
    past_candidates = [d for d in candidates if d <= today]
    if not past_candidates:
        # 전부 미래로 계산되면(예: 시계 오차) 그래도 가장 가까운 후보를 채택
        if not candidates:
            return None
        return min(candidates, key=lambda d: abs((d - today).days))

    return max(past_candidates)


# ==========================================
#              파서 및 병합 로직
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
        # 식별자(ID)에는 title을 포함한다. 채널+시간대만으로는 같은
        # 채널/시간에 요일마다 전혀 다른 프로그램이 편성되는 경우를
        # 구분할 수 없다(예: KBS2 오후 10시는 요일마다 다른 예능).
        # 말줄임("...")으로 갈리는 제목 중복 문제는 아래 dedupe 단계에서
        # 같은 (category, channel, days, time) 그룹 내 제목을 정규화해
        # 별도로 처리한다.
        programs.append({
            "id": f"{category}_{title}_{channel}_{slot['time']}",
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


def normalize_truncated_titles(programs: list):
    """네이버 위젯은 카드 레이아웃 상태에 따라 같은 프로그램의 제목을
    풀텍스트로 줄 때와, CSS 말줄임으로 끝을 "..."으로 잘라서 줄 때가
    섞여 있다(예: "콩콩팜팜 (...동물농장)" vs "콩콩팜팜 (...동...").
    title이 id에 포함되어 있어 이 차이만으로 같은 편성이 두 건으로
    갈라져 중복 표시되는 문제가 있었으므로, 같은 (category, channel,
    days, time) 조합 안에서는 말줄임 제목을 그 그룹의 가장 긴(풀)
    제목으로 통일한다."""
    groups = {}
    for p in programs:
        key = (p["category"], p["channel"], tuple(p["days"]), p["time"])
        groups.setdefault(key, []).append(p)

    for key, group in groups.items():
        if len(group) <= 1:
            continue
        titles = [p["title"] for p in group]
        # "..."으로 끝나는(말줄임된) 제목들을 후보에서 제외하고,
        # 남은 것 중 가장 긴 제목을 그 그룹의 대표 제목으로 삼는다.
        full_candidates = [t for t in titles if not t.endswith("...")]
        if not full_candidates:
            continue
        canonical = max(full_candidates, key=len)
        # 말줄임 제목이 대표 제목의 접두사일 때만(=정말 같은 프로그램이
        # 잘려서 생긴 텍스트일 때만) 치환한다. 우연히 같은 시간/채널에
        # 편성된 서로 다른 프로그램까지 잘못 합치지 않기 위한 방어.
        truncated_prefix_len = len(canonical) - 3
        for p in group:
            t = p["title"]
            if t == canonical or not t.endswith("..."):
                continue
            if truncated_prefix_len > 0 and canonical.startswith(t[:-3]):
                p["title"] = canonical


def dedupe_programs(programs: list):
    """동일한 프로그램의 쪼개진 요일 카드들을 하나로 합칩니다.

    주의: 네이버 위젯이 페이징 도중 같은 (category,title,channel,time) 조합의
    카드를 ratingDate가 다른 채로 중복 노출하는 경우가 있다(예: 갱신 중인
    스냅샷 차이). 단순히 "먼저 만난 카드"를 채택하면 더 오래된 ratingDate가
    살아남아 최신 시청률이 영구히 반영되지 않는 문제가 생긴다. 그래서 같은
    id가 다시 나타나면 ratingDate를 비교해 더 최신(과거가 아닌) 쪽을 채택한다."""
    normalize_truncated_titles(programs)
    # 제목을 정규화했으므로 id도 그에 맞춰 다시 계산한다.
    for p in programs:
        p["id"] = f"{p['category']}_{p['title']}_{p['channel']}_{p['time']}"

    today = datetime.now(KST).date()
    merged = {}
    for p in programs:
        key = p["id"]
        if key not in merged:
            merged[key] = p
            continue

        existing_days = set(merged[key]["days"])
        existing_days.update(p["days"])
        merged[key]["days"] = [d for d in DAY_ORDER if d in existing_days]

        # ratingDate가 더 최신인 카드로 rating/ratingDate를 교체한다.
        existing_resolved = resolve_rating_date(merged[key].get("ratingDate"), today)
        new_resolved = resolve_rating_date(p.get("ratingDate"), today)
        if new_resolved and (existing_resolved is None or new_resolved > existing_resolved):
            merged[key]["rating"] = p["rating"]
            merged[key]["ratingDate"] = p["ratingDate"]

    return list(merged.values())


def parse_cards_from_html(html: str, category: str, min_rating: float = 5.0, base_url: str = ""):
    soup = BeautifulSoup(html, 'lxml')
    results = []
    for li in soup.select('li.info_box'):
        for p in parse_card(li, category, base_url=base_url):
            if p["rating"] >= min_rating:
                results.append(p)
    return results


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
        next_btn.evaluate("node => node.click()")
    except Exception:
        return False

    before_cur, before_tot = parse_current_total(before_paging_text)

    steps = int(timeout_s / 0.5)
    for _ in range(steps):
        page.wait_for_timeout(500)
        after_paging_text = read_paging_text(page)
        cur, tot = parse_current_total(after_paging_text)

        # 페이지 번호(현재/전체)를 읽을 수 있으면 이걸 최우선 판정 기준으로 삼습니다.
        # 시그니처(카드 제목 목록)는 일부만 갱신된 과渡 상태에서도 "달라짐"으로
        # 오판해 페이지 전환이 끝나기 전에 HTML을 읽어버리는 원인이었습니다.
        if before_cur is not None:
            if cur is not None and cur != before_cur:
                # 전환 확인 후에도 잠깐 더 기다려 렌더링이 끝난 뒤 읽도록 합니다.
                page.wait_for_timeout(400)
                return True
            # 숫자를 신뢰할 수 있는 상황이면 시그니처만으로는 전환 완료로 보지 않습니다.
            continue

        after_visible_sig = visible_signature(page)
        if after_visible_sig and before_visible_sig and after_visible_sig != before_visible_sig:
            page.wait_for_timeout(400)
            return True
    return False


def click_all_days_tab(page, category):
    """
    파이썬(Playwright)의 마우스 클릭을 쓰지 않고, 
    브라우저 내부 JS로 직접 침투해 숨김 요소 에러(Not visible)를 원천 차단합니다.
    """
    try:
        page.wait_for_selector(".cm_tap_area", timeout=10000)

        js_code = """
            () => {
                const links = Array.from(document.querySelectorAll('.cm_tap_area ul li a'));
                for (const a of links) {
                    const text = a.innerText || a.textContent || '';
                    if (text.trim().includes('전체')) {
                        const href = a.getAttribute('href');
                        if (href && href !== '#' && href.trim() !== '') {
                            return { type: 'url', value: href };
                        }
                        a.click();
                        return { type: 'click', value: 'clicked' };
                    }
                }
                return null;
            }
        """
        result = page.evaluate(js_code)

        if result:
            if result['type'] == 'url':
                target_url = urljoin(page.url, result['value'])
                print(f"  [{category}] 🚀 '전체' 탭 주소 강제 추출 성공! 다이렉트 접속합니다.")
                safe_goto(page, target_url)
                page.wait_for_timeout(2000)
                return True
            elif result['type'] == 'click':
                print(f"  [{category}] 🚀 '전체' 탭 JS 강제 클릭 성공! (숨김 요소 무시)")
                page.wait_for_timeout(2500)
                return True
        else:
            print(f"  [{category}] '전체' 탭을 찾을 수 없습니다. (기본 화면 진행)")

    except Exception as e:
        print(f"  [{category}] '전체' 탭 이동 중 예외 발생: {e}")
    return False


# ---------- 네비게이션 헬퍼 (networkidle 타임아웃 방어) ----------

def safe_goto(page, url: str, retries: int = 3, timeout: int = 30000):
    """page.goto를 안전하게 수행한다.

    'networkidle'은 네이버 검색 페이지처럼 백그라운드에서 분석/광고 요청이
    계속 발생하는 페이지에서는 네트워크가 절대 idle 상태로 떨어지지 않아
    타임아웃이 자주 발생한다(실제로는 페이지가 정상 렌더링됐어도). 그래서
    'domcontentloaded'로 빠르게 진입한 뒤, 실제 콘텐츠(.cm_tap_area)가
    뜨는지를 명시적으로 기다리는 방식으로 바꾼다. 그래도 실패하면(러너
    IP 일시 차단/네트워크 불안정 등 진짜 장애) 잠깐 쉬었다가 재시도한다."""
    from playwright.sync_api import TimeoutError as PWTimeoutError

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            try:
                page.wait_for_selector(".cm_tap_area", timeout=15000)
            except PWTimeoutError:
                # 탭 영역이 끝내 안 뜨면 페이지 구조가 다르거나 비정상일 수
                # 있으니, 재시도 루프가 다시 판단하도록 예외를 올린다.
                raise
            return
        except PWTimeoutError as e:
            last_err = e
            print(f"  [safe_goto] 시도 {attempt}/{retries} 실패: {e}")
            if attempt < retries:
                page.wait_for_timeout(5000)
    raise last_err


# ---------- 데이터 수집 함수 ----------

def fetch_drama(page, max_pages: int = 30):
    safe_goto(page, DRAMA_URL)
    click_all_days_tab(page, "drama")

    all_programs = []
    page_num = 1
    max_retries_per_page = 3

    while page_num <= max_pages:
        page.wait_for_timeout(800)
        paging_text = read_paging_text(page)
        cur, tot = parse_current_total(paging_text)
        html = page.content()

        programs = parse_cards_from_html(html, "drama", min_rating=MIN_RATING_DRAMA, base_url=DRAMA_URL)
        all_programs.extend(programs)

        if cur is not None and tot is not None:
            print(f"  [drama] page {page_num} (네이버 표시: 현재{cur}/전체{tot}) 수집 중...")
        else:
            print(f"  [drama] page {page_num} 수집 중...")

        if cur is not None and tot is not None and cur >= tot:
            break

        if cur is None and tot is None:
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

    return dedupe_programs(all_programs)


def fetch_variety(page, max_pages: int = 30):
    safe_goto(page, VARIETY_URL)
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

        if cur is not None and tot is not None:
            print(f"  [variety] page {page_num} (네이버 표시: 현재{cur}/전체{tot}) 수집 중...")
        else:
            print(f"  [variety] page {page_num} 수집 중...")

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

    return dedupe_programs(all_programs)


# ---------- 저장 로직 (ratingDate 기준으로 해당 주차 파일에 분배) ----------

def _validate_week_membership(monday_date, programs, today):
    """programs 중 ratingDate가 monday_date 주차(월~일) 범위 밖인 항목을 걸러낸다.
    weekStart/weekEnd와 안 맞는 program은 잘못된 경로(과거 로직의 잔존 오염,
    수동 편집 실수 등)로 그 파일에 끼어든 것이므로 보관하지 않는다.
    반환값: (정상 program 리스트, 제외된 program 리스트)"""
    week_start = monday_date
    week_end = monday_date + timedelta(days=6)

    valid, invalid = [], []
    for p in programs:
        resolved = resolve_rating_date(p.get("ratingDate"), today)
        if resolved is None or (week_start <= resolved <= week_end):
            # ratingDate가 없거나 파싱 불가하면 보수적으로 그대로 유지
            # (잘못 지우는 것보다 안전하게 두는 쪽을 택함)
            valid.append(p)
        else:
            invalid.append(p)
    return valid, invalid


def _merge_programs_into_file(out_dir: str, monday_date, programs: list):
    """programs를 monday_date가 속한 주차 파일에 머지 저장한다.
    (기존 dispatch_to_current_week의 병합 로직을 그대로 사용, 대상 주차만 인자로 받음)"""
    file_date = monday_date.isoformat()
    week_end = (monday_date + timedelta(days=6)).isoformat()
    file_path = os.path.join(out_dir, f"{file_date}.json")
    today = datetime.now(KST).date()

    if os.path.exists(file_path):
        try:
            with open(file_path, encoding="utf-8") as f:
                existing_data = json.load(f)
            existing_programs = existing_data.get("programs", [])
        except Exception:
            existing_programs = []
    else:
        existing_programs = []

    # 정합성 체크: 기존에 저장돼있던 program 중 이 주차(weekStart~weekEnd)에
    # 속하지 않는 ratingDate를 가진 게 있으면 제거한다. 과거 로직(오늘 날짜
    # 기준으로 무조건 같은 파일에 쓰던 시절)의 잔존 오염이나, 다른 경로로
    # 잘못 들어온 데이터가 영구히 박혀있는 것을 막기 위함이다.
    existing_programs, contaminated = _validate_week_membership(monday_date, existing_programs, today)
    for p in contaminated:
        print(f"  [정합성 정리] {file_date}.json 에서 '{p['title']}'"
              f" ({p['channel']}, ratingDate={p.get('ratingDate')}) 제거 — 이 주차 범위 밖의 날짜")

    by_id = {p["id"]: p for p in existing_programs}
    for p in programs:
        if p["id"] in by_id:
            existing_days = set(by_id[p["id"]]["days"])
            existing_days.update(p["days"])
            p["days"] = [d for d in DAY_ORDER if d in existing_days]
        by_id[p["id"]] = p

    # 기존에 누적 저장된 데이터(과거 회차에 말줄임으로 박혀있을 수 있음)와
    # 이번에 새로 수집한 데이터를 합친 전체 집합에 대해 다시 한 번
    # 말줄임 정규화 + id 재계산을 적용해, 주 단위로 쌓이는 과정에서도
    # 같은 프로그램이 풀제목/말줄임제목으로 갈려 중복되지 않게 한다.
    all_merged = list(by_id.values())
    normalize_truncated_titles(all_merged)
    by_id = {}
    for p in all_merged:
        p["id"] = f"{p['category']}_{p['title']}_{p['channel']}_{p['time']}"
        if p["id"] in by_id:
            existing_days = set(by_id[p["id"]]["days"])
            existing_days.update(p["days"])
            p["days"] = [d for d in DAY_ORDER if d in existing_days]
        by_id[p["id"]] = p

    merged_payload = {
        "weekStart": file_date,
        "weekEnd": week_end,
        "collectedAt": datetime.now(KST).isoformat(),
        "programs": list(by_id.values()),
    }

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(merged_payload, f, ensure_ascii=False, indent=2)

    print(f"  [Merge Success] {file_date}.json 에 {len(merged_payload['programs'])}개 데이터 안착 완료!")


def dispatch_by_rating_date(out_dir: str, programs: list):
    """각 program을 ratingDate(실제 방송/시청률 측정 날짜) 기준으로
    소속 주차를 계산해 그 주차 파일에 분배 저장한다.

    배경: 토/일(주말) 시청률은 네이버 집계가 며칠 늦게 올라온다. 기존에는
    스크래핑 '실행 시점'(오늘)의 주차 파일에 무조건 몰아넣었기 때문에,
    예를 들어 6/29(월)에 수집된 6/27,28(토,일) 데이터가 6/22 주차가 아니라
    6/29 주차 파일로 잘못 들어가서:
      - 6/22 주차 파일은 토일 데이터가 영구히 비어 보임(직전 주 값이 잔류)
      - 6/29 주차 파일에는 아직 끝나지 않은 이번 주에 지난주 토일 데이터가 섞임
    이 문제가 있었다.

    수정 후에는 ratingDate를 파싱해 실제 방송 날짜를 구하고, 그 날짜가 속한
    주의 월요일 파일에 정확히 귀속시킨다. ratingDate가 없거나 파싱 실패하면
    안전하게 '오늘' 기준 주차로 폴백한다.
    """
    today = datetime.now(KST).date()
    today_monday = monday_of(today)

    buckets = {}  # monday_date -> [program, ...]
    for p in programs:
        resolved = resolve_rating_date(p.get("ratingDate"), today)
        target_monday = monday_of(resolved) if resolved else today_monday
        buckets.setdefault(target_monday, []).append(p)

    for target_monday, bucket_programs in sorted(buckets.items()):
        tag = "이번 주" if target_monday == today_monday else "과거 주차(소급 반영)"
        print(f"  [{tag}] {target_monday.isoformat()} 주차에 {len(bucket_programs)}개 분배")
        _merge_programs_into_file(out_dir, target_monday, bucket_programs)




def prune_dropped_programs(out_dir: str, collected_ids: set, today):
    """이번 스크래핑에서 더 이상 안 보이는(=시청률이 컷오프 미달로 떨어진)
    프로그램을 '직전 주차' 파일에서만 찾아 제거한다.

    범위를 직전 주 하나로 한정하는 이유:
    - 이번 주(진행 중인 주)는 아직 데이터가 다 안 모인 상태라 대상에서 뺀다
      (괜히 진행 중인 주 데이터를 흔들 위험을 없앤다).
    - 그보다 오래된 주차는 네이버 위젯이 시간이 지나 더 이상 보여주지 않는
      게 정상이라("방영종료" 탭으로 넘어갔거나 화면에서 자연스럽게 빠짐),
      그런 정상적인 경우까지 "컷오프로 사라졌다"고 착각해서 지우면 과거
      데이터가 통째로 날아간다.
    토/일 시청률이 막 갱신되는 시점인 '바로 직전 주'만 이 정리 대상으로
    삼아야 안전하다."""
    today_monday = monday_of(today)
    target_monday = today_monday - timedelta(days=7)

    file_path = os.path.join(out_dir, f"{target_monday.isoformat()}.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    existing_programs = data.get("programs", [])
    kept = [p for p in existing_programs if p["id"] in collected_ids]
    dropped = [p for p in existing_programs if p["id"] not in collected_ids]

    if not dropped:
        return

    for p in dropped:
        print(f"  [컷오프 제거] {target_monday.isoformat()} 주차에서 '{p['title']}'"
              f" ({p['channel']}, {p.get('ratingDate')}, {p['rating']}%) 제거 — 이번 수집에서 더 이상 확인되지 않음")

    data["programs"] = kept
    data["collectedAt"] = datetime.now(KST).isoformat()
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
        drama_programs = fetch_drama(page, max_pages=args.max_pages)

        print("collecting variety...")
        variety_programs = fetch_variety(page, max_pages=args.max_pages)

        browser.close()

    all_raw_programs = drama_programs + variety_programs
    dispatch_by_rating_date(final_out_dir, all_raw_programs)

    # 이번에 수집된 전체 program id 집합을 기준으로, 직전 주차 파일에서
    # 컷오프 미달로 더 이상 안 보이는 항목을 정리한다.
    collected_ids = {p["id"] for p in all_raw_programs}
    today = datetime.now(KST).date()
    prune_dropped_programs(final_out_dir, collected_ids, today)


if __name__ == "__main__":
    main()
