# Korea3MinBreakoutTrader (국내 주식 첫 3분봉 돌파 자동매매 시스템)

Windows 데스크톱 독립형 자동매매 애플리케이션 (Python 3.11+ / PySide6 / SQLite / Kiwoom REST API)

---

## 1. 개요 및 트레이딩 전략 정의

전략 이름: `First3MinuteCandleBreakout`

### 원본 강세 수식 분석:
```text
crossup(C, valuewhen(1, date!=date(1), C(1))) && V > valuewhen(1, date!=date(1), V(1))
```

### HTS 수식과 본 시스템 구현 간 차이점:
- **HTS 인덱싱 오류 방지**: 키움증권 영웅문 등의 수식관리자에서 `date != date(1)` 조건은 당일 첫 봉에서 참이 되지만, 그 시점에서 `C(1)`이나 `V(1)`을 참조하면 **당일 첫 봉이 아니라 전일 종가(전일 마지막 봉)**를 참조하는 논리적 함정이 발생합니다.
- **본 시스템의 명확한 정의**:

#### 매수 — 3분봉 시가돌파 + 거래량 충족
  1. 매 거래일 첫 번째 3분봉(09:00~09:03 KST)을 확정합니다.
  2. 첫 3분봉의 **시가**를 `breakout_level`, 거래량을 `first_bar_volume`으로 저장합니다. (첫 봉 자체에서는 신호 없음)
  3. 이후 봉(09:03 이후)에서:
     - `cross_up = previous_close <= breakout_level AND current_close > breakout_level`
     - `volume_confirmed = current_bar_volume > first_bar_volume × min_volume_ratio` (기본 0.7 → 첫 봉 거래량의 70% 초과)
     - `bullish_signal = cross_up AND volume_confirmed`
  4. 지정 시간대(기본 09:03 ~ 10:00) 내, 종목당 당일 1회만 진입합니다.
  5. HTS 수식 표현: `crossup(C, valuewhen(1, date!=date(1), O)) && V > valuewhen(1, date!=date(1), V)`

#### 매도 — 손절 -2% / 트레일링 스탑 15% (`strategy/exit_rules.py`)
  | 우선순위 | 조건 | 계산 |
  |---|---|---|
  | 1 | 손절 | 현재가 ≤ 진입가 × (1 − 2%) |
  | 2 | 트레일링 스탑 | 최고가가 진입가 대비 **+1% 이상** 오른 뒤부터: 현재가 ≤ 최고가 − (최고가 − 진입가) × 15% |
  | 3 | 고정 익절 (옵션) | `take_profit_rate > 0`일 때만. 기본 0 = 미사용 |

  예) 진입 10,000원 → 최고 11,000원(상승분 1,000) → **10,850원 이하**에서 전량 청산.
  최고가가 +1%(10,100원)에 한 번도 닿지 않았으면 트레일링은 작동하지 않고 손절선(9,800원)만 유효합니다.
  예) 진입 10,000원 → 최고 10,080원(+0.8%) → 다시 10,000원: 트레일링 미발동 → 보유 유지

#### 주요 설정 (`config.example.json` / `StrategyConfig`)
  | 키 | 기본값 | 설명 |
  |---|---|---|
  | `breakout_reference` | `"open"` | `"close"`로 바꾸면 구버전(첫 봉 종가 돌파) |
  | `min_volume_ratio` | `0.7` | 돌파 봉 거래량 > 첫 봉 거래량 × 배수 (70% 초과) |
  | `stop_loss_rate` | `2.0` | 손절 % |
  | `trailing_retrace_rate` | `15.0` | 상승분 되돌림 % (0 = 끔) |
  | `trailing_activation_rate` | `1.0` | 트레일링 발동: 진입 후 최고가가 +1% 이상 올라야 15% 룰 적용 |
  | `take_profit_rate` | `0.0` | 고정 익절 % (0 = 끔) |
  | `use_first_bar_low_stop` | `false` | 첫 봉 저가 이탈 즉시 손절 병행 |

