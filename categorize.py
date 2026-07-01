# -*- coding: utf-8 -*-
"""
상품 카테고리 분류기
#사용법: (1)동업계교류실적을 기반으로 브랜드+상품명+상품중분류 주기적 학습
(2)브랜드별 강제분류 일부추가 (특히 가전)

학습된 모델(category_model.pkl)을 로드해 브랜드+상품명으로 카테고리를 예측한다.
크롤러(hd_scraper.py 등)에서 이 모듈을 import해서 사용한다.

== 분류 순서 (우선순위) ==
0) 브랜드 강제 매핑 (BRAND_FORCE_MAP): 브랜드명이 정확히 일치하면 카테고리 확정
   (포함 방식이 아닌 완전일치 - "삼성금거래소"는 "삼성"과 다름)
1) 키워드 규칙: 상품명에 명확한 단어가 있으면 확정
   - 사은품 제외: '+' 기준 첫 덩어리(본품)만 검사
2) 브랜드명이 학습데이터에서 단일 카테고리로만 운영된 브랜드면 바로 확정
3) 브랜드가 비어있으면 상품명에서 브랜드 추론해서 보강 (infer_brand)
   - 추론된 브랜드도 BRAND_FORCE_MAP 검사
4) 모델 분류

== 카테고리 그룹 ==
가전       = 대형가전 + 소형가전 + 다이슨 + 로보락
미용       = 듀얼소닉 + 미용
의류       = 패션의류 + 레포츠의류
잡화/주얼리 = 패션잡화 + 쥬얼리 + 수입명품
여행       = 여행 + 여행(결제)
리빙/주방   = 주방용품 + 인테리어/침구 + 생활용품
기타       = 건강식품, 일반식품, 보험, 일반렌탈, 문화/스포츠, GA 등
"""

import os
import re
import joblib
import pandas as pd
from infer_brand import infer_brand, extract_core_brand

MODEL_PATH = os.path.join(os.path.dirname(__file__), "category_model.pkl")

_TRAINING_XLSX_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "training_data.xlsx"),
    "training_data.xlsx",
]

# 세분류(모델 예측 클래스) -> 통합 그룹명
GROUP_MAP = {
    "대형가전": "가전", "소형가전": "가전", "다이슨": "가전", "로보락": "가전",
    "듀얼소닉": "미용", "미용": "미용",
    "패션의류": "의류", "레포츠의류": "의류",
    "패션잡화": "잡화/주얼리", "쥬얼리": "잡화/주얼리", "수입명품": "잡화/주얼리",
    "여행": "여행", "여행(결제)": "여행",
    "주방용품": "리빙/주방", "인테리어/침구": "리빙/주방", "생활용품": "리빙/주방",
}

# 브랜드명 완전일치 기반 강제 카테고리 매핑
# - 포함(contain)이 아니라 정확히 이 문자열이어야 적용됨
# - "삼성금거래소", "삼성화재"는 "삼성"과 다르므로 걸리지 않음
# - 학습데이터 확인 결과 기준으로 작성. 새 브랜드 추가 시 여기에도 추가할 것.
BRAND_FORCE_MAP = {
    # 가전
    "삼성":          "가전",
    "삼성(SAMSUNG)": "가전",
    "삼성전자":       "가전",
    "삼성화재":       "보험",
    "LG":            "가전",
    "LG(엘지)":      "가전",
    "LG전자":        "가전",
    "엘지":          "가전",
    "FILA":          "의류",
    "삼성금거래소":   "잡화/주얼리",
    "한국금거래소":   "잡화/주얼리",
    "캐롤프랑크":  "미용",
    
    # 필요시 추가: "LG디스플레이": "가전", ...
}

