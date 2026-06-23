"""
naver_parser.py
방영드라마 / 방영예능 네이버 검색 위젯 HTML 파서 (공용 모듈)

검증된 마크업 구조 (2026-06 기준):
  li.info_box
    strong.title > a            -> 제목, 링크
    div.main_info span.info_txt
      a.broadcaster             -> 채널
      (나머지 텍스트)           -> "(요일) 시간" 반복
    div.sub_info span.info_txt
      span.num_txt              -> 시청률(%)
      (나머지 텍스트)           -> "닐슨코리아 (MM.DD.)" 형태의 집계일

주의:
  - ul.list_info 가 여러 개 존재하며, 그중 하나는 style="display:none"으로
    숨겨진 상태로 같이 내려온다 (드라마 위젯에서 확인됨). 따라서 페이지
    전체에서 li.info_box 를 셀렉터로 직접 긁으면 보이는/숨김 여부와
    무관하게 전부 잡힌다.
  - 예능 위젯은 페이지가 다수(확인 시점 기준 24p)이며, "다음" 버튼이
    href="#" + onclick(JS)로 동작하는 AJAX 페이징이라 requests만으로는
    2페이지 이상을 가져올 수 없다. Playwright 등으로 클릭하며 순회 필요.
"""
import re
from bs4 import BeautifulSoup

DAY_ORDER = ["월", "화", "수", "목", "금", "토", "일"]
DAY_INDEX = {d: i for i, d in enumerate(DAY_ORDER)}


def expand_days(day_token: str):
    """'월~목' -> ['월','화','수','목'] / '월, 화' -> ['월','화'] / '월~수, 금' -> ['월','화','수','금']"""
    days = []
    for part in [p.strip() for p in day_token.split(",")]:
        if not part:
            continue
        if "~" in part:
            start, end = [p.strip() for p in part.split("~")]
            si, ei = DAY_INDEX[start], DAY_INDEX[end]
            days.extend(DAY_ORDER[si:ei + 1])
        else:
            days.append(part)
    return days


def parse_schedule_text(schedule_text: str):
    """'(월, 화) 오후 08:50' 등을 [{'days':[...], 'time': '...'}] 리스트로 변환"""
    groups = re.findall(r'\(([^)]+)\)\s*((?:오전|오후)\s*\d{1,2}:\d{2})', schedule_text)
    return [{"days": expand_days(day_token), "time": time_token.strip()} for day_token, time_token in groups]


def parse_card(li, category: str):
    """li.info_box 하나 -> 방영 슬롯별로 펼쳐진 program dict 리스트 (시청률 필터링은 호출부에서)"""
    title_tag = li.select_one('strong.title a')
    if not title_tag:
        return []
    title = title_tag.get_text(strip=True)
    link = title_tag.get('href', '')

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
            rating_date = m.group(1).rstrip('.')  # '06.22.' -> '06.22'

    if rating is None:
        return []

    programs = []
    for slot in slots:
        programs.append({
            "id": f"{category}_{title}_{'-'.join(slot['days'])}_{slot['time']}",
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


def parse_cards_from_html(html: str, category: str, min_rating: float = 5.0):
    """HTML 문자열 전체에서 li.info_box 를 모두 찾아 파싱 + 시청률 필터링"""
    soup = BeautifulSoup(html, 'lxml')
    results = []
    for li in soup.select('li.info_box'):
        for p in parse_card(li, category):
            if p["rating"] >= min_rating:
                results.append(p)
    return results


def dedupe_programs(programs: list):
    """동일 id 중복 제거 (페이지 순회 중 같은 카드가 두 번 잡히는 경우 방지)"""
    seen = set()
    out = []
    for p in programs:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        out.append(p)
    return out
