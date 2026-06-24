# -*- coding: utf-8 -*-
"""
브랜드 미보유/부실 상품명에서 브랜드를 추론하는 모듈 (GS, CJ 공용 보조)

GS는 라방바 API 특성상 브랜드 필드가 항상 비어있다.
CJ는 API의 brandName이 거의 항상 None이고, 상품 상세페이지는 JS로 렌더링되는
SPA라 requests로는 브랜드를 가져올 수 없다(별도 상세 API 미확인). 그래서 CJ도
상품명(itemNm) 텍스트에서 브랜드를 추론해야 한다.

분류 모델은 "브랜드명 + 상품명"으로 학습되어 브랜드가 핵심 신호인데, 이 신호가
없으면 분류 확신도가 크게 떨어진다(예: "LG 통돌이 세탁기"가 생활용품/일반식품
등으로 헷갈림 - 확신도 20% 이하).

이 모듈은 학습 데이터(training_data.xlsx)의 브랜드 목록에서
핵심 토큰("LG(엘지)" -> "LG", "삼성(SAMSUNG)" -> "삼성")을 추출해 사전을 만들고,
상품명 안에 그 토큰이 포함돼 있으면 찾아내 분류 모델 입력 보강 및 화면 표시용
브랜드로 사용한다.

== 매칭 안전장치 ==
- 매칭 전에 대괄호/소괄호 마케팅 카피를 제거한다
  (예: "[방송에서만] 핏업 골드..." -> "핏업 골드..."로 정리 후 매칭)
  CJ 상품명은 "[최초가69,900원]", "(최신상)" 같은 안내문이 브랜드 앞에 자주 붙어
  있어, 이걸 제거하지 않으면 "맨 앞 단어 매칭"이 거의 항상 실패한다.
- 단, 대괄호 안에 브랜드명이 들어있는 경우도 있다(예: GS 상품명
  "[아로마티카] 스파 샴푸..."). 이런 케이스를 놓치지 않기 위해, 본문 매칭이
  실패하면 제거했던 대괄호/소괄호 안의 텍스트들도 같은 규칙으로 한 번 더
  시도한다(괄호 안 내용 자체를 "맨 앞 단어" 취급).
- 긴 토큰을 먼저 매칭한다 (longest-match-first) - "LG전자"가 "LG"보다 먼저 매칭되도록
- 2글자 이하 토큰은 "맨 앞 단어"와 "정확히 일치"할 때만 인정한다
  (예: "로던"이 상품명 중간 어딘가에 우연히 끼어 있는 경우는 무시,
   상품명이 "로던 ..."으로 시작할 때만 인정). 대괄호 안 텍스트를 검사할 때는
   그 괄호 안 텍스트의 첫 단어를 기준으로 동일하게 적용한다.
- 공백 유무 차이로 매칭이 실패하는 경우(학습데이터 "라이나생명" vs
  상품명 "라이나 생명")를 위해, 일반 매칭이 실패하면 공백을 제거한 뒤
  한 번 더 시도한다.

== 사용법 ==
  from infer_brand import infer_brand
  infer_brand("LG 통돌이 세탁기 T19MX7A 미드 블랙")
  # -> "LG(엘지)"  (매칭 안 되면 "")

  infer_brand("[최초가69,900원]배럴 커브드 데님")
  # -> 마케팅 카피 제거 후 "배럴 커브드 데님"으로 매칭 시도
"""

import os
import re
import pandas as pd

_TRAINING_XLSX_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "training_data.xlsx"),
    "training_data.xlsx",
]

_brand_tokens = None  # 길이 내림차순 정렬된 (토큰, 원본브랜드) 리스트
_brand_tokens_nospace = None  # 공백 제거 버전 (토큰_nospace, 원본브랜드), 길이 내림차순

# 상품명 앞에 자주 붙는 대괄호/소괄호 마케팅 카피 제거용
# (분류 모델 입력에는 영향 없음 - 브랜드 추론 매칭 전처리에만 사용)
_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]|\(([^()]*)\)")
_MULTI_SPACE_RE = re.compile(r"\s{2,}")


def extract_core_brand(brand: str) -> str:
    """브랜드명에서 괄호 안내문 제거해 화면 표시용 핵심 브랜드명만 반환.
    'LG(엘지)' -> 'LG', '교원투어(TV)' -> '교원투어'
    내부 매칭에 쓰는 _extract_core와 동일하나, 외부(categorize.py 등)에서
    표시용 브랜드 정제 목적으로 쓸 수 있게 공개 함수로 둔다."""
    return _extract_core(brand)


def _extract_core(brand: str) -> str:
    """브랜드명에서 괄호 안내문 제거: 'LG(엘지)' -> 'LG'"""
    return re.sub(r"\([^)]*\)", "", str(brand)).strip()