# 명확한 키워드 규칙: (정규식 패턴, 확정 카테고리)
# 위에서부터 순서대로 검사하며 먼저 매칭되는 것을 채택
KEYWORD_RULES = [
    (re.compile(r"암보험|치료비|상해보험|운전자보험|간편보험|실손|여행자보험|건강보험|연금보험|종신보험|보험\b"), "보험"),
    (re.compile(r"항공권|왕복항공|패키지여행|호텔숙박권|자유숙박권|숙박권|크루즈여행"), "여행"),
    (re.compile(r"에어컨|세탁기|냉장고|비스포크|오브제\b|TV\b|티브이|건조기|스타일러|청소기|공기청정기|제습기|인덕션|식기세척기"), "가전"),
    (re.compile(r"24K|18K|목걸이|귀걸이"), "잡화/주얼리"),
    (re.compile(r"스킨기초|기초|스킨케어|기초세트|조윤주|헤어그릭스|샴푸"), "미용"),
]


_model = None
_brand_group_map = None


def _load_brand_group_map():
    """
    학습데이터를 브랜드별로 묶어서,
    단일 카테고리 그룹에서만 등장하면 {브랜드명: 그룹명},
    여러 그룹에 걸치면 {브랜드명: None} (모호 - 상품명까지 봐야 함).
    """
    global _brand_group_map
    if _brand_group_map is not None:
        return _brand_group_map

    xlsx_path = None
    for cand in _TRAINING_XLSX_CANDIDATES:
        if os.path.exists(cand):
            xlsx_path = cand
            break

    if xlsx_path is None:
        _brand_group_map = {}
        return _brand_group_map

    df = pd.read_excel(xlsx_path, sheet_name=0)
    mapping = {}
    for brand, sub in df.dropna(subset=["브랜드명", "상품중분류명"]).groupby("브랜드명"):
        groups = {_to_group(c) for c in sub["상품중분류명"].unique()}
        mapping[str(brand)] = groups.pop() if len(groups) == 1 else None
        core = extract_core_brand(str(brand))
        if core and core not in mapping:
            mapping[core] = mapping[str(brand)]

    _brand_group_map = mapping
    return _brand_group_map


def _brand_direct_category(brand: str) -> str:
    """학습데이터 상 단일 카테고리 브랜드면 그 카테고리, 아니면 빈 문자열."""
    mapping = _load_brand_group_map()
    group = mapping.get(brand)
    if group is None:
        core = extract_core_brand(brand)
        group = mapping.get(core)
    return group or ""


def _load_model():
    global _model
    if _model is None:
        _model = joblib.load(MODEL_PATH)
    return _model


def _to_group(raw_category: str) -> str:
    return GROUP_MAP.get(raw_category, raw_category)


def _main_item_text(brand: str, product: str) -> str:
    """
    키워드 규칙 검사용 본품 텍스트만 추출.
    - '+' 기준 첫 덩어리만 (사은품/구성품에 다른 카테고리 키워드가 섞여 오발동 방지)
    - 브랜드 부기 제거: "노랑풍선(TV)" 같은 표기가 TV 키워드에 걸리는 것 방지
    """
    main_part = str(product or "").split("+")[0].strip()
    if brand and main_part.startswith(brand):
        main_part = main_part[len(brand):].strip()
    core_brand = extract_core_brand(brand) if brand else ""
    return f"{core_brand} {main_part}".strip()


def _rule_match(text: str) -> str:
    for pattern, category in KEYWORD_RULES:
        if pattern.search(text):
            return category
    return ""


def _predict_with_model(brand: str, product: str) -> str:
    model = _load_model()
    text = f"{brand or ''} {product or ''}".strip()
    proba = model.predict_proba([text])[0]
    idx = proba.argmax()
    return _to_group(model.classes_[idx])


def _brand_force(brand: str) -> str:
    """BRAND_FORCE_MAP 완전일치 검사. 매칭되면 확정 카테고리, 아니면 빈 문자열."""
    return BRAND_FORCE_MAP.get(brand, "")


