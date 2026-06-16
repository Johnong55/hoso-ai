"""Email service đơn giản — gửi email qua SMTP (smtplib).

Nếu SMTP_HOST chưa cấu hình → fallback **log token ra backend log** (development
mode). Khi đó admin có thể copy link reset từ log để test.

Khi production: cấu hình SMTP_HOST/USER/PASSWORD trong .env, ví dụ với Gmail:
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=youraccount@gmail.com
  SMTP_PASSWORD=<app-password>  # KHÔNG dùng password thật, phải tạo App Password
  SMTP_FROM_EMAIL=youraccount@gmail.com
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from loguru import logger

from app.core.config import settings


def _build_reset_link(token: str) -> str:
    """Build URL frontend mà user click để mở trang reset password."""
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    return f"{base}/reset-password?token={token}"


def _build_reset_email_body(full_name: str, reset_link: str) -> tuple[str, str]:
    """Trả về (plain_text, html) — multipart cho client cũ + client mới."""
    greeting = f"Xin chào {full_name}," if full_name else "Xin chào,"
    expire_min = settings.PASSWORD_RESET_EXPIRE_MINUTES

    plain = f"""{greeting}

Bạn vừa yêu cầu đặt lại mật khẩu cho tài khoản HoSoAI của mình.
Vui lòng nhấn vào liên kết sau (có hiệu lực trong {expire_min} phút):

{reset_link}

Nếu bạn không yêu cầu đặt lại mật khẩu, vui lòng bỏ qua email này — tài khoản
của bạn vẫn an toàn.

Trân trọng,
Đội ngũ HoSoAI
"""

    html = f"""<html><body style="font-family: Arial, sans-serif; max-width: 560px; margin: 0 auto; padding: 24px; color: #111;">
  <h2 style="color: #2563eb;">Đặt lại mật khẩu HoSoAI</h2>
  <p>{greeting}</p>
  <p>Bạn vừa yêu cầu đặt lại mật khẩu cho tài khoản HoSoAI của mình. Nhấn nút bên dưới để tiếp tục:</p>
  <p style="text-align: center; margin: 32px 0;">
    <a href="{reset_link}"
       style="display: inline-block; background: #2563eb; color: #fff; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: 600;">
       Đặt lại mật khẩu
    </a>
  </p>
  <p style="font-size: 13px; color: #555;">Liên kết có hiệu lực trong {expire_min} phút.</p>
  <p style="font-size: 13px; color: #555;">Nếu nút trên không hoạt động, copy link sau vào trình duyệt:<br/>
    <a href="{reset_link}" style="color: #2563eb; word-break: break-all;">{reset_link}</a>
  </p>
  <hr style="margin: 24px 0; border: none; border-top: 1px solid #e5e7eb;"/>
  <p style="font-size: 12px; color: #888;">Nếu bạn không yêu cầu đặt lại mật khẩu, vui lòng bỏ qua email này — tài khoản của bạn vẫn an toàn.</p>
</body></html>"""

    return plain, html


def send_password_reset_email(to_email: str, full_name: str, token: str) -> bool:
    """Gửi email reset password.

    Returns True nếu gửi thành công (hoặc fallback log mode), False nếu SMTP fail.
    """
    reset_link = _build_reset_link(token)

    # Fallback mode — chưa cấu hình SMTP
    if not settings.SMTP_HOST or not settings.SMTP_USER:
        logger.warning(
            f"Auth | password_reset_email | SMTP chưa cấu hình, log token thay vì gửi email\n"
            f"  TO: {to_email}\n"
            f"  LINK: {reset_link}"
        )
        return True

    plain, html = _build_reset_email_body(full_name, reset_link)
    msg = EmailMessage()
    msg["Subject"] = "Đặt lại mật khẩu HoSoAI"
    msg["From"] = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM_EMAIL))
    msg["To"] = to_email
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    try:
        if settings.SMTP_USE_TLS:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
                smtp.starttls()
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as smtp:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                smtp.send_message(msg)
        logger.info(f"Auth | password_reset_email | sent to={to_email}")
        return True
    except Exception as e:
        logger.error(f"Auth | password_reset_email | SMTP error | {e}")
        return False
