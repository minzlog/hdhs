# -*- coding: utf-8 -*-
"""
상품 카테고리 분류기

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
2) 브랜드 보강: 브랜드 필드가 비어있는 입력(GS 등)은 상품명 안에서 학습 데이터의
   브랜드 사전과 매칭되는 토큰("LG", "삼성" 등)을 찾아 브랜드로 채워 넣은 뒤 분류한다.
   브랜드가 핵심 신호인 모델 특성상, 이게 없으면 확신도가 크게 떨어진다.
3) 모델 분류: 위 두 단계로 못 잡으면 학습 모델이 예측한 세분류를 그룹으로 합쳐서 반환.

== 카테고리 그룹 ==
가전       = 대형가전 + 소형가전 + 다이슨 + 로보락
미용       = 듀얼소닉 + 미용
의류       = 패션의류 + 레포츠의류
잡화/주얼리 = 패션잡화 + 쥬얼리 + 수입명품
여행       = 여행 + 여행(결제)
리빙/주방   = 주방용품 + 인테리어/침구 + 생활용품
기타       = 건강식품, 일반식품, 보험, 일반렌탈, 문화/스포츠, GA 등
            (위 그룹에 속하지 않는 나머지는 모델이 예측한 세분류명 그대로 사용)

== 모델 재학습 ==
새 학습 데이터(엑셀: 브랜드명/판매상품명/상품중분류명 컬럼)가 생기면
train_model.py를 다시 실행해 category_model.pkl을 교체한다.
"""

import os
import re
import joblib
from infer_brand import infer_brand

MODEL_PATH = os.path.join(os.path.dirname(__file__), "category_model.pkl")

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


_model = None


def _load_model():
    global _model
    if _model is None:
        _model = joblib.load(MODEL_PATH)
    return _model


def _to_group(raw_category: str) -> str:
    """세분류명을 통합 그룹명으로 변환. 매핑에 없으면 원래 세분류명 그대로 사용."""
    return GROUP_MAP.get(raw_category, raw_category)


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


def classify(brand: str, product: str) -> str:
    """
    브랜드명 + 상품명으로 카테고리(통합 그룹)를 예측.
    빈 입력이 아니면 항상 가장 가능성 높은 카테고리를 반환한다 (미분류 없음).
    """
    if not brand and not product:
        return ""

    # 1) 명확한 키워드 규칙 우선 적용
    rule_hit = _rule_match(f"{brand or ''} {product or ''}")
    if rule_hit:
        return rule_hit

    # 2) 브랜드가 비어있으면 상품명에서 브랜드 추론해서 보강
    effective_brand = brand
    if not effective_brand:
        effective_brand = infer_brand(product)

    # 3) 모델 분류
    return _predict_with_model(effective_brand, product)


def classify_batch(items: list) -> list:
    """
    [(brand, product), ...] 리스트를 한 번에 분류 (개별 호출보다 빠름).
    반환: 통합 그룹명 문자열 리스트.
    """
    # 키워드 규칙으로 먼저 걸러내고, 나머지만 모델에 보낸다
    results = [None] * len(items)
    model_indices = []
    model_texts = []

    for i, (brand, product) in enumerate(items):
        if not brand and not product:
            results[i] = ""
            continue
        rule_hit = _rule_match(f"{brand or ''} {product or ''}")
        if rule_hit:
            results[i] = rule_hit
            continue
        effective_brand = brand or infer_brand(product)
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
