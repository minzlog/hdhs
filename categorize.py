# -*- coding: utf-8 -*-
"""
상품 카테고리 분류기
#사용법: (1)동업계교류실적을 기반으로 브랜드+상품명+상품중분류 주기적 학습
(2)브랜드별 강제분류 일부추가 (특히 가전)

학습된 모델(category_model.pkl)을 로드해 브랜드+상품명으로 카테고리를 예측한다.
크롤러(hd_scraper.py 등)에서 이 모듈을 import해서 사용한다.

== 사용법 ==
  from categorize import classify

  category = classify("삼성(SAMSUNG)", "삼성 비스포크 김치냉장고 4도어")
  # -> "가전"

== 분류 순서 (우선순위) ==
1) 명확한 키워드 규칙: 상품명에 "암보험", "치료비", "여행자보험" 등 의심의 여지가
   거의 없는 단어가 있으면 모델 판단 없이 바로 그 카테고리로 확정한다.
   (모델은 통계적 추정이라 이런 명백한 경우도 가끔 헷갈리므로, 규칙이 더 믿을만하다)
   단, 검사 대상은 "본품"만이다 - 상품명이 "본품+사은품1+사은품2..." 구조일 때
   '+' 뒤의 사은품/구성품 텍스트는 제외하고 첫 덩어리(본품)만 본다.
   (예: 건강식품 본품에 "미니 냉장고" 사은품이 끼어 있어도 가전 규칙에 안 걸림)
2) 브랜드 보강: 브랜드 필드가 비어있는 입력(GS, CJ 등)은 상품명 안에서 학습 데이터의
   브랜드 사전과 매칭되는 토큰("LG", "삼성" 등)을 찾아 브랜드로 채워 넣은 뒤 분류한다.
   브랜드가 핵심 신호인 모델 특성상, 이게 없으면 확신도가 크게 떨어진다.
3) 모델 분류: 위 두 단계로 못 잡으면 학습 모델이 예측한 세분류를 그룹으로 합쳐서 반환.
   (모델 입력은 상품명 전체를 그대로 사용 - 사은품 텍스트도 통계적 신호로는 유효하므로
   1)과 달리 자르지 않는다)

== 카테고리 그룹 ==
가전       = 대형가전 + 소형가전 + 다이슨 + 로보락
미용       = 듀얼소닉 + 미용
의류       = 패션의류 + 레포츠의류
잡화/주얼리 = 패션잡화 + 쥬얼리 + 수입명품
여행       = 여행 + 여행(결제)
리빙/주방   = 주방용품 + 인테리어/침구 + 생활용품
기타       = 건강식품, 일반식품, 보험, 일반렌탈, 문화/스포츠, GA 등
            (위 그룹에 속하지 않는 나머지는 모델이 예측한 세분류명 그대로 사용)

== 화면 표시용 브랜드 추출 (GS/CJ 전용) ==
GS(라방바)와 CJ(API brandName 미제공)는 brand 필드가 비어 있어 HD/LT처럼
화면에 브랜드를 분리해서 보여줄 수 없었다. resolve_display_brand()는 분류에
쓰는 것과 같은 추론 로직으로 "화면에 보여줄 정제된 브랜드명"을 반환한다.

  from categorize import resolve_display_brand

  resolve_display_brand("", "LG 통돌이 세탁기 T19MX7A 미드 블랙")
  # -> "LG"  (학습데이터 표기 "LG(엘지)"에서 부기를 뺀 표시용 버전)

  resolve_display_brand("", "[최초가69,900원] 25SS 프렌치 린넨 펄니트 3종")
  # -> ""  (사전에 없는 브랜드는 추론 불가 - 빈 문자열)

브랜드가 이미 있으면(HD/LT, 또는 CJ가 API로 받아온 값이 있는 경우) 그 값을
그대로 정제해서 반환하고, 비어 있을 때만 product에서 추론을 시도한다.

== 모델 재학습 ==
새 학습 데이터(엑셀: 브랜드명/판매상품명/상품중분류명 컬럼)가 생기면
train_model.py를 다시 실행해 category_model.pkl을 교체한다.
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

# 명확한 키워드 규칙: (정규식 패턴, 확정 카테고리)
# 위에서부터 순서대로 검사하며 먼저 매칭되는 것을 채택
KEYWORD_RULES = [
    (re.compile(r"암보험|치료비|상해보험|운전자보험|간편보험|실손|여행자보험|건강보험|연금보험|종신보험|보험\b"), "보험"),
    (re.compile(r"항공권|왕복항공|패키지여행|호텔숙박권|자유숙박권|숙박권|크루즈여행"), "여행"),
    # [보강] 통계 모델 오분류 방지를 위한 강력한 가전 제품 키워드 규칙 추가
    (re.compile(r"에어컨|세탁기|냉장고|비스포크|오브제\b|TV\b|티브이|건조기|스타일러|청소기|공기청정기|제습기|인덕션|식기세척기"), "가전"),
]

# 브랜드명에 이 패턴이 포함되면 상품명/모델 판단과 무관하게 무조건 가전으로 확정.
# (live brand 필드가 학습데이터 표기("삼성(SAMSUNG)")와 다른 plain 텍스트("삼성")로
#  들어올 때 모델이 영문 부기 신호 부재로 오분류하는 문제를 우회하기 위한 규칙)
BRAND_FORCE_RULES = [
    (re.compile(r"삼성|LG|SAMSUNG|엘지|스타일리스"), "가전"),
    (re.compile(r"삼성화재|현대해상|보험"), "보험"),
    (re.compile(r"노스페이스|코오롱스포츠"), "의류"),
]


_model = None
_brand_group_map = None  # {브랜드명: 통합그룹명 or None(모호한 브랜드)}


def _load_brand_group_map():
    """
    학습데이터(training_data.xlsx)를 브랜드명 기준으로 묶어서,
    그 브랜드가 단 하나의 카테고리 그룹에서만 등장하면 {브랜드명: 그룹명},
    여러 그룹에 걸쳐 있으면(예: 동인비/라비앙처럼 미용+건강식품 겸업) {브랜드명: None}으로 표시한다.
    None인 브랜드는 브랜드명만으로 확정할 수 없으므로 호출부에서 상품명까지 같이 봐야 한다.
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
        # 원본 표기뿐 아니라 괄호 제거한 핵심 브랜드명으로도 같이 찾을 수 있게 등록
        core = extract_core_brand(str(brand))
        if core and core not in mapping:
            mapping[core] = mapping[str(brand)]

    _brand_group_map = mapping
    return _brand_group_map