def resolve_display_brand(brand: str, product: str) -> str:
    """
    화면에 보여줄 브랜드명 반환 (GS/CJ처럼 brand 비어있는 경우 product에서 추론).
    - brand 있으면: 괄호 부기 제거 후 반환 ("삼성(SAMSUNG)" -> "삼성")
    - brand 없으면: infer_brand로 추론 시도
    - 둘 다 실패하면 빈 문자열
    """
    if brand:
        return extract_core_brand(brand)
    inferred = infer_brand(product)
    if inferred:
        return extract_core_brand(inferred)
    return ""


def resolve_display_brand_batch(items: list) -> list:
    return [resolve_display_brand(brand, product) for brand, product in items]


def classify(brand: str, product: str) -> str:
    if not brand and not product:
        return ""

    # 0) 브랜드 완전일치 강제 매핑
    if brand:
        forced = _brand_force(brand)
        if forced:
            return forced

    # 1) 키워드 규칙 (본품만 검사)
    rule_hit = _rule_match(_main_item_text(brand, product))
    if rule_hit:
        return rule_hit

    # 2) 학습데이터 단일 카테고리 브랜드 직접 확정
    if brand:
        direct = _brand_direct_category(brand)
        if direct:
            return direct

    # 3) 브랜드 비어있으면 상품명에서 추론해서 보강
    effective_brand = brand
    if not effective_brand:
        effective_brand = infer_brand(product)
        if effective_brand:
            forced = _brand_force(effective_brand)
            if forced:
                return forced

    # 4) 모델 분류
    return _predict_with_model(effective_brand, product)


def classify_batch(items: list) -> list:
    results = [None] * len(items)
    model_indices = []
    model_texts = []

    for i, (brand, product) in enumerate(items):
        if not brand and not product:
            results[i] = ""
            continue

        # 0) 브랜드 완전일치 강제 매핑
        if brand:
            forced = _brand_force(brand)
            if forced:
                results[i] = forced
                continue

        # 1) 키워드 규칙
        rule_hit = _rule_match(_main_item_text(brand, product))
        if rule_hit:
            results[i] = rule_hit
            continue

        # 2) 단일 카테고리 브랜드 직접 확정
        if brand:
            direct = _brand_direct_category(brand)
            if direct:
                results[i] = direct
                continue

        # 3)+4) 브랜드 추론 후 모델
        effective_brand = brand
        if not effective_brand:
            effective_brand = infer_brand(product)
            if effective_brand:
                forced = _brand_force(effective_brand)
                if forced:
                    results[i] = forced
                    continue
        model_indices.append(i)
        model_texts.append(f"{effective_brand or ''} {product or ''}".strip())

    if model_texts:
        model = _load_model()
        proba_matrix = model.predict_proba(model_texts)
        for j, proba in enumerate(proba_matrix):
            idx = proba.argmax()
            results[model_indices[j]] = _to_group(model.classes_[idx])

    return results


if __name__ == "__main__":
    samples = [
        ("삼성(SAMSUNG)", "삼성 비스포크 김치냉장고 4도어"),
        ("삼성",          "삼성 AI Q9000 에어컨 홈멀티"),
        ("삼성금거래소",   "24K 순금 더블볼륨 체인 목걸이"),
        ("삼성화재",       "다이렉트 운전자보험"),
        ("LG",            "LG 통돌이 세탁기 T19MX7A 미드 블랙"),
        ("LG생활건강",     "숨37 로시크숨 에센스"),
        ("닥터린",        "닥터린 하이퍼셀 대마종자유 12박스"),
        ("에이스바이옴",   "에이스바이옴 비에날씬 프로 12박스"),
        ("",              "원스톱프리미엄암보험_치료비플랜"),
        ("",              "[방송에서만 1박 더] 해피한 자유숙박권 총 7박"),
    ]
    for brand, product in samples:
        cat = classify(brand, product)
        print(f"[{brand or '(없음)'}] {product[:30]:30s} -> {cat}")
