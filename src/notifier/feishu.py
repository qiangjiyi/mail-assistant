"""
飞书通知模块
通过Webhook发送卡片消息
"""

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from ..config import get_config
from ..mail.parser import EmailData


class FeishuNotifier:
    """飞书Webhook通知器"""
    
    def __init__(self):
        self._config = get_config()
        self._feishu_config = self._config.feishu
        
        self._enabled = self._feishu_config.get("enabled", False)
        self._webhook_url = self._feishu_config.get("webhook_url", "")
        self._secret = self._feishu_config.get("secret", "")
        
        self._security_config = self._feishu_config.get("security", {})
        self._keyword_filter_enabled = self._security_config.get("keyword_filter_enabled", True)
        self._blocked_keywords = self._security_config.get("blocked_keywords", [])
        self._hmac_enabled = self._security_config.get("hmac_enabled", True)
        
        self._card_config = self._feishu_config.get("card_template", {})
        self._theme_color = self._card_config.get("theme_color", "#1677ff")
        self._card_width = self._card_config.get("width", 500)
        
        self._client: Optional[httpx.AsyncClient] = None
    
    async def initialize(self) -> None:
        self._client = httpx.AsyncClient(timeout=10.0)
        logger.info("飞书通知器已初始化")
    
    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
    
    def _generate_signature(self, timestamp: str, secret: str) -> str:
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(hmac_code).decode("utf-8")

    def _add_signature(self, message: Dict[str, Any]) -> Dict[str, Any]:
        if not self._hmac_enabled or not self._secret:
            return message

        timestamp = str(int(time.time()))
        signed_message = message.copy()
        signed_message["timestamp"] = timestamp
        signed_message["sign"] = self._generate_signature(timestamp, self._secret)
        return signed_message

    @staticmethod
    def _is_success_response(result: Dict[str, Any]) -> bool:
        return result.get("code") == 0 or result.get("StatusCode") == 0
    
    def _build_card_message(self, email: EmailData) -> Dict[str, Any]:
        category = email.ai_category or "未分类"
        header_templates = {
            "工作沟通": "blue",
            "通知提醒": "orange",
            "垃圾邮件": "red",
            "社交动态": "purple",
            "个人事务": "green",
            "技术·文档": "turquoise",
        }
        header_template = header_templates.get(category, "grey")
        confidence_pct = int((email.ai_confidence or 0) * 100)
        confidence_label = self._get_confidence_label(confidence_pct)
        subject = self._truncate_text(email.subject or "(无主题)", 60)
        summary = self._truncate_text(email.ai_summary or email.short_preview or "无", 120)
        verification_code = self._extract_verification_code(email)

        summary_content = f"**摘要**\n{summary}"
        if verification_code:
            summary_content += f"\n\n**验证码**: <font color='red'>{verification_code}</font>"
        summary_content += f"\n\n**置信度**: {confidence_pct}% · {confidence_label}"
        
        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{email.display_sender or '未知发件人'}**\n"
                        f"<font color='grey'>{email.sender_email or '未知邮箱'}</font>"
                    )
                }
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": summary_content
                }
            },
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": (
                            f"账号: {email.account_email or '未知账号'}"
                            " · "
                            f"收到时间: {self._format_received_time(email.date)}"
                        )
                    }
                ]
            }
        ]
        
        card = {
            "msg_type": "interactive",
            "card": {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"[{category}] {subject}"
                    },
                    "template": header_template
                },
                "elements": elements
            }
        }
        
        return card

    @staticmethod
    def _get_confidence_label(confidence_pct: int) -> str:
        if confidence_pct >= 85:
            return "高"
        if confidence_pct >= 60:
            return "中"
        return "低"

    @staticmethod
    def _format_received_time(received_at: Optional[datetime]) -> str:
        if not received_at:
            return "未知"

        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)

        return received_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _truncate_text(text: str, max_length: int) -> str:
        text = " ".join((text or "").split())
        if len(text) <= max_length:
            return text
        return text[:max_length].rstrip() + "..."

    @classmethod
    def _extract_verification_code(cls, email: EmailData) -> Optional[str]:
        text = "\n".join(
            item
            for item in (
                email.subject,
                email.ai_summary,
                email.body_preview,
                email.body_plain[:500] if email.body_plain else "",
            )
            if item
        )
        return cls._extract_verification_code_from_text(text)

    # 验证码/校验码等关键词（中英文），用于正则和段落回退
    _VERIFICATION_KEYWORDS = (
        r"验证码|校验码|动态码|确认码|安全码|验证代码"
        r"|verification code|verify code|security code|one-time code|otp"
    )
    _VERIFICATION_KEYWORDS_GROUP = f"(?:{_VERIFICATION_KEYWORDS})"

    @classmethod
    def _extract_verification_code_from_text(cls, text: str) -> Optional[str]:
        """
        从文本中提取验证码。

        支持的格式：
        1. 关键词后紧跟验证码：您的验证码是 839201 / Your verification code: A19Z8
        2. 验证码在前、关键词在后：123456 是您的验证码
        3. 关键词和验证码跨段落 / 中间夹着描述：输入验证码授权新设备。\\n\\n858545
           （段落回退：定位到关键词后，在后续 200 字符窗口内挑第一个独立数字）
        """
        if not text:
            return None

        kw = cls._VERIFICATION_KEYWORDS_GROUP
        patterns = [
            # 1) 关键词在前：放宽中间允许 0-30 个非数字字符，覆盖关键词和数字之间
            #    夹着中文描述/换行的模板（Bitget、币安、PayPal 等）。
            rf"{kw}(?:是|为|is)?\D{{0,30}}?([A-Za-z0-9]{{4,10}})",
            # 2) 验证码在前、关键词在后
            rf"([A-Za-z0-9]{{4,10}})[\s，,。.;；]*(?:是|为|is)?[\s\S]{{0,12}}{kw}",
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                code = match.group(1).strip()
                if re.search(r"\d", code):
                    return code

        # 3) 段落回退：当关键词和验证码隔得较远，前两条都未匹配时，
        #    在关键词之后 200 字符窗口内挑第一个独立的 4-8 位数字。
        #    (?<![.\d]) / (?![.\d]) 排除 IP、订单号等带点/连字符的连续数字。
        keyword_match = re.search(kw, text, re.IGNORECASE)
        if keyword_match:
            window = text[keyword_match.end():keyword_match.end() + 200]
            for num_match in re.finditer(r"(?<![.\d])(\d{4,8})(?![.\d])", window):
                return num_match.group(1)

        return None
    
    def _should_block(self, email: EmailData) -> bool:
        if not self._keyword_filter_enabled:
            return False
        
        check_text = f"{email.subject} {email.body_preview}"
        
        for keyword in self._blocked_keywords:
            if keyword in check_text:
                logger.warning(f"邮件包含屏蔽关键词 '{keyword}'，跳过通知: {email.subject}")
                return True
        
        return False
    
    async def send_notification(self, email: EmailData) -> bool:
        if not self._enabled:
            logger.debug("飞书通知未启用")
            return False
        
        if not self._webhook_url:
            logger.warning("飞书Webhook URL未配置")
            return False
        
        if self._should_block(email):
            return False
        
        try:
            message = self._add_signature(self._build_card_message(email))
            
            if not self._client:
                await self.initialize()
            
            response = await self._client.post(
                self._webhook_url,
                json=message,
            )
            
            response.raise_for_status()
            result = response.json()
            
            if self._is_success_response(result):
                logger.info(f"飞书通知发送成功: {email.subject[:30]}...")
                return True
            else:
                logger.error(f"飞书通知发送失败: {result}")
                return False
                
        except httpx.HTTPStatusError as e:
            logger.error(f"飞书通知请求失败: {e.response.status_code} - {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"飞书通知发送异常: {e}")
            return False
    
    async def send_batch_notifications(self, emails: List[EmailData]) -> int:
        success_count = 0
        
        for email in emails:
            if await self.send_notification(email):
                success_count += 1
            await asyncio.sleep(0.5)
        
        return success_count
    
    async def send_text_message(self, text: str) -> bool:
        if not self._enabled or not self._webhook_url:
            return False
        
        try:
            if not self._client:
                await self.initialize()
            
            message = {
                "msg_type": "text",
                "content": {
                    "text": text
                }
            }
            
            response = await self._client.post(
                self._webhook_url,
                json=self._add_signature(message),
            )
            response.raise_for_status()
            result = response.json()

            if self._is_success_response(result):
                return True

            logger.error(f"发送文本消息失败: {result}")
            return False
            
        except Exception as e:
            logger.error(f"发送文本消息失败: {e}")
            return False
