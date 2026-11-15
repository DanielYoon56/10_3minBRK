# 2WAYTRADING (국내 주식 자동매매 시스템)

> **OPEN · CLOSE 2대 전략 체제** — 장중 가짜 돌파로 손실이 잦던 장중(INTRADAY) 전략을 완전히 폐기하고,
> 시장 거래가 집중되는 **OPEN**(장 초반 단타)과 전 기간 유일하게 일관된 흑자를 입증한 **CLOSE**(장 마감 전 종가베팅/스윙) 2대 핵심 전략 체제로 집중 운용합니다.
> 진입 조건의 기본값은 **검증 전 기본값**입니다. 모의거래에서는 바로 동작하지만, **실거래에서는
> `config.json` 의 `strategies.<전략>.live_enabled` 를 직접 `true` 로 바꾼 전략만 신규 매수**합니다.

---

## 1. 구조

```text
순위 조회(ka10030·ka10027) ─> CandidateScanner ─> 후보 목록(순위)
                                                     │
3분봉 조회(ka10080, 완성 봉) ─> BarStore 저장 ─> SymbolSession (종목별 당일 상태)
                                                     │
                              StrategyManager ─> OPEN / CLOSE (각자 판정)
                                                     │  StrategySignal
                              LiveTradingEngine (진입 조건 검사 · 주문 · 청산) ─> 키움
```

- **전략은 신호만 냅니다.** 주문은 엔진만 냅니다 (`strategies/` 안에는 주문 호출이 없습니다).
- **전략은 서로 독립입니다.** 진입(`on_bar`)·청산(`check_exit`)·설정·통계를 전략마다 따로 가집니다.
  같은 종목에 여러 전략이 신호를 내도 주문은 한 번이고, 먼저 승인된 전략이 그 포지션을 소유해 청산까지 판정합니다.
- **완성된 3분봉만** 씁니다. 진행 중인 봉·시간외 봉·중복 봉은 `SymbolSession`이 받지 않습니다.
  키움은 체결이 없던 3분 구간의 봉을 보내지 않으므로 빈 구간 수(`gaps`)를 유동성 지표로 남깁니다.
- 실거래와 백테스트가 같은 `SymbolSession`·전략 코드를 씁니다 (`backtest/replay.py`).

### WAY 1 — OPEN 전략 (`strategies/open_strategy.py`)
장 초반(`start_time`~`end_time`, 기본 09:06~09:45)에 완성된 3분봉으로 판정합니다. 갭이 크다는 이유만으로는 사지 않습니다.

| 패턴 | 조건 (모두 충족) | 손절 기준 |
|---|---|---|
| A. 시초 범위 돌파 `orb` | 첫 `or_bars`봉 고가를 종가로 상향 돌파 · 양봉 · VWAP 위 · 거래량 ≥ 범위 봉 평균 × `orb_vol_ratio` | 돌파 봉 저가 |
| B. 시초 눌림 `pullback` | 상승폭 ≥ `pb_min_impulse_pct`% · 되돌림 `pb_min_retrace`~`pb_max_retrace` · 눌림 거래량 감소 · VWAP 지지 · 직전 봉 고가 위 양봉 | 눌림 저가 |
| C. 모멘텀 지속 `momentum` | 시가 대비 ≥ `mo_min_gain_pct`% · 최근 `mo_bars`봉 저점 상승·VWAP 위·거래량 유지 · 종가 당일 신고가 | 최근 봉 최저가 |

### WAY 2 — CLOSE 전략 (`strategies/close_strategy.py`)
장 마감 전(`start_time`~`end_time`, 기본 14:48~15:15)에 주봉(추세) → 일봉(셋업) → 3분봉(막판 매수세)의 3단계 검증 후 익일 오버나이트 스윙 진입을 판정합니다.

| 단계 | 조건 |
|---|---|
| 1. 주봉 (`analyze_weekly`) | 주봉 정배열 + 중기 이평 상승(BULLISH) 또는 명확한 추세 |
| 2. 일봉 (`analyze_daily`) | 20일선 우상향 중 돌파 셋업(BREAKOUT_SETUP) 또는 20일선 눌림 회복(PULLBACK) 또는 정배열(UPTREND) |
| 3. 3분봉 (`analyze_3min`) | VWAP 위 마감 · 당일 고가권(상단 70% 이상) 유지 · 막판 매수 우위 거래량 및 급락 없음 |

