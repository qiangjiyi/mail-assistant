"""
邮箱整理助手 - 主服务入口
"""

import asyncio
import signal
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from .config import get_config, reload_config
from .mail import (
    ConnectionManager,
    IMAPAccount,
    MailFetcher,
    MailParser,
)
from .classifier import AIClassifier, RuleEngine
from .notifier import FeishuNotifier
from .archiver import MailArchiver
from .storage import Database, StateManager


class MailAssistantService:
    """
    邮箱整理助手服务
    
    协调各模块完成邮件抓取、分类、归档和通知
    """
    
    def __init__(self, config_path: Optional[str] = None):
        """
        初始化服务
        
        Args:
            config_path: 配置文件路径
        """
        self._config = reload_config(config_path)
        
        # 验证配置
        errors = self._config.validate()
        if errors:
            for error in errors:
                logger.warning(f"配置警告: {error}")
        
        # 组件
        self._connection_manager: Optional[ConnectionManager] = None
        self._parsers: Dict[str, MailParser] = {}
        self._fetchers: Dict[str, MailFetcher] = {}
        self._ai_classifier: Optional[AIClassifier] = None
        self._rule_engine: Optional[RuleEngine] = None
        self._notifier: Optional[FeishuNotifier] = None
        self._archiver: Optional[MailArchiver] = None
        self._database: Optional[Database] = None
        self._state_manager: Optional[StateManager] = None
        
        # 运行状态
        self._running = False
        self._tasks: List[asyncio.Task] = []
        
        # 心跳任务
        self._heartbeat_task: Optional[asyncio.Task] = None
    
    async def initialize(self) -> None:
        """初始化所有组件"""
        logger.info("初始化服务组件...")
        
        # 初始化数据库
        db_path = self._config.database.get("path", "./data/mail_assistant.db")
        self._database = await Database.create(db_path)
        
        # 初始化状态管理器
        self._state_manager = StateManager()
        
        # 初始化连接管理器
        self._connection_manager = ConnectionManager()
        
        # 添加邮箱账户连接
        for account_config in self._config.get_enabled_accounts():
            account = IMAPAccount.from_dict(account_config)
            self._connection_manager.add_connection(account)
            
            # 创建邮件解析器
            parser = MailParser()
            self._parsers[account.email] = parser
        
        # 连接所有账户
        logger.info("连接邮箱账户...")
        results = await self._connection_manager.connect_all()
        
        for email, success in results.items():
            if success:
                logger.info(f"✓ {email} 连接成功")
            else:
                logger.warning(f"✗ {email} 连接失败")
        
        # 初始化AI分类器
        self._ai_classifier = AIClassifier()
        
        # 初始化规则引擎
        self._rule_engine = RuleEngine()
        
        # 初始化飞书通知器
        self._notifier = FeishuNotifier()
        await self._notifier.initialize()
        
        # 初始化归档器
        self._archiver = MailArchiver(self._connection_manager)
        
        # 创建邮件抓取器
        for email, connection in self._connection_manager.connections.items():
            parser = self._parsers.get(email)
            if parser:
                fetcher = MailFetcher(
                    connection=connection,
                    parser=parser,
                    on_emails_fetched=self._on_emails_fetched,
                )
                self._fetchers[email] = fetcher
        
        logger.info("服务组件初始化完成")
    
    async def start(self, run_once: bool = False) -> None:
        """
        启动服务
        
        Args:
            run_once: 是否只运行一次（处理存量邮件）
        """
        if self._running:
            logger.warning("服务已在运行中")
            return
        
        # 初始化
        await self.initialize()
        
        self._running = True
        await self._state_manager.set_service_running(True)
        
        logger.info("=" * 50)
        logger.info("邮箱整理助手服务启动")
        logger.info("=" * 50)
        
        # 启动心跳
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        
        if run_once:
            # 只处理存量邮件
            await self._process_existing_emails()
            logger.info("存量邮件处理完成，服务退出")
            await self.stop()
            return
        
        # 启动邮件抓取任务
        await self._start_fetchers()
        
        # 主循环
        try:
            while self._running:
                await asyncio.sleep(10)

                # 检查连接状态
                if self._running:
                    await self._check_connections()

                # Gmail IDLE 看门狗（解决 aioimaplib 卡死导致 IDLE 永远不超时的问题）
                if self._running:
                    await self._check_idle_health()

        except asyncio.CancelledError:
            logger.info("服务被取消")
        finally:
            await self.stop()
    
    async def stop(self) -> None:
        """停止服务"""
        if not self._running:
            return
        
        logger.info("正在停止服务...")
        self._running = False
        
        # 取消所有任务
        for task in self._tasks:
            task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        
        # 停止抓取器
        for fetcher in self._fetchers.values():
            await fetcher.stop()
        
        # 断开连接
        if self._connection_manager:
            await self._connection_manager.disconnect_all()
        
        # 关闭数据库
        if self._database:
            await self._database.close()
        
        # 关闭通知器
        if self._notifier:
            await self._notifier.close()
        
        await self._state_manager.set_service_running(False)
        
        logger.info("服务已停止")
    
    async def _start_fetchers(self) -> None:
        """启动邮件抓取器"""
        for email, fetcher in self._fetchers.items():
            account = fetcher.account

            if account.provider == "gmail":
                # Gmail使用IDLE模式
                logger.info(f"{email} 启动IDLE模式")
                task = await fetcher.start_idle()
                if task:
                    self._tasks.append(task)
            else:
                # 163使用轮询模式
                logger.info(f"{email} 启动轮询模式")
                task = fetcher.start_polling_task()
                self._tasks.append(task)

    async def _check_idle_health(self) -> None:
        """
        Gmail IDLE 看门狗：

        IDLE 命令底层可能在 `idle()` 处无限阻塞（Gmail 静默断开后 aioimaplib 不返回也不抛异常），
        这时 idle_timeout 永远不会触发。看门狗通过定期检查 last_idle_heartbeat 来发现僵死的 IDLE，
        强制取消 task 并重启。
        """
        # 540s IDLE 超时 + 缓冲；保留至少一轮 NOOP 的余量
        idle_stale_threshold = 12 * 60
        # 看门狗重启循环熔断：300s 内 restart 超过这个次数 → 视为 watchdog 自己在死循环重启，
        # 强制再重启一次（防止被同一份坏状态反复唤醒）。
        # 阈值 3：用户场景是 1 Gmail + 1 轮询，正常时 watchdog 几乎不触发；
        # 连续 3 次重启仍无心跳，说明底层问题持久，再多重启也是徒劳——保留信号给上层。
        idle_restart_window_seconds = 300
        idle_restart_threshold = 3
        now = time.time()

        for email, fetcher in list(self._fetchers.items()):
            if fetcher.account.provider != "gmail":
                continue

            connection = fetcher.connection
            last_beat = connection._state.last_idle_heartbeat
            if last_beat <= 0:
                # 还没建立过心跳，跳过
                continue

            if connection._state.restart_in_progress:
                # 已经在重启流程中
                continue

            recent_restarts = fetcher.recent_idle_starts(idle_restart_window_seconds)

            if now - last_beat <= idle_stale_threshold:
                # 心跳还新鲜，但 watchdog 短时间内反复重启过，判定为重启循环
                if recent_restarts > idle_restart_threshold:
                    logger.warning(
                        f"Gmail IDLE 看门狗重启循环 "
                        f"({recent_restarts} 次/{idle_restart_window_seconds}s)，"
                        f"强制再重启一次: {email}"
                    )
                else:
                    continue
            else:
                logger.warning(
                    f"Gmail IDLE 心跳超时 ({int(now - last_beat)}s 无心跳)，强制重启: {email}"
                )
            try:
                new_task = await fetcher.restart_idle()
                if new_task is None:
                    continue
                # 清理已结束的 task，把新 task 挂上去
                self._tasks = [t for t in self._tasks if not t.done()]
                self._tasks.append(new_task)
            except Exception as e:
                logger.error(f"重启 Gmail IDLE 失败 ({email}): {e}")
    
    async def _check_connections(self) -> None:
        """检查连接状态"""
        if not self._running:
            return

        if self._connection_manager:
            connected = self._connection_manager.connected_count
            total = len(self._connection_manager.connections)
            
            if connected < total:
                logger.warning(f"部分连接断开: {connected}/{total}")
                # 尝试重连
                await self._connection_manager.reconnect_all()
    
    async def _process_existing_emails(self) -> None:
        """处理存量邮件"""
        logger.info("开始处理存量邮件...")

        tasks = [
            asyncio.create_task(
                self._process_existing_account(email, fetcher),
                name=f"process-existing:{email}",
            )
            for email, fetcher in self._fetchers.items()
        ]

        if not tasks:
            logger.info("没有可处理的邮箱账户")
            return

        await asyncio.gather(*tasks)

    async def _process_existing_account(self, email: str, fetcher: MailFetcher) -> None:
        """处理单个邮箱的存量邮件。"""
        try:
            # 选择文件夹
            if not await fetcher.connection.select_folder():
                return

            # 获取存量邮件
            emails = await fetcher._fetch_existing_emails()

            if emails:
                logger.info(f"{email}: 发现 {len(emails)} 封存量邮件")
                processed_emails = await self._process_emails(
                    emails,
                    send_individual_notifications=False,
                )
                await self._send_existing_summary(email, processed_emails)

        except Exception as e:
            logger.error(f"处理存量邮件失败 {email}: {e}")
            await self._state_manager.increment_errors()
    
    async def _on_emails_fetched(self, emails: List) -> None:
        """
        新邮件回调
        
        Args:
            emails: 邮件列表
        """
        if not emails:
            return
        
        logger.info(f"收到 {len(emails)} 封新邮件")
        
        # 处理邮件
        await self._process_emails(emails)
    
    async def _process_emails(
        self,
        emails: List,
        send_individual_notifications: bool = True,
    ) -> List:
        """
        处理邮件列表
        
        Args:
            emails: 邮件列表
        """
        processed_emails = []

        for email_data in emails:
            finalize = False  # 控制下半段（通知 + 计数）是否执行
            try:
                # 检查是否已处理
                existing_email = await self._database.get_email_by_message_id(email_data.message_id)
                if existing_email and existing_email.get("processed") == 1:
                    if existing_email.get("archived_folder"):
                        email_data.ai_category = existing_email.get("ai_category")
                        email_data.ai_confidence = existing_email.get("ai_confidence")
                        email_data.ai_summary = existing_email.get("ai_summary")
                        email_data.archived_folder = existing_email.get("archived_folder")

                        logger.info(f"已处理邮件仍在收件箱，尝试补归档: {email_data.subject[:30]}")
                        if self._archive_config.get("enabled", True):
                            try:
                                success = await self._archiver.archive_email(email_data)
                            except Exception as e:
                                logger.error(f"补归档异常: {email_data.subject[:30]}: {e}")
                                success = False
                            if success:
                                await self._database.mark_email_archived(
                                    message_id=email_data.message_id,
                                    folder=email_data.archived_folder,
                                )
                                processed_emails.append(email_data)
                    else:
                        logger.debug(f"邮件已处理且无归档目标，跳过: {email_data.subject[:30]}")
                    continue

                # 保存到数据库
                await self._database.save_email(email_data)

                # AI分类
                classification = await self._ai_classifier.classify_email_with_retry(email_data)

                # 规则增强
                classification = self._rule_engine.enhance_ai_result(email_data, classification)

                # 更新分类结果
                self._ai_classifier.apply_result_to_email(email_data, classification)

                # 更新数据库
                await self._database.update_email_classification(
                    message_id=email_data.message_id,
                    category=email_data.ai_category,
                    confidence=email_data.ai_confidence,
                    summary=email_data.ai_summary,
                )

                # 归档（独立 try/except，失败不影响后续通知和计数）
                archive_success = False
                if self._archive_config.get("enabled", True):
                    try:
                        archive_success = await self._archiver.archive_email(email_data)
                    except Exception as e:
                        logger.error(
                            f"归档异常: {email_data.subject[:30]}: {type(e).__name__}: {e}"
                        )
                    if archive_success:
                        try:
                            await self._database.mark_email_archived(
                                message_id=email_data.message_id,
                                folder=email_data.archived_folder,
                            )
                        except Exception as e:
                            logger.error(
                                f"标记归档失败: {email_data.subject[:30]}: {e}"
                            )

                finalize = True
                processed_emails.append(email_data)

            except Exception as e:
                logger.error(f"处理邮件失败: {e}")
                await self._state_manager.increment_errors()
                continue

            # ===== 下半段：通知 + 计数 =====
            # 走到这里说明：分类、解析、DB 写入都成功。归档失败/超时不影响通知和计数。
            if not finalize:
                continue

            if send_individual_notifications:
                try:
                    await self._notifier.send_notification(email_data)
                except Exception as e:
                    # send_notification 内部已全捕获，这里再兜一层
                    logger.error(
                        f"发送通知异常: {email_data.subject[:30]}: {type(e).__name__}: {e}"
                    )

            try:
                await self._state_manager.increment_processed()
                await self._state_manager.set_last_uid(
                    email_data.account_email,
                    email_data.uid,
                )
                await self._database.log_operation(
                    level="INFO",
                    message=f"邮件处理完成: {email_data.subject}",
                    account_email=email_data.account_email,
                    email_subject=email_data.subject,
                    details={
                        "category": email_data.ai_category,
                        "confidence": email_data.ai_confidence,
                        "folder": email_data.archived_folder,
                    },
                )
            except Exception as e:
                logger.error(
                    f"更新状态失败: {email_data.subject[:30]}: {type(e).__name__}: {e}"
                )

            logger.info(
                f"✓ {email_data.subject[:40]}... -> "
                f"{email_data.ai_category} ({email_data.ai_confidence:.0%})"
            )

        return processed_emails

    async def _send_existing_summary(self, account_email: str, emails: List) -> None:
        """发送存量邮件处理汇总，避免首次整理逐封刷屏。"""
        if not emails:
            return

        category_counts = Counter(email.ai_category or "未分类" for email in emails)
        folder_counts = Counter(email.archived_folder or "未归档" for email in emails)

        category_text = "、".join(
            f"{category} {count} 封"
            for category, count in category_counts.most_common()
        )
        folder_text = "、".join(
            f"{folder} {count} 封"
            for folder, count in folder_counts.most_common()
        )

        text = (
            f"邮箱整理完成\n"
            f"账号：{account_email}\n"
            f"处理：{len(emails)} 封\n"
            f"分类：{category_text}\n"
            f"归档：{folder_text}"
        )
        await self._notifier.send_text_message(text)
    
    async def _heartbeat_loop(self) -> None:
        """心跳循环"""
        while self._running:
            try:
                await asyncio.sleep(60)  # 每分钟心跳
                await self._state_manager.update_heartbeat()
                
                # 定期保存统计
                stats = await self._state_manager.get_processing_stats()
                logger.debug(f"心跳: 已处理 {stats['total_processed']} 封邮件")

                await self._run_database_cleanup_if_needed()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"心跳异常: {e}")

    async def _run_database_cleanup_if_needed(self) -> None:
        """按配置定期清理数据库历史数据。"""
        database_config = self._config.database
        cleanup_config = database_config.get("cleanup", {})

        if not cleanup_config.get("enabled", True):
            return

        interval_hours = cleanup_config.get("interval_hours", 24)
        now = time.time()
        last_cleanup_at = await self._state_manager.get("last_database_cleanup_at", 0)

        if last_cleanup_at and now - last_cleanup_at < interval_hours * 3600:
            return

        result = await self._database.cleanup_old_data(
            emails_retention_days=cleanup_config.get("processed_emails_retention_days", 180),
            logs_retention_days=cleanup_config.get("logs_retention_days", 30),
        )
        await self._state_manager.set("last_database_cleanup_at", now)

        if result["emails_deleted"] or result["logs_deleted"]:
            logger.info(
                "数据库历史数据清理完成: "
                f"邮件 {result['emails_deleted']} 条, "
                f"日志 {result['logs_deleted']} 条"
            )
    
    @property
    def _archive_config(self) -> Dict:
        """归档配置"""
        return self._config.archive
    
    @property
    def status(self) -> Dict:
        """获取服务状态"""
        return {
            "running": self._running,
            "connections": {
                email: conn.is_connected
                for email, conn in self._connection_manager.connections.items()
            } if self._connection_manager else {},
            "statistics": asyncio.run(self._state_manager.get_processing_stats()) if self._state_manager else {},
        }
