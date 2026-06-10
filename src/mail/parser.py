"""
邮件解析模块
解析IMAP邮件为结构化数据
"""

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.header import decode_header
from email.message import Message
from typing import Any, Dict, List, Optional, Tuple

import mailparser
from loguru import logger

from ..config import get_config


@dataclass
class EmailData:
    """邮件数据结构"""
    message_id: str  # 邮件唯一标识
    uid: str  # IMAP UID
    account_email: str  # 所属账户邮箱
    
    # 基本信息
    sender: str  # 发件人
    sender_email: str  # 发件人邮箱
    recipients: List[str]  # 收件人列表
    subject: str  # 主题
    date: datetime  # 日期时间
    date_str: str  # 原始日期字符串
    
    # 内容
    body_plain: str  # 纯文本正文
    body_html: str  # HTML正文
    body_preview: str  # 正文预览（指定长度）
    has_attachments: bool  # 是否有附件
    attachments: List[Dict[str, Any]] = field(default_factory=list)  # 附件列表
    
    # AI分类结果
    ai_category: Optional[str] = None
    ai_confidence: Optional[float] = None
    ai_summary: Optional[str] = None
    
    # 归档信息
    archived_folder: Optional[str] = None
    processed: bool = False  # 是否已处理
    
    @property
    def display_sender(self) -> str:
        """显示用的发件人名称"""
        return self.sender or self.sender_email
    
    @property
    def short_preview(self) -> str:
        """简短预览（单行）"""
        preview = self.body_preview.replace("\n", " ").replace("\r", "")
        if len(preview) > 100:
            return preview[:100] + "..."
        return preview
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "message_id": self.message_id,
            "uid": self.uid,
            "account_email": self.account_email,
            "sender": self.sender,
            "sender_email": self.sender_email,
            "recipients": self.recipients,
            "subject": self.subject,
            "date": self.date.isoformat() if self.date else None,
            "date_str": self.date_str,
            "body_preview": self.body_preview,
            "has_attachments": self.has_attachments,
            "attachments": self.attachments,
            "ai_category": self.ai_category,
            "ai_confidence": self.ai_confidence,
            "ai_summary": self.ai_summary,
            "archived_folder": self.archived_folder,
            "processed": self.processed,
        }


