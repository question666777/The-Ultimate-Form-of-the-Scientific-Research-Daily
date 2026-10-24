# -*- coding: utf-8 -*-
"""
前沿生物追踪 · 163 邮箱发送器

用法：
  python send_email.py --file "报告.html" [--subject "标题"]
  python send_email.py --test                  发送配置测试邮件

凭据读取本脚本同目录的 config.ini，仅保存在本机，不要外传。
仅使用 Python 标准库，无需安装任何依赖。
"""

import argparse
import configparser
import os
import re
import smtplib
import sys
import time
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "config.ini")
MAX_RETRY = 3
PLACEHOLDER = "在此填入SMTP授权码"


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        sys.exit(f"[错误] 找不到配置文件：{path}")
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    if "email" not in cp:
        sys.exit("[错误] config.ini 中缺少 [email] 段")
    cfg = cp["email"]
    missing = [k for k in ("smtp_host", "smtp_port", "sender", "password", "receiver")
               if not cfg.get(k, "").strip()]
    if missing:
        sys.exit(f"[错误] config.ini 缺少字段：{', '.join(missing)}")
    password = cfg["password"].strip().replace(" ", "")
    if not password or password == PLACEHOLDER:
        sys.exit("[错误] 尚未填写 SMTP 授权码。获取方法：网页登录 mail.163.com → "
                 "设置 → POP3/SMTP/IMAP → 开启 SMTP 服务 → 生成授权码，"
                 "填回 config.ini 的 password 字段。")
    return {
        "host": cfg["smtp_host"].strip(),
        "port": int(cfg["smtp_port"]),
        "sender": cfg["sender"].strip(),
        "password": password,
        "receiver": cfg["receiver"].strip(),
    }


def strip_svg_for_email(html: str) -> str:
    """剥离 SVG 图表，替换为占位提示卡片（163 邮箱 webmail 不渲染 SVG）。"""
    placeholder = (
        '<div style="background:#f8f8f8;border:1px solid #e0e0e0;'
        'border-radius:8px;padding:20px 16px;margin:16px 0;'
        'text-align:center;color:#888;font-size:14px;'
        'font-family:sans-serif;line-height:1.6;">'
        '📊 此处为图表展示区<br>'
        '<span style="font-size:12px;color:#aaa;">'
        '请在附件中打开完整 HTML 版本查看'
        '</span></div>'
    )
    return re.sub(r'<svg[\s\S]*?</svg>', placeholder, html, flags=re.IGNORECASE)


def build_message(cfg: dict, html_path: str, subject: str) -> MIMEMultipart:
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    msg = MIMEMultipart("mixed")
    msg["From"] = cfg["sender"]
    msg["To"] = cfg["receiver"]
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = Header(subject, "utf-8")
    # 正文 = 剥离 SVG 后的 HTML（163 webmail 不渲染 SVG）
    email_html = strip_svg_for_email(html)
    msg.attach(MIMEText(email_html, "html", "utf-8"))
    # 附件 = 原始完整 HTML（含 SVG 图表，浏览器打开即完整版）
    with open(html_path, "rb") as f:
        part = MIMEApplication(f.read())
    part.add_header("Content-Disposition", "attachment",
                    filename=os.path.basename(html_path))
    msg.attach(part)
    return msg


def send(cfg: dict, msg: MIMEMultipart) -> bool:
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30) as smtp:
                smtp.login(cfg["sender"], cfg["password"])
                smtp.sendmail(cfg["sender"], [cfg["receiver"]], msg.as_string())
            print(f"[成功] 邮件已发送至 {cfg['receiver']}")
            return True
        except Exception as exc:  # noqa: BLE001 - 任何 SMTP 异常都走重试
            last_err = exc
            print(f"[警告] 第 {attempt}/{MAX_RETRY} 次发送失败：{exc}")
            if attempt < MAX_RETRY:
                time.sleep(5 * attempt)
    print(f"[失败] 重试 {MAX_RETRY} 次后仍未发出：{last_err}")
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="前沿生物追踪 · 163 邮件发送器")
    ap.add_argument("--file", help="要发送的 HTML 报告完整路径")
    ap.add_argument("--subject", help="邮件标题（缺省用文件名）")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="config.ini 路径")
    ap.add_argument("--test", action="store_true", help="发送一封配置测试邮件")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.test:
        msg = MIMEMultipart("mixed")
        msg["From"] = cfg["sender"]
        msg["To"] = cfg["receiver"]
        msg["Date"] = formatdate(localtime=True)
        msg["Subject"] = Header("【前沿生物追踪】配置测试", "utf-8")
        msg.attach(MIMEText(
            "<h2>配置测试成功</h2><p>SMTP 链路正常，每日报告将发送至此邮箱。</p>",
            "html", "utf-8"))
        sys.exit(0 if send(cfg, msg) else 1)

    if not args.file:
        ap.error("请用 --file 指定报告路径，或用 --test 测试配置")
    if not os.path.exists(args.file):
        sys.exit(f"[错误] 报告文件不存在：{args.file}")
    subject = args.subject or os.path.splitext(os.path.basename(args.file))[0]
    msg = build_message(cfg, args.file, subject)
    sys.exit(0 if send(cfg, msg) else 1)


if __name__ == "__main__":
    main()
