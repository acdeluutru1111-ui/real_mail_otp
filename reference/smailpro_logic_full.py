# ⚠️ REFERENCE ONLY - DO NOT IMPORT IN PRODUCTION - Contains sensitive logging
#
# This file is kept for reference and documentation purposes only.
# It contains verbose logging that may expose sensitive data and should
# NEVER be imported or used in the production backend.
#
# For production use, see:
# - app/integrations/smailpro.py (SmailPro adapter)
# - app/integrations/sonjj.py (Sonjj adapter)
# - app/integrations/http_client.py (HTTP client with proper redaction)
#
# ═══════════════════════════════════════════════════════════════════

"""
smailpro_logic_full.py — Module tổng hợp logic SmailPro: tạo mail, đọc inbox, đọc chi tiết message.

Tổng hợp từ tempmail.py (resona_tool) + smailpro.py (invoi).
Sử dụng trực tiếp, không phụ thuộc file khác.

═══════════════════════════════════════════════════════════════════
LUỒNG HOÀN CHỈNH:
═══════════════════════════════════════════════════════════════════

  Bước 1: Tạo email tạm
      mail = SmailPro(cookies={...})
      email = mail.create()                    # → {"address", "key", "timestamp"}

  Bước 2: Gửi verification đến email đó (phía caller)

  Bước 3: Poll chờ email đến
      msg = mail.wait_for_message(email, timeout=120)

  Bước 4: Đọc inbox + chi tiết
      payload, inbox = mail.get_inbox(email)
      detail = mail.get_message_detail(mid, payload)

  Bước 5: Trích xuất code/link/token từ nội dung
      code = SmailPro.extract_otp_code(text)
      token = SmailPro.extract_verification_token(text)
      links = SmailPro.extract_links(text)

═══════════════════════════════════════════════════════════════════
CÁCH DỤNG NHANH:
═══════════════════════════════════════════════════════════════════

  # ── Cách 1: Dùng class (recommended) ──
  from smailpro_logic_full import SmailPro

  mail = SmailPro(cookies={"XSRF-TOKEN": "...", "sonjj_session": "..."})
  email = mail.create()
  print(f"Email: {email['address']}")

  # ... gửi verification đến email['address'] ...

  msg = mail.wait_for_message(email, timeout=120)
  code = SmailPro.extract_otp_code(msg.get("body", ""))
  print(f"Code: {code}")


  # ── Cách 2: Dùng hàm nhanh ──
  from smailpro_logic_full import quick_create, quick_wait_code

  email = quick_create(cookies={...})
  code = quick_wait_code(email, cookies={...}, timeout=120)


  # ── Cách 3: Đọc inbox chi tiết ──
  from smailpro_logic_full import SmailPro

  mail = SmailPro(cookies={...})
  email = mail.create()

  # ... chờ email đến ...

  payload, messages = mail.get_inbox(email)
  for msg in messages:
      detail = mail.get_message_detail(msg["mid"], payload)
      print(detail["subject"], detail["body"][:200])

═══════════════════════════════════════════════════════════════════
"""

import json
import logging
import os
import re
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

# ═══════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════

SMAILPRO_BASE = "https://smailpro.com"
SMAILPRO_CREATE_URL = f"{SMAILPRO_BASE}/app/create"
SMAILPRO_INBOX_URL = f"{SMAILPRO_BASE}/app/inbox"

SONJJ_API_BASE = "https://api.sonjj.com"
SONJJ_INBOX_URL = f"{SONJJ_API_BASE}/v1/temp_outlook/inbox"
SONJJ_MESSAGE_URL = f"{SONJJ_API_BASE}/v1/temp_outlook/message"

# Mapping domain → sonjj endpoint prefix
_DOMAIN_ENDPOINT_MAP = {
    "outlook.com": "temp_outlook",
    "hotmail.com": "temp_outlook",
    "live.com": "temp_outlook",
    "msn.com": "temp_outlook",
    "gmail.com": "temp_gmail",
    "googlemail.com": "temp_gmail",
    "yahoo.com": "temp_yahoo",
    "ymail.com": "temp_yahoo",
    "mail.ru": "temp_mailru",
    "icloud.com": "temp_icloud",
    "me.com": "temp_icloud",
    "mac.com": "temp_icloud",
    # Custom domains từ smailpro
    "spyboys.com": "temp_other",
    "spyboy.net": "temp_other",
    "spyboy.org": "temp_other",
    "nqminh.com": "temp_other",
}

