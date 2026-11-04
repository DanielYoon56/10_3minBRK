"""
Korea3MinBreakoutTrader - 20시 매집 확인 스캔 (장 마감 후, 앱과 별도 실행)

- 오늘 확정 일봉으로 매집 종목(① 거래량·캔들 OR ② 외국인 AND 기관 순매수)을 골라
  콘솔에 표시하고 텔레그램으로 보낸다. 기준 수치는 config.json 의 screener 설정 그대로.
- 결과는 screen_다음평일.json 으로 저장 → 다음 날 아침 앱이 감시종목으로 그대로 재사용
- 평일 20:00 작업 스케줄러 등록: evening_scan_install.bat

  python python_app/evening_scan.py [--pause] [--no-telegram] [--force]
"""

import argparse
import logging
import os
import sys
from datetime import datetime, time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python_app.app_config import APP_DIR, load_config, kiwoom_keys, strategy_config_from, telegram_settings
from python_app.broker.kiwoom_condition import KiwoomConditionClient
from python_app.broker.kiwoom_rest import KiwoomRestBroker
from python_app.engine.live_engine import kst_now
from python_app.models.types import TradingMode
from python_app.notify.telegram import TelegramNotifier
from python_app.screener.accumulation import AccumulationScreener, ScreenerConfig, ScreenResult

LOG_DIR = os.path.join(APP_DIR, "logs")
logger = logging.getLogger("Korea3MinBreakoutTrader.EveningScan")


def _fmt_date(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d


def format_result(res: ScreenResult, mode_label: str) -> str:
    n1 = sum("①" in h.reasons for h in res.hits)
    n2 = sum(any(r.startswith("②") for r in h.reasons) for h in res.hits)
    lines = [f"📋 20시 매집 스캔 [{mode_label}] 기준일 {_fmt_date(res.prev_date)}",
             f"→ {_fmt_date(res.trade_date)} 감시종목 {len(res.hits)}개 (후보 {res.candidates_checked}) "
             f"[①거래량·캔들 {n1} / ②수급 {n2}]"]
    if not res.hits:
        lines.append("조건을 만족한 종목이 없습니다.")
    for i, h in enumerate(res.hits, 1):
        lines.append(f"{i}. {h.name}({h.symbol}) {'+'.join(h.reasons)}")
        if h.detail:
            lines.append(f"   {h.detail}")
    for e in res.errors[:3]:
        lines.append(f"⚠ {e}")
    return "\n".join(lines)


def _chunks(text: str, limit: int = 3800):
    """텔레그램 4,096자 제한 → 줄 단위로 나눠 보냄"""
    buf = ""
    for line in text.split("\n"):
        if buf and len(buf) + len(line) + 1 > limit:
            yield buf
            buf = ""
        buf = f"{buf}\n{line}" if buf else line
    if buf:
        yield buf


def run(send_telegram: bool = True, force: bool = False) -> int:
    now = kst_now()
    if now.weekday() >= 5 and not force:
        logger.info("주말 → 20시 매집 스캔 건너뜀")
        return 0
    if now.time() < dtime(15, 40) and not force:
        # PC가 꺼져 있다 다음 날 아침 늦게 실행되면 오늘 봉이 미확정 → 잘못된 날짜로 저장되므로 건너뜀
        logger.info("장 마감(15:40) 전 → 20시 매집 스캔 건너뜀 (오늘 일봉 미확정)")
        return 0
    cfg = load_config()
    mode = TradingMode(cfg["trading_mode"])
    app_key, secret = kiwoom_keys(cfg, mode)
    broker = KiwoomRestBroker(app_key, secret, mode=mode, config=strategy_config_from(cfg))
    enabled, token, chat = telegram_settings(cfg)
    notifier = TelegramNotifier(token, chat, enabled=enabled and send_telegram)
    if not broker.connect():
        msg = f"⚠ 20시 매집 스캔 실패: 키움 {broker.mode_label} 접속 실패 — 앱키/시크릿키를 확인하세요"
        logger.error(msg)
        if notifier.active:
            notifier.send_now(msg)
        return 1

    screener = AccumulationScreener(
        broker, ScreenerConfig.from_dict(cfg.get("screener")), state_dir=APP_DIR,
        log=lambda lv, m: getattr(logger, lv if lv in ("info", "warning", "error") else "info")(m),
        condition_client_factory=lambda: KiwoomConditionClient(broker, raw_log_dir=LOG_DIR))
    logger.info("🔍 20시 매집 스캔 시작 (오늘 확정 일봉 기준, 코스피·코스닥 전체)")
    try:
        res = screener.run(now, use_cache=False, after_close=True)
    except Exception as e:
        logger.exception("20시 매집 스캔 오류")
        if notifier.active:
            notifier.send_now(f"⚠ 20시 매집 스캔 실패: {e}")
        return 1

    text = format_result(res, broker.mode_label)
    print("\n" + "=" * 60 + "\n" + text + "\n" + "=" * 60, flush=True)
    if notifier.active:
        ok = all(notifier.send_now(part)[0] for part in _chunks(text))
        logger.info("텔레그램 전송 " + ("완료" if ok else "실패"))
    elif send_telegram:
        logger.warning("텔레그램이 꺼져 있거나 봇 토큰/chat_id 가 비어 있어 전송하지 않음")
    return 0


def main():
    parser = argparse.ArgumentParser(description="20시 매집 확인 스캔")
    parser.add_argument("--pause", action="store_true", help="끝난 뒤 Enter 를 누를 때까지 창 유지")
    parser.add_argument("--no-telegram", action="store_true", help="텔레그램 전송 안 함")
    parser.add_argument("--force", action="store_true", help="주말·장 마감 전에도 실행")
    args = parser.parse_args()

    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(os.path.join(LOG_DIR, f"{datetime.now():%Y%m%d}.log"), encoding="utf-8")],
    )
    code = run(send_telegram=not args.no_telegram, force=args.force)
    if args.pause:
        try:
            input("\nEnter 를 누르면 창이 닫힙니다...")
        except EOFError:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