def _brand_direct_category(brand: str) -> str:
    """
    브랜드명이 주어졌을 때, 학습데이터 상 그 브랜드가 단일 카테고리에서만
    운영돼 왔다면 그 카테고리를 바로 반환. 모호한 브랜드(미용/건강식품 겸업 등)이거나
    학습데이터에 없는 브랜드면 빈 문자열을 반환해 호출부가 상품명까지 보게 한다.
    """
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
    """세분류명을 통합 그룹명으로 변환. 매핑에 없으면 원래 세분류명 그대로 사용."""
    return GROUP_MAP.get(raw_category, raw_category)


def _main_item_text(brand: str, product: str) -> str:
    """
    키워드 규칙 검사용 본품 텍스트만 추출.
    - 사은품 제외: 홈쇼핑 상품명은 보통 "본품+사은품1+사은품2..." 구조라 사은품에
      다른 카테고리 키워드(예: 건강식품 본품+냉장고 사은품)가 섞이면 키워드 규칙이
      오발동할 수 있다. '+' 기준으로 첫 덩어리(본품)만 검사 대상으로 삼아 이를 방지한다.
    - 브랜드 부기 제거: "노랑풍선(TV)", "여행박사(TV)"처럼 브랜드명에 TV홈쇼핑
      채널 표기로 "(TV)"가 붙는 경우가 있는데, 이게 가전 규칙의 'TV\\b' 패턴에
      잘못 매칭돼 여행 상품을 가전으로 오분류시킨다. 게다가 판매상품명도 보통
      "노랑풍선(TV) 노랑풍선 ..." 식으로 브랜드 원본 표기가 맨 앞에 그대로
      반복되므로, brand뿐 아니라 product 맨 앞의 동일 표기도 함께 제거한다.
    """
    main_part = str(product or "").split("+")[0].strip()
    if brand and main_part.startswith(brand):
        main_part = main_part[len(brand):].strip()

    core_brand = extract_core_brand(brand) if brand else ""
    return f"{core_brand} {main_part}".strip()


def _rule_match(text: str) -> str:
    """명확한 키워드 규칙에 해당하면 확정 카테고리를 반환, 없으면 빈 문자열."""
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


def resolve_display_brand(brand: str, product: str) -> str:
    """
    화면에 보여줄 브랜드명을 반환 (GS/CJ처럼 brand가 비어있는 경우 product에서 추론).
    - brand가 이미 있으면: 괄호 부기를 뺀 핵심 브랜드명으로 정제해서 반환
      (예: "삼성(SAMSUNG)" -> "삼성")
    - brand가 없으면: product에서 infer_brand로 추론 시도, 성공하면 마찬가지로 정제해서 반환
    - 둘 다 실패하면 빈 문자열 (화면에서는 브랜드 영역을 표시하지 않으면 됨)
    """
    if brand:
        return extract_core_brand(brand)

    inferred = infer_brand(product)
    if inferred:
        return extract_core_brand(inferred)

    return ""


def resolve_display_brand_batch(items: list) -> list:
    """[(brand, product), ...] 리스트를 일괄 처리. 반환: 표시용 브랜드명 문자열 리스트."""
    return [resolve_display_brand(brand, product) for brand, product in items]


