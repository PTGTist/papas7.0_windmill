# PAPAS 7.0 풍차돌리기 웹앱

## 포함 파일
- `app.py`: Streamlit 웹앱
- `requirements.txt`: 실행 패키지
- `supabase_schema.sql`: Supabase 테이블 및 사용자별 보안 정책

## 1. Supabase 준비
1. Supabase에서 새 프로젝트 생성
2. SQL Editor에서 `supabase_schema.sql` 전체 실행
3. Project Settings > API에서 Project URL과 anon/public key 복사
4. Authentication에서 이메일 회원가입 사용 여부와 이메일 인증 설정 확인

## 2. GitHub 저장소
1. 새 저장소 생성 (예: `papas7-windmill`)
2. 이 폴더의 파일 3개를 저장소 최상위에 업로드
3. Streamlit Community Cloud에서 저장소를 선택하고 Main file을 `app.py`로 지정

## 3. Streamlit Secrets
앱 배포 설정의 Secrets에 아래 형식으로 입력:

SUPABASE_URL = "https://프로젝트ID.supabase.co"
SUPABASE_ANON_KEY = "Supabase의 anon 또는 publishable key"

절대로 service_role 키를 사용하거나 GitHub에 올리지 마세요.

## 4. 최초 사용
1. 웹앱에서 이메일 회원가입
2. 이메일 인증이 켜져 있으면 인증 후 로그인
3. 첫 로그인 계정에 기본 SOXL 포트폴리오가 자동 생성됨
4. 설정 메뉴에서 종목/초기 보유수량/전략 비율/초기 현금 수정
5. 풍차돌리기에서 Stage 생성
6. 거래 내역에서 실제 매도·매수 체결 입력

## 계산 원칙
- 매도는 현금 증가 및 주식 수량 감소
- 일반 매수와 Make up 매수는 현금 감소 및 주식 수량 증가
- 매수 기준가는 Stage 매도 평균가에서 시작하는 등비수열
- Make up은 일반 매수와 기존 Make up 체결량을 차감해 부족 수량 산출
- 주봉 회복 기준은 5주선, 일봉 회복 기준은 30일선
- Stage 총 매도량이 전략 적용 수량을 넘으면 경고만 표시하며 입력은 허용

## 현재 구현 범위 / 주의
- 로그인/회원가입, 사용자별 Supabase 저장, Stage 생성 및 설정, 체결 원장, Make up 부족 수량, 대시보드, 기본 분석, yfinance 자동 주가 조회 및 수동 가격 저장 포함
- 주가 자동 조회는 yfinance 공개 데이터 경로를 사용하며 데이터 제공 여부/지연/제한을 보장하지 않습니다.
- 현재 앱은 매매 기록 및 분석용이며 증권사 주문을 실행하지 않습니다.
- 실제 손익, 세금, 환율별 원가 추적, 고급 MDD, 알림 발송, 기존 엑셀 전체 데이터 자동 이관은 후속 검증/개발이 필요합니다.
