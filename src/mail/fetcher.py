"""
邮件抓取模块
支持IDLE模式和轮询模式
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from dateutil import parser as date_parser
from loguru import logger

from .connection import IMAPConnection, IMAPAccount
from .parser import MailParser, EmailData
from ..config import get_config


class MailFetcher:
    """
    邮件抓取器
    支持Gmail IDLE模式和163轮询模式
    """
    
    def __init__(
        self,
        connection: IMAPConnection,
        parser: MailParser,
        on_emails_fetched: Optional[Callable[[List[EmailData]], Awaitable[None]]] = None,
    ):
        """
        初始化邮件抓取器
        
        Args:
            connection: IMAP连接
            parser: 邮件解析器
            on_emails_fetched: 邮件抓取完成回调
        """
        self.connection = connection
        self.parser = parser
        self.on_emails_fetched = on_emails_fetched
        
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._last_uid: Optional[str] = None
        
        self._config = get_config()
        self._fetcher_config = self._config.fetcher
    
    @property
    def account(self) -> IMAPAccount:
        """获取账户配置"""
        return self.connection.account
    
    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running
    
    async def start_polling(self) -> None:
        """开始轮询模式（163邮箱）"""
        if self.account.provider == "gmail":
            logger.warning(f"{self.account.name} 是Gmail，应使用IDLE模式")
            return
        
        self._running = True
        fetch_interval = self.account.fetch_interval
        
        logger.info(f"{self.account.name} 开始轮询模式 (间隔: {fetch_interval}秒)")
        
        while self._running:
            try:
                # 检查连接
                if not self.connection.is_connected:
                    logger.warning(f"{self.account.name} 断开，尝试重连...")
                    if not await self.connection.reconnect():
                        await asyncio.sleep(fetch_interval)
                        continue
                    
                    await self.connection.select_folder()
                
                # 抓取新邮件
                await self._fetch_new_emails()
                
                # 等待下次轮询
                await asyncio.sleep(fetch_interval)
                
            except asyncio.CancelledError:
                logger.info(f"{self.account.name} 轮询被取消")
                break
            except Exception as e:
                logger.error(f"{self.account.name} 轮询出错: {e}")
                await asyncio.sleep(10)
        
        logger.info(f"{self.account.name} 轮询结束")
    
    async def start_idle(self) -> None:
        """开始IDLE模式（Gmail）"""
        if self.account.provider != "gmail":
            logger.warning(f"{self.account.name} 不是Gmail，无法使用IDLE模式")
            return
        
        self._running = True
        
        # 设置回调
        self.connection.on_new_mail = self._on_idle_new_mail
        self.connection.on_idle_timeout = self._on_idle_timeout
        
        logger.info(f"{self.account.name} 开始IDLE模式")
        
        # 首次同步存量邮件
        if self._fetcher_config.get("process_existing", True):
            await self._fetch_existing_emails()
        
        # 开始IDLE监听
        await self.connection.idle_listen()
    
    async def _on_idle_new_mail(self, uids: List[str]) -> None:
        """IDLE模式检测到新邮件"""
        logger.info(f"{self.account.name} IDLE检测到新邮件")
        await self._fetch_new_emails()
    
    async def _on_idle_timeout(self) -> None:
        """IDLE超时处理"""
        logger.warning(f"{self.account.name} IDLE超时，正在重连...")
        await self.connection.reconnect()
        await self.connection.select_folder()
    
    async def stop(self) -> None:
        """停止抓取"""
        self._running = False
        
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        
        logger.info(f"{self.account.name} 抓取器已停止")
    
    async def _fetch_new_emails(self) -> List[EmailData]:
        """
        抓取新邮件
        
        Returns:
            新邮件数据列表
        """
        try:
            # 选择收件箱
            if not await self.connection.select_folder():
                return []
            
            # 搜索新邮件（自上次以来的）
            # 使用 UNSEEN 或 SINCE <date>
            if self._last_uid:
                # 从上次UID之后开始
                uids = await self.connection.search(f"UID {self._last_uid}:*")
            else:
                # 首次获取所有未读邮件
                uids = await self.connection.search("UNSEEN")
            
            if not uids:
                logger.debug(f"{self.account.name} 没有新邮件")
                return []
            
            logger.info(f"{self.account.name} 发现 {len(uids)} 封新邮件")
            
            # 解析邮件
            emails = await self._fetch_and_parse_emails(uids)
            
            # 更新最后UID
            if uids:
                self._last_uid = uids[-1]
            
            # 触发回调
            if emails and self.on_emails_fetched:
                await self.on_emails_fetched(emails)
            
            return emails
            
        except Exception as e:
            logger.error(f"{self.account.name} 抓取新邮件失败: {e}")
            return []
    
    async def _fetch_existing_emails(self) -> List[EmailData]:
        """
        抓取存量邮件（首次同步）
        
        Returns:
            存量邮件数据列表
        """
        try:
            logger.info(f"{self.account.name} 开始同步存量邮件...")
            
            if not await self.connection.select_folder():
                return []
            
            # 计算日期范围
            days = self.account.initial_days
            since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
            
            # 搜索指定日期之后的邮件
            uids = await self.connection.search(f"SINCE {since_date}")
            
            if not uids:
                logger.info(f"{self.account.name} 在过去 {days} 天内没有邮件")
                return []
            
            logger.info(f"{self.account.name} 发现 {len(uids)} 封存量邮件")
            
            # 批量处理
            batch_size = self.account.batch_size
            batch_delay = self.account.batch_delay
            
            all_emails = []
            
            for i in range(0, len(uids), batch_size):
                batch_uids = uids[i:i + batch_size]
                logger.debug(f"处理批次 {i//batch_size + 1}: {len(batch_uids)} 封邮件")
                
                emails = await self._fetch_and_parse_emails(batch_uids)
                all_emails.extend(emails)
                
                # 批次间延迟
                if i + batch_size < len(uids):
                    await asyncio.sleep(batch_delay)
            
            logger.info(f"{self.account.name} 存量邮件同步完成: {len(all_emails)} 封")
            
            return all_emails
            
        except Exception as e:
            logger.error(f"{self.account.name} 同步存量邮件失败: {e}")
            return []
    
    async def _fetch_and_parse_emails(self, uids: List[str]) -> List[EmailData]:
        """
        获取并解析邮件
        
        Args:
            uids: 邮件UID列表
            
        Returns:
            解析后的邮件数据列表
        """
        emails = []
        
        try:
            # 选择文件夹
            if not await self.connection.select_folder():
                return []
            
            # 批量获取邮件
            for uid in uids:
                try:
                    # 使用 FETCH 获取邮件内容
                    result = await asyncio.wait_for(
                        self.connection._connection.uid("FETCH", uid, "(RFC822)"),
                        timeout=self._config.get("imap.operation_timeout_seconds", 30),
                    )
                    
                    if result.result == "OK":
                        for line in result.lines:
                            if isinstance(line, (bytes, bytearray)) and self._looks_like_rfc822_payload(line):
                                # 解析邮件，确保传入 account_email
                                email_data = await self.parser.parse_raw_email(
                                    bytes(line), uid, account_email=self.account.email
                                )
                                if email_data:
                                    emails.append(email_data)
                                    logger.debug(f"解析邮件: {email_data.subject[:50]}...")
                    
                    # 避免请求过快
                    await asyncio.sleep(0.5)
                    
                except asyncio.TimeoutError:
                    self.connection._mark_disconnected(f"UID FETCH {uid} timeout")
                    logger.warning(f"{self.account.name} 获取邮件 {uid} 超时")
                    break
                except Exception as e:
                    logger.warning(f"获取邮件 {uid} 失败: {type(e).__name__}: {e!r}")
                    continue
            
        except Exception as e:
            logger.error(f"批量获取邮件失败: {e}")
        
        return emails

    @staticmethod
    def _looks_like_rfc822_payload(line: bytes | bytearray) -> bool:
        """过滤 IMAP FETCH 元数据，只保留真正的 RFC822 邮件内容。"""
        line = bytes(line)

        if b"\n" not in line:
            return False

        header_end = line.find(b"\r\n\r\n")
        if header_end == -1:
            header_end = line.find(b"\n\n")

        header_block = line[:header_end if header_end != -1 else min(len(line), 4096)]
        known_headers = (
            b"From:",
            b"To:",
            b"Subject:",
            b"Date:",
            b"Message-ID:",
            b"MIME-Version:",
            b"Content-Type:",
            b"Received:",
        )

        return any(header in header_block for header in known_headers)
    
    def start_polling_task(self) -> asyncio.Task:
        """启动轮询任务"""
        if self._poll_task:
            self._poll_task.cancel()
        
        self._poll_task = asyncio.create_task(self.start_polling())
        return self._poll_task