- **오버나이트 청산**: 다음 거래일 09:00부터 손절/트레일링을 감시하며, 지정 시각(`exit_time`, 기본 10:30)까지 도달 시 시장가 일괄 청산 (최대 1일 보유).

### 자금 관리 및 위험 통제 (`risk/capital_guard.py`)
- 일일 손실 한도(`max_daily_loss`) 도달 시 자동 신규 매수 차단.
- 1R 리스크 기반 포지션 사이징 및 오버나이트 총 노출 한도 제어.

패턴이 맞아도 다음이면 진입하지 않습니다: 첫 봉 없음 · 전일 일봉 없음 · 갭 과다 · 빠진 봉 과다(유동성) · 누적 거래대금 부족 ·
지수 약세 · 지수 대비 상대강도 부족 · 과열(전일 대비·VWAP 이격) · 손절폭 과다 · 저항(전일 고가·상한가)까지 여유 부족.
손절가는 손절 기준 - 1호가이며, 지수 조회에 실패하면 지수 조건은 판정하지 않습니다.

### 백테스트 (`backtest/`)
```cmd
:: 1) 과거 데이터 받기 (조회 전용, 중단 후 다시 실행하면 이어받음) → python_app\history.db
.venv\Scripts\python.exe python_appacktest\download.py all --top 150
:: 2) 재생 — 설정은 실거래와 같은 config.json. --split 날짜부터는 검증 구간으로 따로 보고
.venv\Scripts\python.exe python_appacktestun.py --split 2026-07-01
```
- 재생은 실거래와 같은 전략·세션·후보 순위·청산 규칙 코드를 씁니다 (`backtest/replay.py`).
- **미래 정보 차단**: 시각 t 의 판정에는 그 시각까지 마감된 봉만 쓰고, 신호 다음 봉의 시가에 체결합니다.
- **체결 가정**: 매수·매도 각 1호가 불리하게, 수수료·거래세 차감. 봉 안에서는 시가 → 저가 → 고가 → 종가 순(손절 우선).
- **한계**: 현재 상장 종목만(생존 편향), 받아 둔 종목 안에서만 후보를 고름, 청산을 2초 시세가 아닌 3분봉으로 판정,
  부분 체결 미반영. 검증 구간 결과를 보고 값을 고치면 그 구간은 더 이상 검증 구간이 아닙니다.

### 매도 — 기본 청산 규칙 (`strategy/exit_rules.py`)
전략이 따로 정하지 않으면 공통 규칙을 씁니다: 손절 → 트레일링 스탑 → 목표가.
- 손절가: 전략이 준 구조 기반 손절가. 없으면 매입가 × (1 - `stop_loss_rate`%).
- 트레일링: 최고가가 `trailing_activation_rate`% 이상 오른 뒤, 상승분의 `trailing_retrace_rate`%
  (최고가 1R 이상이면 `trailing_retrace_rate_1r`%)를 되돌리면 청산. 1R = 매입가 - 손절가.
- 전략별로 `config.json`의 `strategies.<전략>.exit`에서 위 값을 덮어쓸 수 있습니다.
- 오버나이트 보유 신호(`hold_overnight`)로 산 종목은 매수한 날의 장 마감 일괄 청산과 다음 날 시초 청산에서 제외되고,
  다음 거래일 장 마감에는 청산됩니다 (최대 1일 보유).

### 주요 설정 (`config.example.json`)
| 묶음 | 내용 |
|---|---|
| 최상위 (`StrategyConfig`) | 공통 청산 규칙·자금 관리: `stop_loss_rate`, `trailing_*`, `eod_liquidation*`, `order_amount_per_trade`, `max_concurrent_positions`, `max_daily_loss` |
| `scanner` | 실시간 후보 탐색: 후보 수, 주가·거래대금·등락률 범위, 순위 가중치 (기본값은 검증 전 임시값) |
| `strategies` | 전략별 설정 (`open` / `close`) — 2대 핵심 전략 설정 |
| `rule_presets` | 공통 매매 설정에 이름을 붙여 보관한 사본 |

---

## 2. 키움증권 연동 (모의거래 / 실거래)

