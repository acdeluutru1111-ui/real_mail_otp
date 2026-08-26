"""
auto_2_cookie_sonji_logic.py
============================
Logic tự động lấy 2 loại cookie từ sonjj.com/smailpro.com:
  1. XSRF-TOKEN (CSRF protection)
  2. sonjj_session (Session cookie)

Flow:
  1. Gửi Magic Link → sonjj.com
  2. Đọc link từ Gmail IMAP
  3. Truy cập sign-in link → lấy ghost-members-ssr
  4. GET /members/api/session → lấy JWT
  5. SSO → my.sonjj.com (tích lũy cookies)
  6. Visit smailpro.com → lấy XSRF-TOKEN + sonjj_session

Cách sử dụng:
  - Điền thông tin Gmail vào .env: GMAIL_EMAIL, GMAIL_APP_PASSWORD
  - Chạy: python auto_2_cookie_sonji_logic.py
  - Hoặc import: from auto_2_cookie_sonji_logic import get_cookies
"""

import os
import re
import time
import socket
import imaplib
import email
import logging
import urllib.parse
import requests
from typing import Optional, Tuple

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def get_cookies(
    gmail_email: str,
    gmail_password: str,
    max_wait: int = 30,
    poll_interval: int = 5,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Tự động lấy XSRF-TOKEN và sonjj_session từ sonjj.com/smailpro.com.

    Args:
        gmail_email: Địa chỉ Gmail để nhận magic link
        gmail_password: Gmail App Password
        max_wait: Thời gian tối đa chờ email (giây)
        poll_interval: Khoảng thời gian giữa các lần kiểm tra email (giây)

    Returns:
        Tuple[Optional[str], Optional[str]]: (xsrf_token, sonjj_session)
        Trả về (None, None) nếu thất bại
    """
    # ── Step 1: Gửi magic link ──
    if not _request_magic_link(gmail_email):
        logger.error("❌ Không thể gửi magic link")
        return None, None

    # ── Step 2: Đọc sign-in link từ Gmail ──
    signin_url = _read_signin_link(gmail_email, gmail_password, max_wait, poll_interval)
    if not signin_url:
        logger.error("❌ Không tìm thấy sign-in link trong Gmail")
        return None, None

    # ── Step 3-6: Truy cập link và lấy cookies ──
    xsrf_token, sonjj_session = _extract_cookies_from_signin(signin_url)

    if xsrf_token:
        logger.info(f"✅ XSRF-TOKEN: {xsrf_token[:30]}...")
    if sonjj_session:
        logger.info(f"✅ sonjj_session: {sonjj_session[:30]}...")

    return xsrf_token, sonjj_session


def _request_magic_link(gmail_email: str) -> bool:
    """Gửi magic link đến sonjj.com."""
    try:
        resp = requests.post(
            "https://sonjj.com/members/api/send-magic-link/",
            json={"email": gmail_email},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Origin": "https://sonjj.com",
                "Referer": "https://sonjj.com/redirect-auth/",
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info("📧 Magic link đã gửi đến Gmail")
            return True
        else:
            logger.error(f"❌ Gửi magic link thất bại: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"❌ Lỗi gửi magic link: {e}")
        return False


def _read_signin_link(
    gmail_email: str,
    gmail_password: str,
    max_wait: int = 30,
    poll_interval: int = 5,
) -> Optional[str]:
    """Đọc sign-in link từ Gmail qua IMAP."""
    logger.info(f"⏳ Đang chờ email từ sonjj.com (tối đa {max_wait}s)...")

    # Force IPv4 cho IMAP (tránh lỗi trên một số môi trường)
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(10)
    old_getaddrinfo = socket.getaddrinfo

    def ipv4_only_getaddrinfo(*args, **kwargs):
        return [r for r in old_getaddrinfo(*args, **kwargs) if r[0] == socket.AF_INET]

    socket.getaddrinfo = ipv4_only_getaddrinfo

    start = time.time()
    last_seen_id = None

    try:
        while time.time() - start < max_wait:
            try:
                mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=8)
                mail.login(gmail_email, gmail_password)
                mail.select("inbox")

                status, messages = mail.search(None, 'FROM "sonjj.com" UNSEEN')

                if status == "OK" and messages[0]:
                    msg_ids = messages[0].split()
                    latest_id = msg_ids[-1]

                    if latest_id != last_seen_id:
                        last_seen_id = latest_id
                        status, msg_data = mail.fetch(latest_id, "(RFC822)")

                        if status == "OK":
                            msg = email.message_from_bytes(msg_data[0][1])
                            body = _get_email_body(msg)

                            # Tìm sign-in URL
                            link_match = re.search(
                                r'(https://sonjj\.com/members/\?token=[^\s\)"\'>]+)',
                                body,
                            )
                            if link_match:
                                signin_url = link_match.group(1).rstrip(")")
                                logger.info(f"🔗 Tìm thấy sign-in link!")
                                mail.logout()
                                return signin_url

                mail.logout()

            except imaplib.IMAP4.error as e:
                logger.error(f"❌ Lỗi IMAP: {e}")
                return None
            except Exception as e:
                logger.error(f"❌ Lỗi đọc Gmail: {e}")
                if "Network is unreachable" in str(e) or "Connection refused" in str(e):
                    logger.error("❌ IMAP bị chặn trên host này")
                    return None

            elapsed = int(time.time() - start)
            logger.info(f"⏳ Chưa có email ({elapsed}s), thử lại sau {poll_interval}s...")
            time.sleep(poll_interval)

        logger.error("❌ Hết thời gian chờ email sonjj.com")
        return None
    finally:
        socket.setdefaulttimeout(old_timeout)
        socket.getaddrinfo = old_getaddrinfo


def _extract_cookies_from_signin(signin_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Truy cập sign-in link và lấy cookies.

    Flow:
      1. Access sign-in link → get ghost-members-ssr
      2. GET /members/api/session → get JWT
      3. SSO → my.sonjj.com (tích lũy cookies)
      4. Visit smailpro.com → get XSRF-TOKEN + sonjj_session
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Origin": "https://sonjj.com",
        "Referer": "https://sonjj.com/redirect-auth/",
    })

    # ── Step 3: Truy cập sign-in link ──
    logger.info("🔑 Step 3: Truy cập sign-in link...")
    try:
        resp = session.get(signin_url, allow_redirects=True, timeout=15)
        logger.info(f"   Status: {resp.status_code}, URL: {resp.url}")
    except Exception as e:
        logger.error(f"❌ Lỗi truy cập sign-in link: {e}")
        return None, None

    # ── Step 4: Lấy JWT ──
    logger.info("🔑 Step 4: Lấy JWT...")
    jwt_token = None
    try:
        session_resp = session.get("https://sonjj.com/members/api/session", timeout=15)
        logger.info(f"   Session status: {session_resp.status_code}")

        if session_resp.status_code == 200:
            try:
                session_data = session_resp.json()
                jwt_token = session_data.get("jwt")
            except Exception:
                text = session_resp.text.strip()
                if text and "." in text and len(text) > 50:
                    jwt_token = text
    except Exception as e:
        logger.error(f"❌ Lỗi lấy JWT: {e}")

    # ── Step 5: SSO đến my.sonjj.com ──
    if jwt_token:
        logger.info("🔑 Step 5: SSO đến my.sonjj.com...")
        try:
            sso_url = f"https://my.sonjj.com/auth/sonjj?session={jwt_token}"
            sso_resp = session.get(sso_url, allow_redirects=True, timeout=15)
            logger.info(f"   SSO status: {sso_resp.status_code}, URL: {sso_resp.url}")
        except Exception as e:
            logger.error(f"❌ Lỗi SSO: {e}")
    else:
        logger.info("⏭️ Step 5: Không có JWT, bỏ qua SSO")

    # ── Step 6: Visit smailpro.com để lấy cookies ──
    logger.info("🔑 Step 6: Visit smailpro.com để lấy cookies...")
    xsrf_token = None
    sonjj_session = None

    try:
        # Xây dựng cookie header từ tất cả cookies đã tích lũy
        cookie_parts = []
        for c in session.cookies:
            cookie_parts.append(f"{c.name}={c.value}")
        cookie_header = "; ".join(cookie_parts)

        smail_resp = requests.get(
            "https://smailpro.com/temporary-email",
            headers={
                "User-Agent": session.headers["User-Agent"],
                "cookie": cookie_header,
            },
            allow_redirects=True,
            timeout=15,
        )
        logger.info(f"   SmailPro status: {smail_resp.status_code}")

        # Lấy XSRF-TOKEN + sonjj_session từ response
        for c in smail_resp.cookies:
            if c.name == "XSRF-TOKEN" and not xsrf_token:
                xsrf_token = urllib.parse.unquote(c.value)
            if c.name == "sonjj_session" and not sonjj_session:
                sonjj_session = c.value

        # Fallback: kiểm tra Set-Cookie headers
        for key, val in smail_resp.headers.items():
            if key.lower() == "set-cookie":
                for part in val.split(","):
                    if "XSRF-TOKEN=" in part and not xsrf_token:
                        m = re.search(r"XSRF-TOKEN=([^;]+)", part)
                        if m:
                            xsrf_token = urllib.parse.unquote(m.group(1))
                    if "sonjj_session=" in part and not sonjj_session:
                        m = re.search(r"sonjj_session=([^;]+)", part)
                        if m:
                            sonjj_session = m.group(1)
    except Exception as e:
        logger.error(f"❌ Lỗi visit smailpro.com: {e}")

    if not xsrf_token and not sonjj_session:
        logger.error("❌ Không lấy được XSRF-TOKEN/sonjj_session")

    return xsrf_token, sonjj_session


def _get_email_body(msg) -> str:
    """Lấy nội dung email (ưu tiên plain text, fallback HTML)."""
    body_plain = ""
    body_html = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body_plain = payload.decode(errors="replace")
            elif content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    body_html = payload.decode(errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body_plain = payload.decode(errors="replace")

    return body_plain if body_plain else body_html


# ════════════════════════════════════════════════════════════════════
#                           DEMO / TEST
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    gmail_email = os.getenv("GMAIL_EMAIL", "")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD", "")

    if not gmail_email or not gmail_password:
        print("❌ Vui lòng设置 GMAIL_EMAIL và GMAIL_APP_PASSWORD trong .env")
        print("   GMAIL_EMAIL=your_email@gmail.com")
        print("   GMAIL_APP_PASSWORD=your_app_password")
        exit(1)

    print("🔄 Đang tự động lấy cookies từ sonjj.com...")
    xsrf, session = get_cookies(gmail_email, gmail_password)

    if xsrf and session:
        print("\n" + "=" * 60)
        print("✅ THÀNH CÔNG! Đã lấy được 2 cookies:")
        print("=" * 60)
        print(f"\n1. XSRF-TOKEN:\n   {xsrf}")
        print(f"\n2. sonjj_session:\n   {session}")
        print("\n" + "=" * 60)
        print("💡 Sử dụng cookies này trong header của request:")
        print(f'   Cookie: XSRF-TOKEN={xsrf}; sonjj_session={session}')
        print("=" * 60)
    else:
        print("\n❌ Không lấy được cookies. Kiểm tra lại:")
        print("   1. GMAIL_EMAIL và GMAIL_APP_PASSWORD đúng chưa?")
        print("   2. Gmail có bật IMAP không?")
        print("   3. App Password có đúng không?")
