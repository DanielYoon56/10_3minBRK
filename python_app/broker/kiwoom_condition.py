"""
Korea3MinBreakoutTrader - 키움 조건검색 (영웅문 HTS에서 만든 조건식 결과 조회)

조건검색은 REST가 아니라 실시간 웹소켓으로만 제공된다.
  실거래: wss://api.kiwoom.com:10000/api/dostk/websocket
  모의  : wss://mockapi.kiwoom.com:10000/api/dostk/websocket

프로토콜 (trnm 필드로 구분)
  LOGIN    → {"trnm":"LOGIN","token":<REST 접근토큰>}            응답 return_code 0 = 성공
  CNSRLST  → {"trnm":"CNSRLST"}                                 응답 data = [[seq, 조건식명], ...]
  CNSRREQ  → {"trnm":"CNSRREQ","seq":..,"search_type":"0","stex_tp":"K","cont_yn":"N","next_key":""}
             응답 data = 종목 목록 (+ cont_yn / next_key 로 연속조회)
  PING     ← 서버가 보냄 → 받은 그대로 되돌려 보내야 연결 유지

준비: 영웅문 HTS [0150] 조건검색에서 조건식을 만들고 '서버에 저장'해야 목록에 나온다.

※ 공개 자료마다 결과 항목의 키 이름이 달라(예: "9001"/"jmcode"/"stk_cd") 여러 키를 모두 인식하고,
  매일 첫 응답 원본을 logs/condition_raw_YYYYMMDD.json 에 남겨 실제 형식을 확인할 수 있게 했다.
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("Korea3MinBreakoutTrader.Condition")

CODE_KEYS = ("9001", "jmcode", "stk_cd", "code", "shcode")
NAME_KEYS = ("302", "jmname", "stk_nm", "name", "hname")


class ConditionSearchError(Exception):
    pass


def _clean_code(v: Any) -> str:
    s = str(v or "").strip()
    if len(s) == 7 and s[0].isalpha():
        s = s[1:]
    s = s.split("_")[0]
    return s if len(s) == 6 and s.isalnum() else ""


def parse_items(data: Any) -> List[Dict[str, str]]:
    """CNSRREQ data → [{"symbol","name"}] (dict·list·문자열 형식 모두 처리)"""
    out: List[Dict[str, str]] = []
    if isinstance(data, dict):
        data = data.get("list") or data.get("items") or [data]
    for it in data or []:
        code, name = "", ""
        if isinstance(it, dict):
            for k in CODE_KEYS:
                if it.get(k):
                    code = _clean_code(it[k])
                    if code:
                        break
            for k in NAME_KEYS:
                if it.get(k):
                    name = str(it[k]).strip()
                    break
        elif isinstance(it, (list, tuple)) and it:
            code = _clean_code(it[0])
            name = str(it[1]).strip() if len(it) > 1 else ""
        elif isinstance(it, str):
            code = _clean_code(it)
        if code:
            out.append({"symbol": code, "name": name or code})
    return out


class KiwoomConditionClient:
    """동기식 조건검색 클라이언트 (요청 1회 → 결과 반환 → 연결 종료)"""

    def __init__(self, broker, raw_log_dir: Optional[str] = None, timeout: float = 15.0, ws_factory=None):
        self.broker = broker
        self.timeout = timeout
        self.raw_log_dir = raw_log_dir
        self._ws_factory = ws_factory      # 테스트용 주입
        self._ws = None

    @property
    def ws_url(self) -> str:
        host = urlparse(self.broker.base_url).hostname or "api.kiwoom.com"
        return f"wss://{host}:10000/api/dostk/websocket"

    # ------------------------------------------------------------------ 연결
    def _open(self):
        if not self.broker._ensure_token():
            raise ConditionSearchError("접근토큰 발급 실패")
        if self._ws_factory:
            ws = self._ws_factory(self.ws_url)
        else:
            import websocket   # websocket-client
            ws = websocket.create_connection(self.ws_url, timeout=self.timeout)
        self._ws = ws
        ws.send(json.dumps({"trnm": "LOGIN", "token": self.broker.access_token}))
        msg = self._recv_until(lambda m: m.get("trnm") == "LOGIN")
        if str(msg.get("return_code", 0)) != "0":
            raise ConditionSearchError(f"조건검색 로그인 실패: {msg.get('return_msg', msg)}")

    def close(self):
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def __enter__(self):
        self._open()
        return self

    def __exit__(self, *exc):
        self.close()

    def _recv_until(self, match) -> Dict[str, Any]:
        end = time.monotonic() + self.timeout
        while time.monotonic() < end:
            raw = self._ws.recv()
            if not raw:
                raise ConditionSearchError("서버가 연결을 종료했습니다")
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("trnm") == "PING":
                self._ws.send(raw if isinstance(raw, str) else json.dumps(msg))
                continue
            if match(msg):
                return msg
        raise ConditionSearchError("조건검색 응답 시간 초과")

    def _request(self, payload: Dict[str, Any], match) -> Dict[str, Any]:
        if self._ws is None:
            self._open()
        self._ws.send(json.dumps(payload))
        msg = self._recv_until(match)
        if str(msg.get("return_code", 0)) not in ("0", ""):
            raise ConditionSearchError(f"{payload.get('trnm')} 실패: {msg.get('return_msg', msg)}")
        return msg

    # ------------------------------------------------------------------ 기능
    def list_conditions(self) -> List[Tuple[str, str]]:
        """[(seq, 조건식명)] — HTS에서 서버 저장한 조건식"""
        msg = self._request({"trnm": "CNSRLST"}, lambda m: m.get("trnm") == "CNSRLST")
        out = []
        for row in msg.get("data") or []:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                out.append((str(row[0]).strip(), str(row[1]).strip()))
            elif isinstance(row, dict):
                seq = row.get("seq") or row.get("0")
                name = row.get("name") or row.get("1") or row.get("cond_nm")
                if seq is not None:
                    out.append((str(seq).strip(), str(name or "").strip()))
        return out

    def find_seq(self, name: str) -> str:
        conds = self.list_conditions()
        for seq, nm in conds:
            if nm == name:
                return seq
        for seq, nm in conds:   # 앞뒤 공백·대소문자 차이 허용
            if nm.replace(" ", "").lower() == name.replace(" ", "").lower():
                return seq
        names = ", ".join(nm for _, nm in conds) or "(없음 — HTS에서 '서버 저장' 했는지 확인)"
        raise ConditionSearchError(f"조건식 '{name}' 을 찾을 수 없습니다. 저장된 조건식: {names}")

    def search(self, name: str, max_pages: int = 10) -> List[Dict[str, str]]:
        """조건식 이름 → 현재 해당 종목 [{"symbol","name"}] (일회성 조회)"""
        seq = self.find_seq(name)
        items: List[Dict[str, str]] = []
        cont, key = "N", ""
        for page in range(max_pages):
            msg = self._request(
                {"trnm": "CNSRREQ", "seq": seq, "search_type": "0", "stex_tp": "K",
                 "cont_yn": cont, "next_key": key},
                lambda m: m.get("trnm") == "CNSRREQ" and str(m.get("seq", seq)).strip() == seq)
            if page == 0:
                self._log_raw(name, msg)
            items.extend(parse_items(msg.get("data")))
            if str(msg.get("cont_yn", "N")).upper() != "Y" or not msg.get("next_key"):
                break
            cont, key = "Y", str(msg["next_key"])
        seen, uniq = set(), []
        for it in items:
            if it["symbol"] not in seen:
                seen.add(it["symbol"])
                uniq.append(it)
        logger.info("조건식 '%s'(seq %s) → %d종목", name, seq, len(uniq))
        return uniq

    def _log_raw(self, name: str, msg: Dict[str, Any]):
        if not self.raw_log_dir:
            return
        try:
            os.makedirs(self.raw_log_dir, exist_ok=True)
            p = os.path.join(self.raw_log_dir, f"condition_raw_{datetime.now():%Y%m%d}.json")
            sample = dict(msg)
            if isinstance(sample.get("data"), list):
                sample["data"] = sample["data"][:5]
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"condition": name, "response_sample": sample}, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