def _get_sonjj_endpoint(email_address: str) -> tuple:
    """
    Xác định sonjj API endpoint dựa trên domain của email.

    Returns:
        Tuple (inbox_url, message_url)
    """
    domain = email_address.split("@")[-1].lower() if "@" in email_address else ""
    endpoint = _DOMAIN_ENDPOINT_MAP.get(domain, "temp_other")  # Default: temp_other cho custom domains
    inbox_url = f"{SONJJ_API_BASE}/v1/{endpoint}/inbox"
    message_url = f"{SONJJ_API_BASE}/v1/{endpoint}/message"
    logger.info("[SONJJ] Domain '%s' → endpoint '%s'", domain, endpoint)
    return inbox_url, message_url

DEFAULT_TIMEOUT = 60
DEFAULT_POLL_INTERVAL = 3
DEFAULT_POLL_TIMEOUT = 120
REQUEST_TIMEOUT = 30

# Cookie file mặc định
_COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smail_cookies.json")

logger = logging.getLogger("smailpro")


# ═══════════════════════════════════════════════════════════════════
# HEADERS
# ═══════════════════════════════════════════════════════════════════

def _smailpro_headers(origin: bool = False) -> Dict[str, str]:
    """Headers chuẩn cho request đến smailpro.com."""
    h = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.8",
        "content-type": "application/json",
        "priority": "u=1, i",
        "referer": f"{SMAILPRO_BASE}/temporary-email",
        "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if origin:
        h["origin"] = SMAILPRO_BASE
    return h


def _sonjj_headers() -> Dict[str, str]:
    """Headers chuẩn cho request đến api.sonjj.com."""
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.8",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }


# ═══════════════════════════════════════════════════════════════════
# SMAILPRO CLASS — Module chính
# ═══════════════════════════════════════════════════════════════════