def _strip_marketing_copy(text: str):
    """매칭 전처리: 대괄호/소괄호 안내문을 제거한 본문과, 제거된 괄호 안
    내용들을 함께 반환한다. 분류용 원본 텍스트는 그대로 두고, 브랜드 매칭에만
    이 정제본/괄호내용을 사용한다.
    반환: (본문(괄호 제거+공백정리), [괄호 안 텍스트, ...])
    """
    bracket_contents = [g1 or g2 for g1, g2 in _BRACKET_RE.findall(text)]
    bracket_contents = [c.strip() for c in bracket_contents if c and c.strip()]

    body = _BRACKET_RE.sub(" ", text)
    body = _MULTI_SPACE_RE.sub(" ", body).strip()
    return body, bracket_contents


def _load_brand_tokens():
    global _brand_tokens, _brand_tokens_nospace
    if _brand_tokens is not None:
        return _brand_tokens, _brand_tokens_nospace

    xlsx_path = None
    for cand in _TRAINING_XLSX_CANDIDATES:
        if os.path.exists(cand):
            xlsx_path = cand
            break

    if xlsx_path is None:
        _brand_tokens = []
        _brand_tokens_nospace = []
        return _brand_tokens, _brand_tokens_nospace

    df = pd.read_excel(xlsx_path, sheet_name=0)
    raw_brands = df["브랜드명"].dropna().unique()

    seen = set()
    tokens = []
    for b in raw_brands:
        core = _extract_core(b)
        if core and core not in seen:
            seen.add(core)
            tokens.append((core, str(b)))

    # 긴 토큰을 먼저 매칭하도록 길이 내림차순 정렬
    tokens.sort(key=lambda t: len(t[0]), reverse=True)
    _brand_tokens = tokens

    # 공백 제거 버전 (길이는 공백 제거 전 기준으로 정렬해 일관성 유지)
    tokens_nospace = [(t.replace(" ", ""), original) for t, original in tokens]
    tokens_nospace.sort(key=lambda t: len(t[0]), reverse=True)
    _brand_tokens_nospace = tokens_nospace

    return _brand_tokens, _brand_tokens_nospace


def _match(text: str, first_word: str, tokens: list) -> str:
    for token, original in tokens:
        if not token:
            continue
        if len(token) <= 2:
            # 짧은 토큰은 오매칭 위험이 커서 맨 앞 단어와 완전히 같을 때만 인정
            if token == first_word:
                return original
        else:
            if token in text:
                return original
    return ""


def infer_brand(product_name: str) -> str:
    """
    상품명 안에서 학습 데이터 브랜드 사전과 매칭되는 브랜드를 찾아 반환.
    모델은 학습 데이터의 정확한 표기(예: "LG(엘지)")에 민감하므로,
    매칭에 쓴 핵심 토큰이 아니라 원본 표기를 그대로 반환한다.
    매칭 안 되면 빈 문자열.
    """
    if not product_name:
        return ""

    tokens, tokens_nospace = _load_brand_tokens()
    if not tokens:
        return ""

    raw = str(product_name)
    body, bracket_contents = _strip_marketing_copy(raw)
    if not body:
        body = raw.strip()

    # 검사할 텍스트 후보들: 본문(괄호 제거) 먼저, 그 다음 괄호 안 내용들
    # (본문에 브랜드가 있는 경우가 더 흔하므로 우선 순위를 둔다.
    #  예: "[방송에서만] 핏업 골드..." -> 본문 "핏업 골드..."에서 먼저 매칭됨.
    #  본문에서 못 찾으면 "[아로마티카] 스파 샴푸..." 같이 브랜드가
    #  괄호 안에 있는 경우를 위해 괄호 내용도 차례로 시도한다.)
    candidates = [body] + bracket_contents

    for candidate in candidates:
        first_word = candidate.split()[0] if candidate.split() else ""

        result = _match(candidate, first_word, tokens)
        if result:
            return result

        # 공백 차이로 실패한 경우(학습데이터 "라이나생명" vs 상품명 "라이나 생명")
        candidate_nospace = candidate.replace(" ", "")
        first_word_nospace = first_word.replace(" ", "")
        result = _match(candidate_nospace, first_word_nospace, tokens_nospace)
        if result:
            return result

    return ""


if __name__ == "__main__":
    samples = [
        "LG 통돌이 세탁기 T19MX7A 미드 블랙",
        "원스톱프리미엄암보험_치료비플랜",
        "삼성 비스포크 김치냉장고 4도어",
        "스테파넬 26SS 썸머 쿨드레이프 팬츠",
        "[방송에서만]핏업 골드 유기농 대마종자유 18박스(12+6박스)",
        "[최초가69,900원]배럴 커브드 데님",
        "(최신상)아치나인Arch-9 Flux기능성 슬리퍼_블랙",
        "[LIVE]라이나 생명 The건강한치아보험V",
        "[아로마티카] 스파 샴푸 1등 패키지 (샴푸7+트리트먼트2)",  # 브랜드가 괄호 안
        "26년형 신일 써큘레이터 S11 미드나잇 블랙 (SIF-DH09BK) 1대 구성",  # 2글자 브랜드, 맨앞 아님
    ]
    for s in samples:
        print(f"{s[:45]:45s} -> 추론 브랜드: {infer_brand(s) or '(없음)'}")