#### 추가 매수 전략 + AND/OR 조합 (`strategy/combiner.py`, `intraday.py`, `smc.py`)
  GUI [전략 설정] → '매수 전략'에서 여러 개를 고르고 조합을 정한다 (`entry_strategies`, `entry_logic`).

  | 키 | 전략 | 신호 | 진입 |
  |---|---|---|---|
  | `open_breakout` | 첫 봉 시가 돌파 (기존) | 위 설명 | 시장가 |
  | `orb_high` | A. 첫 봉 고가 돌파 | 직전 봉 종가 ≤ 첫 봉 고가 < 현재 봉 종가 + 거래량 조건 | 시장가 |
  | `vwap_pullback` | B. VWAP 눌림 반등 | 고가가 VWAP +1% 이상 간 뒤, 저가가 VWAP ±0.3% 안으로 눌리고 양봉·VWAP 위 마감 | 시장가 |
  | `prev_high` | C. 전일 고가 돌파 | 직전 봉 종가 ≤ 전일 고가 < 현재 봉 종가 + 거래량 조건 (전일 고가는 일봉 ka10081) | 시장가 |
  | `day_swing_high` | D. 당일 전고점 돌파 | 가장 최근 당일 스윙 고점(좌우 `dsh_swing_len`봉보다 높은 고가) 위로 종가 마감 + 거래량 조건. 청산: 체결가 -`dsh_stop_rate`(0.5)% 손절, 최고가 +`dsh_trail_activation_rate`(0.5)% 이후 상승분의 `dsh_trail_retrace_rate`(50)% 되돌림 시 청산 (전역 손절·트레일링과 별개). 시간대 `dsh_start_time`~`dsh_end_time` | 시장가 |
  | `smc_fvg` | SMC CHoCH → FVG | 하락 구조(스윙 저점 아래 마감) 뒤 스윙 고점 위 마감 = CHoCH → 그 뒤 처음 완성되는 상승 FVG | **FVG 중간값 지정가** |

  - **OR**: 하나라도 신호가 나면 진입 (같은 봉에 여럿이면 위 표 순서가 우선).
  - **AND**: 선택한 전략이 **당일 모두** 한 번 이상 신호를 낸 시점(마지막 신호 봉)에 진입. 진입 방식·손절·목표는 마지막 신호 것을 쓴다. 신호가 크게 줄어든다.
  - 시간대: A·C·시가 돌파 = `trade_start_time`~`trade_end_time`, B = `trade_start_time`~`vwap_end_time`, SMC = `smc_start_time`~`smc_end_time`. 엔진은 가장 늦은 마감까지 3분봉을 조회한다.
  - 종목당 진입은 하루 1회 (`allow_reentry_same_day=false`). 엔진을 다시 켜도 그날 청산한 종목은 다시 사지 않는다.

  **SMC 세부** (봉은 당일 3분봉만 사용)
  - 스윙 고/저점: 좌우 `smc_swing_len`(2)봉보다 높/낮은 봉. 하락 구조 없이 위로 깨는 것은 BOS 로 보고 진입하지 않는다.
  - CHoCH 이후 **첫 FVG 하나만** 본다. 이 FVG 가 손절폭 조건을 못 맞추면 그 CHoCH 는 건너뛴다.
  - 진입가 = FVG 중간값(호가 단위 내림), 손절가 = 보호 저점(마지막 하락 전환 ~ CHoCH 사이 최저가) − 1호가 (`smc_stop_mode="fvg"` 면 FVG 하단 − 1호가)
  - 목표가 = 진입가 + (진입가 − 손절가) × `smc_rr` (3 = 3R, 4 = 4R). 손절폭이 `smc_min_risk_pct`(0.3%) 미만·`smc_max_risk_pct`(3%) 초과면 생략.
  - 지정가는 `smc_fvg_expiry_bars`(10봉 = 30분) 안에 체결 안 되면 취소, 체결 전 현재가가 손절가 이하로 내려가도 취소. 부분 체결 후 유효시간이 끝나면 잔량만 취소.
  - 청산: 전략 손절가 / 목표가로만 (`smc_use_trailing=true` 면 트레일링도 적용), 장 마감 일괄 청산은 공통.

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
| 3분봉 마감 4초 후 (09:03:04, 09:06:04 …) | 감시종목 3분봉 조회 → 전략 판정 → 신호 시 시장가 매수 | ka10080 → kt10000 |
| 2초마다 | 앱이 매수한 종목 현재가 → 손절 -2% / 트레일링(+1% 이상 상승 후 15%) → 시장가 매도 | ka10001 → kt10001 |
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
| 앱 켜짐 | 손절 -2% · 트레일링 15% · 15:15 일괄청산 모두 작동 |
| 앱 꺼짐 / PC 절전·재부팅 / 인터넷 끊김 | **감시 중단 — 손실이 -2%를 넘어도 매도 안 됨** |
| 다시 켰을 때 | 최고가 기준 복원 후 감시 재개, 전일 이월분은 09:00 청산 |

권장 운영:
1. Windows **절전/최대절전 끄기** (전원 옵션 → 절전 모드 "안 함"), 자동 업데이트 재부팅 시간 장외로 설정
2. 보조 안전망: 영웅문 HTS **[0624] 주식 자동감시주문**에 매수된 종목의 **손실제한(-2.5% 등 앱보다 약간 넓게)** 을 등록
   - 0624는 키움 서버가 감시하므로 HTS·PC를 꺼도 작동 (도움말 기준 최대 90일)
   - 앱이 먼저 팔면 잔고가 0이 되어 감시 조건은 자동 삭제됨
   - 0624의 트레일링은 "최고가 대비 N틱/%" 방식이라 앱의 "상승분의 15%"와 다르고, 시각 기준 일괄청산 기능은 없음
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
    ├── strategy/breakout.py    # 3분봉 시가돌파 매수 판정
    ├── strategy/intraday.py    # A 첫 봉 고가 / B VWAP 눌림 / C 전일 고가 돌파
    ├── strategy/smc.py         # SMC CHoCH → 첫 FVG 중간값 지정가 (3R/4R)
    ├── strategy/combiner.py    # 매수 전략 AND/OR 조합
    ├── strategy/exit_rules.py  # 손절 -2% / 트레일링 15%
    ├── broker/kiwoom_rest.py   # 키움증권 REST API (모의 mockapi / 실거래 api.kiwoom.com)
    ├── broker/kiwoom_condition.py  # 키움 조건검색 (웹소켓: LOGIN · CNSRLST · CNSRREQ)
    ├── screener/accumulation.py    # 감시종목 선정: 조건식 AND (① 거래량·캔들 OR ② 외국인 AND 기관 순매수)
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
