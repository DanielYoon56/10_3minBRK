"""
Korea3MinBreakoutTrader - 텔레그램 알림

설정 (앱 '알림·자동실행' 탭에서 입력)
  1. 텔레그램에서 @BotFather → /newbot → 봇 토큰 발급
  2. 만든 봇에게 아무 메시지나 보낸 뒤 앱에서 [chat_id 자동 찾기]
  3. [테스트 메시지] 로 확인

동작
  - 전송은 백그라운드 스레드 큐에서 처리 → 자동매매 루프를 절대 막지 않음
  - 실패 시 최대 3회 재시도, 텔레그램 429(전송 제한) 시 retry_after 만큼 대기
  - 토큰/chat_id 가 비어 있거나 꺼져 있으면 아무것도 하지 않음
"""

import logging
import queue
import threading
import time
from typing import List, Optional, Tuple

import requests

logger = logging.getLogger("Korea3MinBreakoutTrader.Telegram")

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    def __init__(self, bot_token: str = "", chat_id: str = "", enabled: bool = True,
                 prefix: str = "", session: Optional[requests.Session] = None):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.enabled = enabled
        self.prefix = prefix
        self.session = session or requests.Session()
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=500)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.bot_token and self.chat_id)

    # ------------------------------------------------------------------ 비동기 전송
    def notify(self, text: str):
        """큐에 넣고 즉시 반환 (스레드 안전)"""
        if not self.active:
            return
        msg = f"{self.prefix} {text}".strip() if self.prefix else text
        try:
            self._q.put_nowait(msg[:4000])
        except queue.Full:
            logger.warning("텔레그램 큐가 가득 차 알림을 버렸습니다")
            return
        self._ensure_thread()

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="telegram", daemon=True)
        self._thread.start()

    def _worker(self):
        while not self._stop.is_set():
            try:
                msg = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            self.send_now(msg)

    def flush(self, timeout: float = 5.0):
        """종료 직전 남은 알림 전송 대기"""
        end = time.monotonic() + timeout
        while not self._q.empty() and time.monotonic() < end:
            time.sleep(0.1)

    # ------------------------------------------------------------------ 동기 전송
    def send_now(self, text: str) -> Tuple[bool, str]:
        if not (self.bot_token and self.chat_id):
            return False, "봇 토큰과 chat_id 를 입력하세요"
        url = API.format(token=self.bot_token, method="sendMessage")
        for attempt in range(3):
            try:
                r = self.session.post(url, json={"chat_id": self.chat_id, "text": text,
                                                 "disable_web_page_preview": True}, timeout=10)
                data = r.json() if r.content else {}
                if r.status_code == 200 and data.get("ok"):
                    return True, "전송 성공"
                if r.status_code == 429:
                    wait = int(data.get("parameters", {}).get("retry_after", 3))
                    time.sleep(min(wait, 30))
                    continue
                desc = data.get("description", r.text[:200])
                logger.error("텔레그램 전송 실패 [%s]: %s", r.status_code, desc)
                if r.status_code in (400, 401, 403, 404):
                    return False, desc        # 토큰/chat_id 오류 → 재시도 무의미
            except requests.RequestException as e:
                logger.error("텔레그램 통신 오류: %s", e)
                desc = str(e)
            time.sleep(1.5 * (attempt + 1))
        return False, desc

    def find_chat_ids(self) -> List[Tuple[str, str]]:
        """봇에게 최근 메시지를 보낸 대화 목록 → [(chat_id, 이름)]"""
        if not self.bot_token:
            return []
        try:
            r = self.session.get(API.format(token=self.bot_token, method="getUpdates"), timeout=10)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            logger.error("getUpdates 실패: %s", e)
            return []
        found = {}
        for upd in data.get("result", []):
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                name = chat.get("title") or " ".join(
                    x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username", "")
                found[str(chat["id"])] = name
        return list(found.items())