def classify(brand: str, product: str) -> str:
    """
    브랜드명 + 상품명으로 카테고리(통합 그룹)를 예측.
    빈 입력이 아니면 항상 가장 가능성 높은 카테고리를 반환한다 (미분류 없음).

    분류 순서:
    0) 브랜드명에 "삼성"/"LG"가 포함되면 상품명/모델 판단과 무관하게 무조건 "가전"
    1) 키워드 규칙 (보험/여행/가전 확정 키워드)
    2) 브랜드명이 제공된 경우: 학습데이터 상 그 브랜드가 단일 카테고리에서만 운영돼
       왔다면 상품명을 볼 것도 없이 그 카테고리로 바로 확정.
       단, 동인비/라비앙처럼 미용+건강식품을 같은 브랜드명으로 같이 운영하는 등
       브랜드 하나가 여러 카테고리에 걸쳐 있으면(모호) 이 단계를 건너뛰고
       상품명까지 같이 보는 모델 판단(4)으로 넘어간다.
    3) 브랜드가 비어있으면 상품명에서 브랜드 추론해서 보강
    4) 모델 분류 (브랜드+상품명 텍스트 기반)
    """
    if not brand and not product:
        return ""

    # 0) 삼성/LG는 무조건 가전 (브랜드명 기준, 최우선)
    if brand:
        for pattern, category in BRAND_FORCE_RULES:
            if pattern.search(brand):
                return category

    # 1) 명확한 키워드 규칙 (사은품 구성품 텍스트는 제외하고 본품만 검사)
    rule_hit = _rule_match(_main_item_text(brand, product))
    if rule_hit:
        return rule_hit

    # 2) 브랜드명이 학습데이터에서 단일 카테고리로만 운영돼 왔다면 바로 그 카테고리로 확정
    if brand:
        direct = _brand_direct_category(brand)
        if direct:
            return direct

    # 3) 브랜드가 비어있으면 상품명에서 브랜드 추론해서 보강
    effective_brand = brand
    if not effective_brand:
        effective_brand = infer_brand(product)
        # 추론된 브랜드가 삼성/LG면 마찬가지로 무조건 가전 (모델 추론에 기대지 않는 보장)
        if effective_brand:
            for pattern, category in BRAND_FORCE_RULES:
                if pattern.search(effective_brand):
                    return category

    # 4) 모델 분류
    return _predict_with_model(effective_brand, product)


def classify_batch(items: list) -> list:
    """
    [(brand, product), ...] 리스트를 한 번에 분류 (개별 호출보다 빠름).
    반환: 통합 그룹명 문자열 리스트.
    classify()와 동일한 우선순위(삼성/LG 강제 -> 키워드 규칙 -> 브랜드 단일카테고리
    직접매핑 -> 모델)를 적용하되, 모델까지 가야 하는 항목만 모아서 한 번에 예측한다.
    """
    results = [None] * len(items)
    model_indices = []
    model_texts = []

    for i, (brand, product) in enumerate(items):
        if not brand and not product:
            results[i] = ""
            continue

        # 0) 삼성/LG는 무조건 가전
        forced = ""
        if brand:
            for pattern, category in BRAND_FORCE_RULES:
                if pattern.search(brand):
                    forced = category
                    break
        if forced:
            results[i] = forced
            continue

        # 1) 키워드 규칙
        rule_hit = _rule_match(_main_item_text(brand, product))
        if rule_hit:
            results[i] = rule_hit
            continue

        # 2) 브랜드명이 단일 카테고리로만 운영돼 왔다면 바로 확정
        if brand:
            direct = _brand_direct_category(brand)
            if direct:
                results[i] = direct
                continue

        # 3)+4) 그 외에는 모델로 (브랜드 비어있으면 상품명에서 추론해 보강)
        effective_brand = brand
        if not effective_brand:
            effective_brand = infer_brand(product)
            if effective_brand:
                inferred_forced = ""
                for pattern, category in BRAND_FORCE_RULES:
                    if pattern.search(effective_brand):
                        inferred_forced = category
                        break
                if inferred_forced:
                    results[i] = inferred_forced
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
    # 간단한 동작 확인
    samples = [
        ("삼성(SAMSUNG)", "삼성 비스포크 김치냉장고 4도어"),
        ("닥터린", "닥터린 하이퍼셀 대마종자유 12박스"),
        ("", "정체불명 신상품 XYZ"),
        ("스케쳐스", "스케쳐스 26SS 맥스쿠셔닝 워킹화"),
        ("다이슨", "다이슨 에어랩 컴플리트 롱"),
        ("", "LG 통돌이 세탁기 T19MX7A 미드 블랙"),
        ("", "원스톱프리미엄암보험_치료비플랜"),
        ("", "[방송에서만 1박 더] 해피한 자유숙박권 총 7박"),
        ("삼성", "삼성 AI Q9000 에어컨 홈멀티"), # 오분류 테스트 케이스 추가
    ]
    for brand, product in samples:
        cat = classify(brand, product)
        print(f"{product[:35]:35s} -> {cat}")