class SmailPro:
    """
    Client cho SmailPro temp email service.

    Luồng: create() → [gửi verification] → wait_for_message() / get_inbox() → get_message_detail()

    Args:
        cookies: Dict cookie {"XSRF-TOKEN": "...", "sonjj_session": "..."}.
                 Nếu None, tự load từ file smail_cookies.json.
        cookies_file: Đường dẫn file cookie JSON (mặc định: smail_cookies.json cùng thư mục).
        proxy: HTTP proxy URL, ví dụ "http://user:pass@host:port". None = không dùng proxy.
    """

    def __init__(
        self,
        cookies: Optional[Dict[str, str]] = None,
        cookies_file: Optional[str] = None,
        proxy: Optional[str] = None,
    ):
        self._cookies_file = cookies_file or _COOKIES_FILE
        self._proxy = proxy
        self._proxies = {"http": proxy, "https": proxy} if proxy else None
        self._cookies = self._load_cookies(cookies)
        self._last_email: Optional[Dict[str, Any]] = None

    # ── Cookie management ──────────────────────────────────────

    @staticmethod
    def _load_cookies(override: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Load cookies từ override hoặc file."""
        if override:
            return {k: v for k, v in override.items() if v}

        if os.path.exists(_COOKIES_FILE):
            try:
                with open(_COOKIES_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                result = {}
                for k in ("XSRF-TOKEN", "sonjj_session"):
                    if k in saved and saved[k]:
                        result[k] = saved[k]
                if result:
                    logger.info("Loaded cookies from %s", _COOKIES_FILE)
                    return result
            except Exception as e:
                logger.warning("Cannot load cookies file: %s", e)

        logger.warning("No cookies available. Set via constructor or smail_cookies.json")
        return {}

    def set_cookies(self, cookies: Dict[str, str]):
        """Cập nhật cookies runtime."""
        self._cookies = {k: v for k, v in cookies.items() if v}

    # ════════════════════════════════════════════════════════════
    # BƯỚC 1: TẠO EMAIL
    # ════════════════════════════════════════════════════════════

    def create(
        self,
        domain: str = "outlook.com",
        username: str = "random",
        email_type: str = "real",
        server: str = "1",
        retries: int = 5,
    ) -> Dict[str, Any]:
        """
        Tạo email tạm thời qua SmailPro.

        Args:
            domain:     Domain email (outlook.com, gmail.com, hotmail.com, etc.)
            username:   Tên người dùng ("random" = ngẫu nhiên)
            email_type: "real" hoặc "fake"
            server:     Server ID ("1", "2", "3")
            retries:    Số lần thử lại nếu lỗi

        Returns:
            Dict: {"address": "xxx@outlook.com", "key": "...", "timestamp": ...}

        Raises:
            RuntimeError: Nếu tạo email thất bại sau tất cả retries
        """
        url = f"{SMAILPRO_CREATE_URL}?username={username}&type={email_type}&domain={domain}&server={server}"

        for attempt in range(1, retries + 1):
            logger.info("Creating email (attempt %d/%d)...", attempt, retries)
            try:
                resp = requests.get(
                    url,
                    headers=_smailpro_headers(),
                    cookies=self._cookies,
                    timeout=REQUEST_TIMEOUT,
                    proxies=self._proxies,
                )
            except requests.RequestException as e:
                logger.error("create() request error: %s", e)
                time.sleep(2)
                continue

            if resp.status_code != 200:
                logger.warning("create() HTTP %d: %s", resp.status_code, resp.text[:200])
                time.sleep(2)
                continue

            try:
                data = resp.json()
            except Exception:
                logger.error("create() JSON parse error: %s", resp.text[:200])
                time.sleep(2)
                continue

            # Parse response — nhiều format khác nhau
            address = (
                data.get("address")
                or data.get("email")
                or (data.get("data") or {}).get("address")
            )
            key = data.get("key") or (data.get("data") or {}).get("key")
            timestamp = data.get("timestamp") or (data.get("data") or {}).get("timestamp")

            if address and key:
                email_data = {
                    "address": address,
                    "key": key,
                    "timestamp": timestamp,
                }
                self._last_email = email_data
                logger.info("Created email: %s", address)
                return email_data

            logger.warning("create() response missing address/key: %s", data)
            time.sleep(2)

        raise RuntimeError(f"Không thể tạo email sau {retries} lần thử")

    # ════════════════════════════════════════════════════════════
    # BƯỚC 2: ĐỌC INBOX
    # ════════════════════════════════════════════════════════════

    def get_inbox(self, email_data: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """
        Đọc inbox — lấy danh sách message.

        Args:
            email_data: Dict từ create(). Nếu None, dùng email gần nhất.

        Returns:
            Tuple (payload, messages):
                - payload: str (cần cho get_message_detail), None nếu inbox trống
                - messages: List[Dict] — mỗi dict có mid, subject, sender, date, snippet
        """
        email_data = email_data or self._last_email
        if not email_data:
            raise ValueError("Chưa tạo email. Gọi create() trước.")

        # ── Bước A: POST smailpro.com/app/inbox → lấy payload ──
        body = [{
            "address": email_data["address"],
            "timestamp": int(email_data["timestamp"]) if email_data.get("timestamp") else 0,
            "key": email_data["key"],
        }]

        logger.info("[INBOX] POST %s with body: %s", SMAILPRO_INBOX_URL, body)

        try:
            resp = requests.post(
                SMAILPRO_INBOX_URL,
                headers=_smailpro_headers(origin=True),
                json=body,
                cookies=self._cookies,
                timeout=REQUEST_TIMEOUT,
                proxies=self._proxies,
            )
        except requests.RequestException as e:
            logger.error("get_inbox() request error: %s", e)
            return None, []

        logger.info("[INBOX] Response status=%d, body=%s", resp.status_code, resp.text[:500])

        if resp.status_code != 200:
            logger.warning("get_inbox() HTTP %d", resp.status_code)
            logger.warning("get_inbox() response headers: %s", dict(resp.headers))
            return None, []

        try:
            inbox_resp = resp.json()
        except Exception:
            logger.error("get_inbox() JSON error: %s", resp.text[:200])
            return None, []

        logger.info("[INBOX] Parsed JSON: %s", str(inbox_resp)[:500])

        # Trích payload
        payload = None
        if isinstance(inbox_resp, list) and inbox_resp:
            payload = inbox_resp[0].get("payload")
        elif isinstance(inbox_resp, dict):
            payload = inbox_resp.get("payload")

        if not payload:
            logger.info("[INBOX] Inbox trống (chưa có payload)")
            logger.info("[INBOX] inbox_resp type: %s, value: %s", type(inbox_resp), str(inbox_resp)[:500])
            return None, []

        logger.info("[INBOX] Payload (%d chars): %s...", len(payload), payload[:100])
        logger.info("[INBOX] Email address: %s", email_data.get("address", "N/A"))

        # ── Bước B: GET sonjj API → danh sách messages ──
        messages = self._fetch_sonjj_messages(payload, email_address=email_data["address"])
        return payload, messages

    def _fetch_sonjj_messages(self, payload: str, email_address: str = "") -> List[Dict[str, Any]]:
        """Gọi Sonjj API lấy danh sách messages từ payload."""
        encoded = urllib.parse.quote(payload, safe="")
        inbox_url, _ = _get_sonjj_endpoint(email_address)
        url = f"{inbox_url}?payload={encoded}"

        logger.info("[SONJJ] GET %s", url[:200])

        try:
            resp = requests.get(
                url,
                headers=_sonjj_headers(),
                timeout=REQUEST_TIMEOUT,
                proxies=self._proxies,
            )
        except requests.RequestException as e:
            logger.error("[SONJJ] _fetch_sonjj_messages() error: %s", e)
            return []

        logger.info("[SONJJ] Response status=%d, body=%s", resp.status_code, resp.text[:500])

        if resp.status_code != 200:
            logger.warning("[SONJJ] _fetch_sonjj_messages() HTTP %d", resp.status_code)
            logger.warning("[SONJJ] response headers: %s", dict(resp.headers))
            return []

        try:
            data = resp.json()
        except Exception:
            logger.error("[SONJJ] _fetch_sonjj_messages() JSON error: %s", resp.text[:200])
            return []

        logger.info("[SONJJ] Parsed JSON type=%s, keys=%s",
                    type(data).__name__,
                    list(data.keys()) if isinstance(data, dict) else f"list[{len(data)}]")

        raw_messages = []
        if isinstance(data, dict):
            raw_messages = data.get("messages") or data.get("data") or []
        elif isinstance(data, list):
            raw_messages = data

        # Normalize
        messages = []
        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue
            messages.append({
                "mid": str(msg.get("mid", "")),
                "subject": str(msg.get("textSubject") or msg.get("subject") or ""),
                "sender": str(msg.get("textFrom") or msg.get("from") or ""),
                "date": str(msg.get("date") or msg.get("textDate") or ""),
                "snippet": str(msg.get("snippet") or msg.get("textSnippet") or ""),
                "_raw": msg,
            })

        logger.info("[SONJJ] Found %d message(s)", len(messages))
        if messages:
            for i, m in enumerate(messages[:3]):
                logger.info("[SONJJ]   [%d] mid=%s, subject=%s, sender=%s",
                            i, m["mid"], m["subject"][:60], m["sender"][:40])
        return messages

    # ════════════════════════════════════════════════════════════
    # BƯỚC 3: ĐỌC CHI TIẾT MESSAGE
    # ════════════════════════════════════════════════════════════

    def get_message_detail(self, mid: str, payload: str, email_address: str = "") -> Dict[str, Any]:
        """
        Lấy chi tiết một message cụ thể.

        Args:
            mid:           Message ID (từ message["mid"])
            payload:       Payload string (từ get_inbox())
            email_address: Email address (để xác định endpoint)

        Returns:
            Dict: {mid, subject, sender, to, date, body_html, body_text, body, _raw}

        Raises:
            RuntimeError: Nếu không lấy được chi tiết
        """
        encoded = urllib.parse.quote(payload, safe="")
        _, message_url = _get_sonjj_endpoint(email_address)
        url = f"{message_url}?payload={encoded}&mid={urllib.parse.quote(mid, safe='')}"

        try:
            resp = requests.get(
                url,
                headers=_sonjj_headers(),
                timeout=REQUEST_TIMEOUT,
                proxies=self._proxies,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Lỗi kết nối get_message_detail: {e}") from e

        if resp.status_code != 200:
            raise RuntimeError(f"get_message_detail() HTTP {resp.status_code}")

        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"get_message_detail() JSON error: {resp.text[:200]}")

        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected response type: {type(data)}")

        body_html = (
            str(data.get("message") or "")
            or str(data.get("body") or "")
            or str(data.get("htmlBody") or "")
            or str(data.get("content") or "")
        )
        body_text = (
            str(data.get("textBody") or "")
            or str(data.get("text") or "")
            or str(data.get("snippet") or "")
        )

        return {
            "mid": mid,
            "subject": str(data.get("subject") or data.get("textSubject") or ""),
            "sender": str(data.get("from") or data.get("textFrom") or ""),
            "to": str(data.get("to") or data.get("textTo") or ""),
            "date": str(data.get("date") or data.get("textDate") or ""),
            "body_html": body_html,
            "body_text": body_text,
            "body": body_html or body_text,
            "_raw": data,
        }

    # ════════════════════════════════════════════════════════════
    # BƯỚC 4: POLL CHỜ EMAIL
    # ════════════════════════════════════════════════════════════

    def wait_for_message(
        self,
        email_data: Optional[Dict[str, Any]] = None,
        timeout: int = DEFAULT_POLL_TIMEOUT,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        check_fn: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """
        Poll inbox cho đến khi có email hoặc timeout.

        Args:
            email_data:    Dict từ create(). Nếu None, dùng email gần nhất.
            timeout:       Giây tối đa chờ (mặc định 120)
            poll_interval: Giây giữa mỗi lần poll (mặc định 3)
            check_fn:      Hàm lọc message (msg_dict) -> bool. None = chấp nhận mọi message.

        Returns:
            Tuple (payload, messages)

        Raises:
            TimeoutError: Nếu timeout mà không có email
        """
        email_data = email_data or self._last_email
        if not email_data:
            raise ValueError("Chưa tạo email. Gọi create() trước.")

        logger.info("Waiting for email at %s (timeout=%ds)...", email_data["address"], timeout)
        deadline = time.time() + timeout
        poll_count = 0

        while time.time() < deadline:
            poll_count += 1
            remaining = int(deadline - time.time())
            logger.debug("Poll #%d (remaining %ds)", poll_count, remaining)

            try:
                payload, messages = self.get_inbox(email_data)
            except Exception as e:
                logger.warning("Poll #%d inbox error: %s", poll_count, e)
                time.sleep(poll_interval)
                continue

            if not messages:
                logger.debug("Poll #%d: inbox trống", poll_count)
                time.sleep(poll_interval)
                continue

            # Lọc theo check_fn nếu có
            if check_fn:
                matched = [m for m in messages if check_fn(m)]
                if matched:
                    logger.info("Poll #%d: found %d matching message(s)", poll_count, len(matched))
                    return payload, matched
                logger.debug("Poll #%d: %d message(s) nhưng không khớp filter", poll_count, len(messages))
                time.sleep(poll_interval)
                continue

            logger.info("Poll #%d: found %d message(s)!", poll_count, len(messages))
            return payload, messages

        raise TimeoutError(f"Timeout ({timeout}s) chờ email tại {email_data['address']}")

    def wait_for_code(
        self,
        email_data: Optional[Dict[str, Any]] = None,
        timeout: int = DEFAULT_POLL_TIMEOUT,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        digits: int = 6,
    ) -> Optional[str]:
        """
        Poll inbox chờ verification code (OTP).

        Args:
            email_data:    Dict từ create()
            timeout:       Giây tối đa chờ
            poll_interval: Giây giữa mỗi lần poll
            digits:        Số chữ số của code (mặc định 6)

        Returns:
            Code string hoặc None nếu timeout
        """
        email_data = email_data or self._last_email
        if not email_data:
            raise ValueError("Chưa tạo email. Gọi create() trước.")

        logger.info("Waiting for %d-digit code at %s (timeout=%ds)...", digits, email_data["address"], timeout)
        deadline = time.time() + timeout
        poll_count = 0

        while time.time() < deadline:
            poll_count += 1

            try:
                payload, messages = self.get_inbox(email_data)
            except Exception as e:
                logger.warning("Poll #%d error: %s", poll_count, e)
                time.sleep(poll_interval)
                continue

            if not messages:
                time.sleep(poll_interval)
                continue

            # Tìm code trong mỗi message
            for msg in messages:
                mid = msg.get("mid")
                code = None

                if mid and payload:
                    try:
                        detail = self.get_message_detail(mid, payload, email_address=email_data["address"])
                        code = self.extract_otp_code(detail.get("body", ""), digits=digits)
                    except Exception:
                        pass

                if not code:
                    # Thử extract từ snippet
                    text = msg.get("snippet", "") + " " + msg.get("subject", "")
                    code = self.extract_otp_code(text, digits=digits)

                if code:
                    logger.info("Found code: %s", code)
                    return code

            time.sleep(poll_interval)

        logger.warning("Timeout waiting for code")
        return None

    # ════════════════════════════════════════════════════════════
    # TIỆN ÍCH: TRÍCH XUẤT
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def extract_otp_code(text: str, digits: int = 6) -> Optional[str]:
        """
        Trích mã OTP (4-8 chữ số) từ nội dung email.

        Args:
            text:   Nội dung email (HTML hoặc plain text)
            digits: Số chữ số mong muốn (mặc định 6)

        Returns:
            Code string hoặc None
        """
        if not text:
            return None

        # Pattern 1: "code/otp/mã: 123456"
        match = re.search(
            r'(?:mã|code|otp|verification|verify|code is|is)[:\s]*(\d{' + str(digits) + r'})',
            text, re.IGNORECASE
        )
        if match:
            return match.group(1)

        # Pattern 2: standalone N-digit number
        match = re.search(r'\b(\d{' + str(digits) + r'})\b', text)
        if match:
            return match.group(1)

        # Pattern 3: bất kỳ 4-8 digits
        match = re.search(r'\b(\d{4,8})\b', text)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def extract_oob_code(text: str) -> Optional[str]:
        """
        Trích oobCode từ email xác minh Firebase (dùng cho Resona).

        Args:
            text: Nội dung email HTML/text

        Returns:
            oobCode string hoặc None
        """
        if not text:
            return None

        # Pattern: oobCode=XXXXX
        match = re.search(r'oobCode=([A-Za-z0-9_-]+)', text)
        if match:
            return match.group(1)

        # Pattern: oobCode%3DXXXXX (URL encoded)
        match = re.search(r'oobCode[=%]3D?([A-Za-z0-9_-]+)', text)
        if match:
            code = match.group(1)
            if code.startswith("3D") and len(code) > 2:
                candidate = code[2:]
                if re.match(r'^[A-Za-z0-9_-]+$', candidate):
                    return candidate
            return code

        return None

    @staticmethod
    def extract_verification_token(text: str) -> Optional[str]:
        """
        Trích verification token từ email (verify-email?token=XXXXX).

        Args:
            text: Nội dung email

        Returns:
            Token string hoặc None
        """
        if not text:
            return None
        match = re.search(r'verify-email\?token=([A-Za-z0-9_\-]+)', text)
        return match.group(1) if match else None

    @staticmethod
    def extract_links(text: str) -> List[str]:
        """
        Trích tất cả URL từ nội dung email.

        Args:
            text: Nội dung HTML/text

        Returns:
            List URL strings
        """
        if not text:
            return []
        return re.findall(r'https?://[^\s<>"\')\]]+', text)

    # ════════════════════════════════════════════════════════════
    # LUỒNG TỔNG HỢP
    # ════════════════════════════════════════════════════════════

    def full_flow(
        self,
        domain: str = "outlook.com",
        timeout: int = DEFAULT_POLL_TIMEOUT,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        check_fn: Optional[Callable[[Dict[str, Any]], bool]] = None,
        extract_body: bool = True,
    ) -> Dict[str, Any]:
        """
        Luồng tổng hợp: tạo email → poll chờ → đọc inbox + chi tiết.

        Args:
            domain:        Domain email
            timeout:       Timeout chờ email
            poll_interval: Khoảng cách giữa mỗi lần poll
            check_fn:      Hàm lọc message
            extract_body:  Có lấy chi tiết body từng message không

        Returns:
            Dict: {
                "email": {...},
                "payload": str,
                "messages": [...],
                "details": [...],
                "verification_token": str|None,
                "oob_code": str|None,
                "otp_code": str|None,
                "all_links": [...]
            }
        """
        result = {
            "email": None,
            "payload": None,
            "messages": [],
            "details": [],
            "verification_token": None,
            "oob_code": None,
            "otp_code": None,
            "all_links": [],
        }

        # Step 1: Tạo email
        email = self.create(domain=domain)
        result["email"] = email

        # Step 2: Poll chờ email
        try:
            payload, messages = self.wait_for_message(
                email, timeout=timeout, poll_interval=poll_interval, check_fn=check_fn
            )
        except TimeoutError as e:
            logger.error("%s", e)
            return result

        result["payload"] = payload
        result["messages"] = messages

        # Step 3: Lấy chi tiết từng message
        if payload and extract_body:
            for msg in messages:
                mid = msg.get("mid")
                if not mid:
                    continue
                try:
                    detail = self.get_message_detail(mid, payload, email_address=email["address"])
                    result["details"].append(detail)

                    # Trích xuất
                    body = detail.get("body", "")

                    token = self.extract_verification_token(body)
                    if token and not result["verification_token"]:
                        result["verification_token"] = token

                    oob = self.extract_oob_code(body)
                    if oob and not result["oob_code"]:
                        result["oob_code"] = oob

                    otp = self.extract_otp_code(body)
                    if otp and not result["otp_code"]:
                        result["otp_code"] = otp

                    result["all_links"].extend(self.extract_links(body))

                except Exception as e:
                    logger.warning("Cannot get detail for mid=%s: %s", mid, e)

        return result


# ═══════════════════════════════════════════════════════════════════
# QUICK FUNCTIONS — Không cần tạo instance
# ═══════════════════════════════════════════════════════════════════

def quick_create(
    cookies: Dict[str, str],
    domain: str = "outlook.com",
) -> Dict[str, Any]:
    """
    Tạo email nhanh (không cần tạo instance).

    Args:
        cookies: Dict cookie {"XSRF-TOKEN": "...", "sonjj_session": "..."}
        domain:  Domain email

    Returns:
        Dict: {"address", "key", "timestamp"}
    """
    mail = SmailPro(cookies=cookies)
    return mail.create(domain=domain)


def quick_wait_code(
    email_data: Dict[str, Any],
    cookies: Dict[str, str],
    timeout: int = 120,
    digits: int = 6,
) -> Optional[str]:
    """
    Chờ verification code nhanh.

    Args:
        email_data: Dict từ quick_create() hoặc SmailPro.create()
        cookies:    Dict cookie
        timeout:    Giây tối đa chờ
        digits:     Số chữ số code

    Returns:
        Code string hoặc None
    """
    mail = SmailPro(cookies=cookies)
    return mail.wait_for_code(email_data, timeout=timeout, digits=digits)


def quick_read_inbox(
    email_data: Dict[str, Any],
    cookies: Dict[str, str],
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Đọc inbox nhanh (không poll).

    Args:
        email_data: Dict từ create()
        cookies:    Dict cookie

    Returns:
        Tuple (payload, messages)
    """
    mail = SmailPro(cookies=cookies)
    return mail.get_inbox(email_data)


def quick_full_flow(
    cookies: Dict[str, str],
    domain: str = "outlook.com",
    timeout: int = 120,
) -> Dict[str, Any]:
    """
    Luồng tổng hợp nhanh: tạo → chờ → đọc chi tiết.

    Args:
        cookies: Dict cookie
        domain:  Domain email
        timeout: Timeout chờ email

    Returns:
        Dict kết quả (xem SmailPro.full_flow)
    """
    mail = SmailPro(cookies=cookies)
    return mail.full_flow(domain=domain, timeout=timeout)


# ═══════════════════════════════════════════════════════════════════
# PARSE COOKIES — Tiện ích cho GUI / browser export
# ═══════════════════════════════════════════════════════════════════

def parse_cookies(raw: str) -> Dict[str, str]:
    """
    Parse smailpro cookies từ JSON (browser export format).

    Hỗ trợ:
    - JSON array cookie objects (từ extension export browser)
    - JSON object với XSRF-TOKEN và sonjj_session keys

    Args:
        raw: JSON string

    Returns:
        Dict {"XSRF-TOKEN": "...", "sonjj_session": "..."} hoặc {} nếu lỗi
    """
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    result = {}
    if isinstance(data, list):
        for item in data:
            name = item.get("name", "")
            value = item.get("value", "")
            if name in ("XSRF-TOKEN", "sonjj_session") and value:
                result[name] = value
    elif isinstance(data, dict):
        for key in ("XSRF-TOKEN", "sonjj_session"):
            if key in data and data[key]:
                result[key] = data[key]
    return result


# ═══════════════════════════════════════════════════════════════════
# CLI — Test nhanh
# ═══════════════════════════════════════════════════════════════════

def main():
    """Chạy test nhanh từ command line."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print("╔══════════════════════════════════════════════════════════╗")
    print("║     SMAILPRO LOGIC FULL — Test & Demo                  ║")
    print("╚══════════════════════════════════════════════════════════╝")

    # Load cookies
    if os.path.exists(_COOKIES_FILE):
        with open(_COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        print(f"\n✅ Loaded cookies từ {_COOKIES_FILE}")
    else:
        print(f"\n❌ Không tìm thấy {_COOKIES_FILE}")
        print("   Tạo file smail_cookies.json với nội dung:")
        print('   {"XSRF-TOKEN": "...", "sonjj_session": "..."}')
        sys.exit(1)

    mail = SmailPro(cookies=cookies)

    # Step 1: Tạo email
    print("\n── Bước 1: Tạo email ──")
    try:
        email = mail.create(domain="outlook.com")
        print(f"  ✅ Email: {email['address']}")
        print(f"     Key: {email['key'][:40]}...")
    except RuntimeError as e:
        print(f"  ❌ {e}")
        sys.exit(1)

    # Step 2: Đọc inbox
    print("\n── Bước 2: Đọc inbox ──")
    payload, messages = mail.get_inbox(email)
    print(f"  Payload: {'có' if payload else 'trống'}")
    print(f"  Messages: {len(messages)}")

    if messages:
        for i, msg in enumerate(messages, 1):
            print(f"  [{i}] {msg['subject'][:60]}")
            print(f"      From: {msg['sender']}")
            print(f"      Mid: {msg['mid']}")

    # Step 3: Nếu có message, lấy chi tiết
    if messages and payload:
        print("\n── Bước 3: Chi tiết message ──")
        for msg in messages[:3]:  # Chỉ lấy 3 message đầu
            mid = msg.get("mid")
            if not mid:
                continue
            try:
                detail = mail.get_message_detail(mid, payload)
                print(f"\n  📧 {detail['subject']}")
                print(f"     From: {detail['sender']}")
                print(f"     To: {detail['to']}")
                body_preview = detail["body"][:200].replace("\n", " ")
                print(f"     Body: {body_preview}...")

                # Trích xuất
                otp = SmailPro.extract_otp_code(detail["body"])
                oob = SmailPro.extract_oob_code(detail["body"])
                links = SmailPro.extract_links(detail["body"])
                if otp:
                    print(f"     🔢 OTP: {otp}")
                if oob:
                    print(f"     🔑 oobCode: {oob}")
                if links:
                    print(f"     🔗 Links: {len(links)}")
            except Exception as e:
                print(f"  ⚠️ Lỗi lấy detail: {e}")

    # Nếu có arg --wait, chờ thêm email mới
    if "--wait" in sys.argv:
        print("\n── Chờ email mới (120s) ──")
        try:
            code = mail.wait_for_code(email, timeout=120)
            if code:
                print(f"\n  🔢 CODE: {code}")
            else:
                print("\n  ⏰ Timeout — không nhận được code")
        except Exception as e:
            print(f"\n  ❌ {e}")


if __name__ == "__main__":
    main()
