"""
邮件抓取模块
支持IDLE模式和轮询模式
"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

from dateutil import parser as date_parser
from loguru import logger

from .connection import IMAPConnection, IMAPAccount
from .parser import MailParser, EmailData
from ..config import get_config
from ..storage.state import StateManager


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
        state_manager: Optional[Any] = None,
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
        self._state_manager = state_manager
        
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._last_uid: Optional[str] = None
        # 频率熔断：跟踪 IDLE 启动时间戳，看门狗从这里也能识别"自旋"。
        # 跟 connection 里的退避是两条独立防线——退避负责不再自旋，
        # 熔断负责在退避失效时（代码 bug / 改坏）也能在 main 看门狗里被抓住。
        self._idle_start_log: List[float] = []

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
        await self._load_last_uid()
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
    
    async def start_idle(
        self,
        skip_existing: bool = False,
        catch_up: bool = False,
    ) -> asyncio.Task:
        """开始IDLE模式（Gmail），返回后台 task 给上层管理生命周期。"""
        if self.account.provider != "gmail":
            logger.warning(f"{self.account.name} 不是Gmail，无法使用IDLE模式")
            return None

        self._running = True
        await self._load_last_uid()

        # 设置回调
        self.connection.on_new_mail = self._on_idle_new_mail
        self.connection.on_idle_timeout = self._on_idle_timeout

        logger.info(f"{self.account.name} 启动IDLE模式")

        async def _run():
            try:
                # Gmail 已有 last_uid 时只做增量补抓。不能再按日期做存量扫描，
                # 否则 Gmail 里 Header Date 很早但仍留在 INBOX 的旧邮件会被当作新邮件推送。
                if not skip_existing and self._fetcher_config.get("process_existing", True):
                    if self._last_uid:
                        await self._catch_up_new_emails()
                    else:
                        emails = await self._fetch_existing_emails()
                        # 只有没有 last_uid 的首次启动才处理存量。
                        if emails and self.on_emails_fetched:
                            await self.on_emails_fetched(emails)
                            parsed_uids = [email.uid for email in emails if email.uid]
                            max_uid = self._max_uid(parsed_uids)
                            if max_uid:
                                await self._remember_last_uid(max_uid)

                # 看门狗重启时，旧 IDLE 连接可能已经错过 EXISTS 推送。
                # 进入新 IDLE 前按 last_uid 做一次增量补抓，避免邮件一直漏在 INBOX。
                if catch_up:
                    await self._catch_up_new_emails()

                # 开始IDLE监听（阻塞直到被 cancel 或自然结束）
                await self.connection.idle_listen()
            except asyncio.CancelledError:
                logger.info(f"{self.account.name} IDLE task 被取消")
                raise
            finally:
                if asyncio.current_task() is self._idle_task:
                    self._running = False

        self._idle_task = asyncio.create_task(_run(), name=f"idle:{self.account.email}")
        self._record_idle_start()
        return self._idle_task

    def _record_idle_start(self) -> None:
        """记录一次 IDLE 启动时间戳；main 看门狗用这个判断是否在自旋。"""
        now = time.time()
        self._idle_start_log.append(now)
        # 只保留最近 5 分钟的样本，避免列表无限增长
        cutoff = now - 300
        self._idle_start_log = [t for t in self._idle_start_log if t >= cutoff]

    def recent_idle_starts(self, window_seconds: int = 60) -> int:
        """返回最近 window_seconds 秒内 IDLE 启动次数。"""
        cutoff = time.time() - window_seconds
        return sum(1 for t in self._idle_start_log if t >= cutoff)

    async def restart_idle(self) -> asyncio.Task:
        """
        看门狗调用：取消当前 IDLE 任务，强制断连，重建新任务。

        返回新 task，调用方需要把它替换到自己的 task 列表里。
        """
        logger.warning(f"{self.account.name} IDLE 心跳超时，看门狗强制重启")
        self.connection.mark_idle_unhealthy()

        # 先关闭底层 transport：让卡在 socket 读写的 idle task 立即收到 EOF，
        # 否则 cancel 后的 await 会永远挂住（底层 aioimaplib 不响应取消）。
        try:
            self.connection._kill_transport()
        except Exception:
            pass

        # 取消旧 task。这里不能用 wait_for(task)，因为底层 socket 读写不响应取消时，
        # wait_for 也会继续等待 task 真正取消完成，反而把看门狗一起卡住。
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            done, pending = await asyncio.wait({self._idle_task}, timeout=10)
            for task in done:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            if pending:
                logger.warning(f"{self.account.name} 旧IDLE task 10秒内未退出，放弃等待")

        # 断连、清状态
        try:
            await self.connection.force_restart_idle()
        except Exception as e:
            logger.error(f"{self.account.name} 强制断连失败: {e}")

        # 重置重连计数：看门狗重启是新周期，不应继承旧 task 的累积重连次数
        self.connection._state.reconnect_count = 0

        # 起新 task（跳过存量同步，但先做增量补抓）
        return await self.start_idle(skip_existing=True, catch_up=True)

    async def _catch_up_new_emails(self) -> List[EmailData]:
        """重建 IDLE 前补抓上次 UID 之后的新邮件。"""
        try:
            if not self.connection.is_connected:
                logger.info(f"{self.account.name} IDLE重启后补抓前先重连")
                if not await self.connection.reconnect():
                    logger.warning(f"{self.account.name} IDLE重启补抓失败：无法重连")
                    return []

            emails = await self._fetch_new_emails()
            if emails:
                logger.info(f"{self.account.name} IDLE重启补抓到 {len(emails)} 封新邮件")
            return emails
        except Exception as e:
            logger.error(f"{self.account.name} IDLE重启补抓异常: {type(e).__name__}: {e}")
            return []
    
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
                uids = await self.connection.search(f"UID {self._next_uid_criteria(self._last_uid)}:*")
                uids = self._filter_uids_after_last(uids)
            else:
                # 首次获取所有未读邮件
                uids = await self.connection.search("UNSEEN")
            
            if not uids:
                logger.debug(f"{self.account.name} 没有新邮件")
                return []
            
            logger.info(f"{self.account.name} 发现 {len(uids)} 封新邮件")
            
            # 解析邮件
            emails = await self._fetch_and_parse_emails(uids)

            # 触发回调
            if emails and self.on_emails_fetched:
                await self.on_emails_fetched(emails)

            # 只在实际解析成功后推进 UID，避免 SELECT/FETCH 超时后跳过邮件。
            parsed_uids = [email.uid for email in emails if email.uid]
            max_uid = self._max_uid(parsed_uids)
            if max_uid:
                await self._remember_last_uid(max_uid)
            
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

    async def _load_last_uid(self) -> None:
        """从持久化状态恢复最后处理 UID，服务重启后继续增量抓取。"""
        if self._last_uid or self._state_manager is None:
            return

        try:
            last_uid = await self._state_manager.get_last_uid(self.account.email)
        except Exception as e:
            logger.debug(f"{self.account.name} 读取 last_uid 失败: {e}")
            return

        if last_uid:
            self._last_uid = str(last_uid)
            logger.debug(f"{self.account.name} 恢复 last_uid: {self._last_uid}")

    async def _remember_last_uid(self, uid: str) -> None:
        """更新内存和持久化 last UID。"""
        uid = str(uid)
        if self._last_uid and not self._uid_is_after(uid, self._last_uid):
            logger.debug(
                f"{self.account.name} 忽略倒退 last_uid: {uid} <= {self._last_uid}"
            )
            return

        self._last_uid = uid

        if self._state_manager is None:
            return

        try:
            await self._state_manager.set_last_uid(self.account.email, self._last_uid)
        except Exception as e:
            logger.debug(f"{self.account.name} 保存 last_uid 失败: {e}")

    @staticmethod
    def _next_uid_criteria(last_uid: str) -> str:
        """返回 IMAP UID 搜索起点，避免重复抓取 last_uid 本身。"""
        try:
            return str(int(last_uid) + 1)
        except (TypeError, ValueError):
            return str(last_uid)

    def _filter_uids_after_last(self, uids: List[str]) -> List[str]:
        """本地兜底过滤，防止 IMAP 服务端/库返回小于 last_uid 的旧 UID。"""
        if not self._last_uid:
            return uids

        filtered = [uid for uid in uids if self._uid_is_after(uid, self._last_uid)]
        skipped = len(uids) - len(filtered)
        if skipped:
            logger.warning(
                f"{self.account.name} 跳过 {skipped} 个不大于 last_uid "
                f"{self._last_uid} 的旧 UID"
            )
        return filtered

    @staticmethod
    def _uid_is_after(candidate: str, current: str) -> bool:
        """判断 candidate 是否比 current 更新。委托给 StateManager 同名实现。"""
        return StateManager._is_uid_after(candidate, current)

    @classmethod
    def _max_uid(cls, uids: List[str]) -> Optional[str]:
        """返回 UID 列表中的最大值。"""
        if not uids:
            return None

        try:
            return str(max(int(uid) for uid in uids))
        except (TypeError, ValueError):
            return max(str(uid) for uid in uids)

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