class MailParser:
    """邮件解析器"""
    
    def __init__(self):
        self._config = get_config()
        self._preview_length = self._config.fetcher.get("body_preview_length", 200)
    
    @staticmethod
    def decode_header_value(header_value: str) -> str:
        """
        解码邮件头部值（处理编码）
        
        Args:
            header_value: 原始头部值
            
        Returns:
            解码后的字符串
        """
        if not header_value:
            return ""
        
        try:
            decoded_parts = decode_header(header_value)
            result = []
            
            for part, encoding in decoded_parts:
                if isinstance(part, bytes):
                    # 尝试解码
                    if encoding:
                        try:
                            result.append(part.decode(encoding))
                        except (UnicodeDecodeError, LookupError):
                            result.append(part.decode("utf-8", errors="replace"))
                    else:
                        result.append(part.decode("utf-8", errors="replace"))
                else:
                    result.append(part)
            
            return "".join(result)
            
        except Exception as e:
            logger.warning(f"解码头部失败: {e}")
            return header_value
    
    @staticmethod
    def extract_email_address(from_str: str) -> Tuple[str, str]:
        """
        从 From 头部提取姓名和邮箱
        
        Args:
            from_str: From 头部原始值
            
        Returns:
            (姓名, 邮箱) 元组
        """
        if not from_str:
            return "", ""
        
        try:
            # 格式: "姓名 <email>" 或 "email"
            match = re.match(r'"?([^"<]*?)"?\s*<(.+?)>', from_str)
            if match:
                name = match.group(1).strip()
                email = match.group(2).strip()
                return name, email
            
            # 纯邮箱格式
            if "@" in from_str:
                return "", from_str.strip()
            
            return from_str.strip(), ""
            
        except Exception as e:
            logger.warning(f"提取邮箱地址失败: {e}")
            return from_str, ""
    
    @staticmethod
    def clean_html(html: str) -> str:
        """
        清理HTML内容，提取纯文本
        
        Args:
            html: HTML内容
            
        Returns:
            纯文本内容
        """
        if not html:
            return ""
        
        try:
            import re
            
            # 移除脚本和样式
            html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
            
            # 替换常见标签
            html = html.replace('<br>', '\n')
            html = html.replace('<br/>', '\n')
            html = html.replace('<br />', '\n')
            html = html.replace('</p>', '\n\n')
            html = html.replace('</div>', '\n')
            html = html.replace('</li>', '\n')
            
            # 移除所有HTML标签
            html = re.sub(r'<[^>]+>', '', html)
            
            # 解码HTML实体
            html = html.replace('&nbsp;', ' ')
            html = html.replace('&lt;', '<')
            html = html.replace('&gt;', '>')
            html = html.replace('&amp;', '&')
            html = html.replace('&quot;', '"')
            html = html.replace('&#39;', "'")
            
            # 清理空白
            html = re.sub(r'\n\s*\n', '\n\n', html)
            html = html.strip()
            
            return html
            
        except Exception as e:
            logger.warning(f"清理HTML失败: {e}")
            return html
    
    async def parse_raw_email(
        self,
        raw_data: bytes,
        uid: str,
        account_email: str = "",
    ) -> Optional[EmailData]:
        """
        解析原始邮件数据
        
        Args:
            raw_data: 原始邮件数据
            uid: 邮件UID
            account_email: 账户邮箱
            
        Returns:
            EmailData对象，解析失败返回None
        """
        try:
            # 使用 mailparser 解析
            message = mailparser.parse_from_bytes(raw_data)

            if not self._has_mail_headers(message):
                logger.debug(f"跳过非邮件载荷: uid={uid}")
                return None
            
            # 提取基本信息
            subject = self.decode_header_value(message.subject or "")
            
            # 发件人 - mail-parser 4.x 使用 from_ 属性
            sender = ""
            sender_email = ""
            from_addrs = getattr(message, 'from_', None) or getattr(message, 'from', [])
            if from_addrs:
                # from_ 可能是 [(name, addr)] 或 [MailAddress]
                first_from = from_addrs[0]
                if isinstance(first_from, tuple):
                    sender = first_from[0] or ""
                    sender_email = first_from[1] or ""
                elif hasattr(first_from, 'name'):
                    # MailAddress 对象
                    sender = getattr(first_from, 'name', '') or ""
                    sender_email = getattr(first_from, 'addr', '') or ""
                else:
                    sender = str(first_from)
            
            # 如果没有提取到，尝试从 headers 获取
            if not sender_email:
                from_header = message.headers.get("From", "")
                sender, sender_email = self.extract_email_address(from_header)
            
            # 收件人 - mail-parser 4.x
            recipients = []
            to_addrs = getattr(message, 'to', []) or []
            for addr in to_addrs:
                if isinstance(addr, tuple):
                    recipients.append(addr[1] or addr[0])
                elif hasattr(addr, 'addr'):
                    # MailAddress 对象
                    recipients.append(addr.addr or addr.name or str(addr))
                else:
                    recipients.append(str(addr))
            
            # 日期
            date = getattr(message, 'date', None)
            date_str = message.headers.get("Date", "")
            
            if date is None:
                date = datetime.now()
                date_str = date.isoformat()
            
            # 提取正文 - mail-parser 4.x API
            # text_plain 和 text_html 是标准属性
            body_plain = ""
            body_html = ""
            
            text_plain = getattr(message, 'text_plain', None) or []
            text_html = getattr(message, 'text_html', None) or []
            
            if text_plain:
                if isinstance(text_plain, list):
                    body_plain = "\n".join(text_plain)
                else:
                    body_plain = str(text_plain)
            
            if text_html:
                if isinstance(text_html, list):
                    body_html = "\n".join(text_html)
                else:
                    body_html = str(text_html)
            
            # 如果没有纯文本但有HTML，清理HTML
            if not body_plain and body_html:
                body_plain = self.clean_html(body_html)
            
            # 生成预览
            body_preview = body_plain[:self._preview_length]
            
            # 提取附件 - mail-parser 4.x 格式
            attachments = []
            has_attachments = False
            
            mail_attachments = getattr(message, 'attachments', None) or []
            for attachment in mail_attachments:
                has_attachments = True
                # 兼容不同格式
                filename = ""
                content_type = ""
                payload = b""
                
                if isinstance(attachment, dict):
                    filename = attachment.get("filename", "")
                    content_type = attachment.get("content_type", "")
                    payload = attachment.get("payload", b"")
                elif hasattr(attachment, 'filename'):
                    filename = getattr(attachment, 'filename', '')
                    content_type = getattr(attachment, 'content_type', '')
                    payload = getattr(attachment, 'payload', b"")
                else:
                    filename = str(attachment)
                
                if isinstance(payload, str):
                    payload = payload.encode('utf-8')
                    
                attachments.append({
                    "filename": filename,
                    "content_type": content_type,
                    "size": len(payload) if payload else 0,
                })
            
            # 邮件唯一ID - mail-parser 4.x 使用 message_id 属性
            message_id = getattr(message, 'message_id', None)
            if not message_id:
                message_id = message.headers.get("Message-ID", f"{uid}@{account_email}")
            
            # 清理message_id中的尖括号
            message_id = str(message_id).strip("<>")
            
            return EmailData(
                message_id=message_id,
                uid=uid,
                account_email=account_email,
                sender=sender,
                sender_email=sender_email,
                recipients=recipients,
                subject=subject or "(无主题)",
                date=date,
                date_str=date_str,
                body_plain=body_plain,
                body_html=body_html,
                body_preview=body_preview,
                has_attachments=has_attachments,
                attachments=attachments,
            )
            
        except Exception as e:
            logger.error(f"解析邮件失败: {e}")
            return None

    @staticmethod
    def _has_mail_headers(message: Any) -> bool:
        headers = getattr(message, "headers", {}) or {}
        return any(
            headers.get(header)
            for header in ("From", "To", "Subject", "Date", "Message-ID", "Content-Type")
        )
    
    def get_body_for_ai(
        self,
        email: EmailData,
        include_secondary: bool = False,
    ) -> str:
        """
        获取发送给AI的邮件正文
        
        Args:
            email: 邮件数据
            include_secondary: 是否包含次要片段（200-500字）
            
        Returns:
            处理后的正文文本
        """
        if include_secondary:
            # 返回200-500字片段
            body = email.body_plain
            if len(body) > 200:
                return body[200:500]
            return body
        
        # 返回前200字
        return email.body_preview
    
    def prepare_ai_input(self, email: EmailData) -> str:
        """
        准备发送给AI的输入文本
        
        Args:
            email: 邮件数据
            
        Returns:
            格式化的输入文本
        """
        return f"""发件人: {email.display_sender} <{email.sender_email}>
主题: {email.subject}
正文: {email.body_preview}"""
