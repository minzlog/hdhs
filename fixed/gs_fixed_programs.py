# -*- coding: utf-8 -*-
"""
gs_fixed_programs.py
GS SHOP 모바일 메인페이지의 "GS SHOP 대표 프로그램" 섹션에서
고정 편성 프로그램(진행자 쇼) 목록과, 각 프로그램이 다음 방송에서
소개할 상품까지 함께 수집한다. (Selenium + Mobile 환경)
"""

import os
import re
import json
import time
from urllib.parse import urljoin
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

KST = timezone(timedelta(hours=9))
OUTPUT_DIR = os.path.join("homeshopping", "fixed_programs")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "GS.json")

GS_MOBILE_URL = "https://m.gsshop.com/index.gs"


def setup_mobile_driver():
    """백그라운드에서 실행되는 모바일 전용 Headless 크롬 브라우저 설정"""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-gpu")
    # 🚨 중요: 모바일 기기(아이폰/갤럭시) 해상도로 설정
    chrome_options.add_argument("--window-size=390,844") 
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    
    # 🚨 중요: 모바일 웹사이트 접속을 위한 Mobile User-Agent 강제 주입
    mobile_ua = "Mozilla/5.0 (Linux; Android 13; SM-S918N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    chrome_options.add_argument(f"user-agent={mobile_ua}")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    
    # webdriver 속성 숨기기
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            })
        """
    })
    return driver


def parse_price(text: str):
    if not text:
        return None
    cleaned = re.sub(r'[^\d]', '', text)
    return int(cleaned) if cleaned else None


def fetch_gs_programs():
    print("  [시스템] 모바일 크롬 브라우저를 백그라운드에서 구동합니다...")
    driver = setup_mobile_driver()
    programs = []

    try:
        print(f"  [GS] 모바일 메인 페이지 접속 중... ({GS_MOBILE_URL})")
        driver.get(GS_MOBILE_URL)
        
        # 페이지 로딩을 위한 대기
        time.sleep(3)
        
        # 'TV' 탭 버튼을 찾아 자바스크립트로 강제 클릭 (화면 로딩 유발)
        # 올려주신 HTML 기준: <a class="item" data-id="618" ...><span>TV</span></a>
        try:
            driver.execute_script("document.querySelector('a[data-id=\"618\"]').click();")
            print("  [GS] 'TV' 탭 클릭 완료, 데이터 로딩 대기 중...")
        except Exception as e:
            print("  [경고] TV 탭 클릭 실패 (기본으로 열려있을 수 있습니다):", e)
            
        # 대표 프로그램 영역(id="tv-sect-pgm2")이 화면에 나타날 때까지 최대 10초 대기
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "tv-sect-pgm2"))
            )
        except Exception as e:
            print(f"  [오류] TV 프로그램 섹션 로딩 실패: {e}")
            
        # 안정적인 DOM 확보를 위해 추가 대기
        time.sleep(2)
        
        # 렌더링된 전체 모바일 HTML 추출
        soup = BeautifulSoup(driver.page_source, "html.parser")
        
        # 대표 프로그램 컨테이너 탐색
        section = soup.select_one("#tv-sect-pgm2")
        if not section:
            print("  [오류] 모바일 페이지에서 '#tv-sect-pgm2' 컨테이너를 찾을 수 없습니다.")
            return programs
            
        # 중복된 가짜 슬라이드(swiper-slide-duplicate) 제외하고 진짜 프로그램 슬라이드만 탐색
        slides = section.select(".swiper-slide:not(.swiper-slide-duplicate)")
        print(f"  [GS] 총 {len(slides)}개의 진행자 프로그램 슬라이드를 발견했습니다.")

        for slide in slides:
            # 1. 프로그램 기본 정보
            head = slide.select_one(".item-head")
            if not head:
                continue
                
            title_tag = head.select_one("h3.ttl-lg")
            title = title_tag.get_text(strip=True) if title_tag else ""
            if not title:
                continue

            schedule_tag = head.select_one("sup.color-primary")
            schedule_raw = schedule_tag.get_text(strip=True) if schedule_tag else ""
            
            desc_tag = head.select_one("sub.desc-md")
            desc = desc_tag.get_text(separator=" ", strip=True) if desc_tag else ""

            # 2. 프로그램 대표 이미지 및 링크
            ban_img_tag = slide.select_one("article.ban-item figure.ban-img img")
            thumbnail = ban_img_tag.get("src") if ban_img_tag else ""
            
            ban_link_tag = slide.select_one("article.ban-item a.ban-link")
            detail_link = ban_link_tag.get("href") if ban_link_tag else ""
            if detail_link and detail_link.startswith("/"):
                detail_link = "https://m.gsshop.com" + detail_link

            # 3. 해당 프로그램의 소개 상품들 파싱
            upcoming_products = []
            prd_items = slide.select("article.prd-item")
            
            for prd in prd_items:
                p_name_tag = prd.select_one(".prd-name")
                p_name = p_name_tag.get_text(strip=True) if p_name_tag else ""
                
                p_price_tag = prd.select_one(".set-price strong")
                p_price = parse_price(p_price_tag.get_text(strip=True)) if p_price_tag else None
                
                p_img_tag = prd.select_one("figure.prd-img img")
                p_image = p_img_tag.get("src") if p_img_tag else ""
                if p_image and p_image.startswith("//"):
                    p_image = "https:" + p_image
                    
                p_link_tag = prd.select_one("a.prd-link")
                p_link = p_link_tag.get("href") if p_link_tag else ""
                if p_link and p_link.startswith("/"):
                    p_link = "https://m.gsshop.com" + p_link
                    
                if p_name:
                    upcoming_products.append({
                        "name": p_name,
                        "price": p_price,
                        "image": p_image,
                        "link": p_link
                    })
                    
            programs.append({
                "conts_no": title,  # GS는 롯데처럼 고유 숫자가 명확하지 않아 타이틀을 식별자로 사용
                "title": title,
                "schedule_raw": schedule_raw,
                "desc": desc,
                "thumbnail": thumbnail,
                "detail_link": detail_link,
                "upcoming_products": upcoming_products
            })
            
    except Exception as e:
        print(f"  [스크립트 오류 발생] {e}")
        
    finally:
        print("  [시스템] 크롬 브라우저를 종료합니다.")
        driver.quit()

    return programs


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("[GS] 고정 프로그램 크롤링 시작 (Mobile Selenium)")
    
    programs = fetch_gs_programs()
        
    payload = {
        "company": "GS",
        "collectedAt": datetime.now(KST).isoformat(),
        "programs": programs
    }
    
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        
    print(f"\n총 {len(programs)}개 프로그램 수집 완료, 저장: {OUTPUT_PATH}")
    for p in programs:
        print(f"  [{p['title']}] {p['schedule_raw']} (상품 {len(p['upcoming_products'])}개)")


if __name__ == "__main__":
    main()