### 1) 앱키 발급 (키움 OpenAPI 포털)
1. 키움증권 계좌 개설 → [키움 OpenAPI 포털](https://openapi.kiwoom.com)에서 REST API 사용 신청
2. **모의투자용**과 **실거래용** 앱키·시크릿키를 각각 발급 (서로 바꿔 쓰면 인증 오류)
3. 포털에서 **이 PC의 공인 IP 주소를 등록** (미등록 IP는 접속 거부)
4. 모의거래를 하려면 키움 **상시 모의투자**에도 신청되어 있어야 합니다

### 2) 앱에서 설정
1. `run.bat` 실행 → **키움 API 설정** 탭에 모의/실거래 키 입력 → **저장** → **연결 테스트**
2. 우측 상단 **[모의거래] / [실거래]** 버튼으로 모드 선택
   - 실거래로 바꾸려면 확인창에 `실거래`를 직접 입력해야 전환됩니다
   - 실거래 시작 시 매수금액·손실한도 요약을 보여주고 한 번 더 확인합니다
   - 자동매매 가동 중에는 모드를 바꿀 수 없습니다
3. **자동매매 시작** — 장 시작 전(08:00 등)에 켜 두면 09:00부터 자동으로 동작합니다

| 모드 | 서버 | 앱키 | 화면 |
|---|---|---|---|
| 모의거래 | `mockapi.kiwoom.com` | 모의투자용 | 파란 배너 |
| 실거래 | `api.kiwoom.com` | 실거래용 | 빨간 배너 ⚠ |

> 키는 이 PC의 `python_app/config.json`에만 저장되고 `.gitignore`로 Git 업로드가 차단됩니다.
> 환경변수 `KIWOOM_MOCK_APPKEY` · `KIWOOM_MOCK_SECRETKEY` · `KIWOOM_REAL_APPKEY` · `KIWOOM_REAL_SECRETKEY`가 있으면 우선 사용합니다.

### 3) 동작 방식 (`engine/live_engine.py`)
| 주기 | 하는 일 | 키움 TR |
|---|---|---|
| 3분봉 마감 4초 후 (09:03:04, 09:06:04 …) | 후보 순위 재조회 → 감시종목 3분봉 조회·기록 → 전략별 판정 → 신호 시 매수 | ka10030·ka10027 → ka10080 → kt10000 |
| 2초마다 | 앱이 매수한 종목 현재가 → 소유 전략의 청산 판정(손절 / 트레일링 / 목표가) → 시장가 매도 | ka10001 → kt10001 |
| 15초마다 | 잔고·주문가능금액 동기화, 체결 확인 | kt00018, kt00001 |
| 주문 후 30초 미체결 | 주문 취소 (매도는 다음 주기 재시도) | ka10075 → kt10003 |

### 4) 안전장치
- **원래 계좌에 있던 종목은 절대 매도하지 않습니다** — 앱이 매수한 종목만 청산
- 늦게 켜서 이미 지나간 봉의 신호로는 매수하지 않음 (신호 후 2분 이내만 진입)
- 일 손실한도(추정 실현+평가 손익) 도달 시 신규 매수 중단
- 1회 매수금액은 수수료 포함으로 넘지 않게 수량 계산, 주문가능금액 부족 시 생략
- 관리종목·최고가를 `live_state_MOCK.json` / `live_state_REAL.json`에 저장 → 재시작해도 트레일링 기준 유지
- **장 마감 일괄 청산 (기본 15:15)**: 앱 매수 종목 전량 시장가 매도 + 이후 신규 매수 금지
  - 15:20~15:30 동시호가 중 매도 주문은 15:30 종가에 체결되므로 취소하지 않음
- **전일 이월 종목**: 앱이 꺼져 청산하지 못하고 넘어온 종목은 다음 날 09:00 시장가 청산 (설정에서 끌 수 있음)

### 5) ⚠ 앱이 켜져 있어야 동작합니다
손절·트레일링·일괄청산은 **이 앱(PC)이 시세를 감시하다가 주문을 보내는 방식**입니다.
키움 REST API에는 서버에 감시주문을 맡기는 기능이 (공개 자료 기준) 없습니다.

| 상황 | 결과 |
|---|---|
| 앱 켜짐 | 손절 -2% · 트레일링 · 15:15 일괄청산 모두 작동 |
| 앱 꺼짐 / PC 절전·재부팅 / 인터넷 끊김 | **감시 중단 — 손실이 -2%를 넘어도 매도 안 됨** |
| 다시 켰을 때 | 최고가 기준 복원 후 감시 재개, 전일 이월분은 09:00 청산 |

권장 운영:
1. Windows **절전/최대절전 끄기** (전원 옵션 → 절전 모드 "안 함"), 자동 업데이트 재부팅 시간 장외로 설정
2. 보조 안전망: 영웅문 HTS **[0624] 주식 자동감시주문**에 매수된 종목의 **손실제한(-2.5% 등 앱보다 약간 넓게)** 을 등록
   - 0624는 키움 서버가 감시하므로 HTS·PC를 꺼도 작동 (도움말 기준 최대 90일)
   - 앱이 먼저 팔면 잔고가 0이 되어 감시 조건은 자동 삭제됨
   - 0624의 트레일링은 "최고가 대비 N틱/%" 방식이라 앱의 "상승분의 N%"와 다르고, 시각 기준 일괄청산 기능은 없음
3. 장기적으로는 항상 켜진 클라우드 서버(VPS)에서 엔진만 돌리는 구조 권장 (SaaS 전환 시 필수)

### 6) 실거래 전 권장 순서
1. 모의거래로 최소 1~2주 운영 → `실행 로그`·`당일 매매` 탭으로 신호·체결 확인
2. 실거래 첫 주는 **1회 매수금액을 작게** (예: 10~30만원), 일 손실한도도 작게
3. 체결 품질(시장가 슬리피지)과 로그를 확인한 뒤 금액 상향

---

## 3. 프로젝트 폴더 구조

```
Korea3minTrader/
├── setup.bat / run.bat / run_console.bat     # 설치 · 실행 · 콘솔 실행(문제 확인용)
├── autostart_install.bat / autostart_uninstall.bat
├── tools/autostart.ps1                       # 작업 스케줄러 등록 (평일 08:00, 절전 깨우기)
└── python_app/
    ├── main.py                 # GUI — 모의/실거래 전환, 키·알림·자동실행 설정, 현황판 (--autostart 지원)
    ├── app_config.py           # config.json 로드/저장 (앱키·텔레그램 토큰 포함, 공유 금지)
    ├── engine/live_engine.py   # 실시간 자동매매 루프 (분봉→신호→주문→청산→알림)
    ├── notify/telegram.py      # 텔레그램 알림 (백그라운드 전송)
    ├── strategies/base_strategy.py      # StrategySignal · SymbolSession · BaseStrategy (전략 공통 인터페이스)
    ├── strategies/candidate_scanner.py  # 실시간 후보 탐색·순위
    ├── strategies/strategy_manager.py   # 전략별 호출·중복 신호 차단·전략별 통계
    ├── strategies/indicators.py         # 공용 계산 (호가 단위 등)
    ├── backtest/bar_store.py            # 3분봉·후보 기록 (bars.db)
    ├── backtest/download.py             # 과거 일봉·3분봉·지수 내려받기 (history.db)
    ├── backtest/replay.py               # 재생 엔진 (실거래와 같은 전략 코드, 다음 봉 시가 체결)
    ├── backtest/metrics.py / run.py     # 성과 지표 · 실행
    ├── strategies/open_strategy.py      # WAY 1 OPEN: 시초 범위 돌파 · 시초 눌림 · 모멘텀 지속
    ├── strategy/exit_rules.py  # 공통 청산 규칙: 손절 / 트레일링 (1R 미만 50%, 1R 이상 30%) / 목표가
    ├── broker/kiwoom_rest.py   # 키움증권 REST API (모의 mockapi / 실거래 api.kiwoom.com)
    ├── broker/kiwoom_condition.py  # 키움 조건검색 (웹소켓: LOGIN · CNSRLST · CNSRREQ) — 현재 미사용
    ├── broker/paper.py         # 오프라인 모의 브로커 (테스트용)
    ├── models/types.py         # 데이터 모델 · 전략 설정값
    ├── db/storage.py           # SQLite 기록
    ├── tests/                  # pytest (가짜 키움 서버 포함)
    ├── logs/                   # 날짜별 실행 로그 (자동 생성)
    ├── config.example.json     # 설정 템플릿 → 첫 실행 시 config.json 생성
    ├── requirements.txt        # 실행용 패키지
    ├── requirements-dev.txt    # 테스트·EXE 빌드용
    └── pyinstaller.spec
```

---

## 4. 개발자용

```cmd
:: 테스트
.venv\Scripts\python.exe -m pip install -r python_app\requirements-dev.txt
.venv\Scripts\python.exe -m pytest python_app\tests -q

:: EXE 빌드 (python_app 폴더에서)
cd python_app
..\.venv\Scripts\pyinstaller.exe pyinstaller.spec
```

설치·사용 방법은 최상위 `README.md`를 보세요.
