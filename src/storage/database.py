"""
数据库模块
SQLite操作封装
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
from loguru import logger

from ..config import get_config
from ..mail.parser import EmailData


class Database:
    """SQLite数据库封装"""
    
    _instance: Optional["Database"] = None
    
    def __init__(self, db_path: str):
        """
        初始化数据库
        
        Args:
            db_path: 数据库文件路径
        """
        self._db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None
        
        # 确保目录存在
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    
    @classmethod
    async def create(cls, db_path: Optional[str] = None) -> "Database":
        """
        创建数据库实例
        
        Args:
            db_path: 数据库路径，默认使用配置
            
        Returns:
            Database实例
        """
        if cls._instance is not None:
            return cls._instance
        
        if db_path is None:
            config = get_config()
            db_path = config.database.get("path", "./data/mail_assistant.db")
        
        db = cls(db_path)
        await db.initialize()
        cls._instance = db
        return db
    
    @classmethod
    def get_instance(cls) -> Optional["Database"]:
        """获取单例实例"""
        return cls._instance
    
    async def initialize(self) -> None:
        """初始化数据库表"""
        logger.info(f"初始化数据库: {self._db_path}")
        
        self._connection = await aiosqlite.connect(self._db_path)
        self._connection.row_factory = aiosqlite.Row
        
        # 创建表
        await self._create_tables()
        
        logger.info("数据库初始化完成")
    
    async def _create_tables(self) -> None:
        """创建数据库表"""
        # 邮件表
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT UNIQUE NOT NULL,
                uid TEXT NOT NULL,
                account_email TEXT NOT NULL,
                sender TEXT,
                sender_email TEXT,
                recipients TEXT,
                subject TEXT,
                date TIMESTAMP,
                date_str TEXT,
                body_preview TEXT,
                has_attachments INTEGER DEFAULT 0,
                attachments TEXT,
                ai_category TEXT,
                ai_confidence REAL,
                ai_summary TEXT,
                archived_folder TEXT,
                processed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 日志表
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                account_email TEXT,
                email_subject TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 创建索引
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_emails_message_id ON emails(message_id)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_emails_account ON emails(account_email)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_emails_processed ON emails(processed)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at)"
        )
        
        await self._connection.commit()
    
    @asynccontextmanager
    async def transaction(self):
        """事务上下文管理器"""
        try:
            yield self._connection
            await self._connection.commit()
        except Exception:
            await self._connection.rollback()
            raise
    
    # ==================== 邮件操作 ====================
    
    async def save_email(self, email: EmailData) -> bool:
        """
        保存邮件到数据库
        
        Args:
            email: 邮件数据
            
        Returns:
            保存是否成功
        """
        try:
            await self._connection.execute(
                """
                INSERT OR REPLACE INTO emails (
                    message_id, uid, account_email, sender, sender_email,
                    recipients, subject, date, date_str, body_preview,
                    has_attachments, attachments, ai_category, ai_confidence,
                    ai_summary, archived_folder, processed, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    email.message_id,
                    email.uid,
                    email.account_email,
                    email.sender,
                    email.sender_email,
                    json.dumps(email.recipients),
                    email.subject,
                    email.date.isoformat() if email.date else None,
                    email.date_str,
                    email.body_preview[:1000],  # 限制长度
                    1 if email.has_attachments else 0,
                    json.dumps(email.attachments),
                    email.ai_category,
                    email.ai_confidence,
                    email.ai_summary,
                    email.archived_folder,
                    1 if email.processed else 0,
                ),
            )
            await self._connection.commit()
            return True
            
        except Exception as e:
            logger.error(f"保存邮件失败: {e}")
            return False
    
    async def get_email_by_message_id(self, message_id: str) -> Optional[Dict[str, Any]]:
        """根据message_id获取邮件"""
        cursor = await self._connection.execute(
            "SELECT * FROM emails WHERE message_id = ?",
            (message_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    
    async def get_email_by_uid(
        self,
        uid: str,
        account_email: str,
    ) -> Optional[Dict[str, Any]]:
        """根据UID和账户获取邮件"""
        cursor = await self._connection.execute(
            "SELECT * FROM emails WHERE uid = ? AND account_email = ?",
            (uid, account_email)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    
    async def is_email_processed(self, message_id: str) -> bool:
        """检查邮件是否已处理"""
        cursor = await self._connection.execute(
            "SELECT processed FROM emails WHERE message_id = ?",
            (message_id,)
        )
        row = await cursor.fetchone()
        return row is not None and row["processed"] == 1
    
    async def update_email_classification(
        self,
        message_id: str,
        category: str,
        confidence: float,
        summary: str,
    ) -> bool:
        """更新邮件分类结果"""
        try:
            await self._connection.execute(
                """
                UPDATE emails SET 
                    ai_category = ?,
                    ai_confidence = ?,
                    ai_summary = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE message_id = ?
                """,
                (category, confidence, summary, message_id)
            )
            await self._connection.commit()
            return True
        except Exception as e:
            logger.error(f"更新邮件分类失败: {e}")
            return False
    
    async def mark_email_archived(
        self,
        message_id: str,
        folder: str,
    ) -> bool:
        """标记邮件已归档"""
        try:
            await self._connection.execute(
                """
                UPDATE emails SET 
                    archived_folder = ?,
                    processed = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE message_id = ?
                """,
                (folder, message_id)
            )
            await self._connection.commit()
            return True
        except Exception as e:
            logger.error(f"标记邮件归档失败: {e}")
            return False
    
    async def get_unprocessed_emails(
        self,
        account_email: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """获取未处理的邮件"""
        if account_email:
            cursor = await self._connection.execute(
                """
                SELECT * FROM emails 
                WHERE account_email = ? AND processed = 0
                ORDER BY date DESC
                LIMIT ?
                """,
                (account_email, limit)
            )
        else:
            cursor = await self._connection.execute(
                """
                SELECT * FROM emails WHERE processed = 0
                ORDER BY date DESC LIMIT ?
                """,
                (limit,)
            )
        
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    
    async def get_emails_by_category(
        self,
        category: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """根据分类获取邮件"""
        cursor = await self._connection.execute(
            """
            SELECT * FROM emails 
            WHERE ai_category = ? AND processed = 1
            ORDER BY date DESC LIMIT ?
            """,
            (category, limit)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    
    # ==================== 统计操作 ====================
    
    async def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = {}
        
        # 总邮件数
        cursor = await self._connection.execute("SELECT COUNT(*) as count FROM emails")
        row = await cursor.fetchone()
        stats["total_emails"] = row["count"]
        
        # 已处理数
        cursor = await self._connection.execute(
            "SELECT COUNT(*) as count FROM emails WHERE processed = 1"
        )
        row = await cursor.fetchone()
        stats["processed_emails"] = row["count"]
        
        # 分类统计
        cursor = await self._connection.execute(
            """
            SELECT ai_category, COUNT(*) as count 
            FROM emails 
            WHERE ai_category IS NOT NULL
            GROUP BY ai_category
            """
        )
        rows = await cursor.fetchall()
        stats["category_stats"] = {row["ai_category"]: row["count"] for row in rows}
        
        # 各账户邮件数
        cursor = await self._connection.execute(
            """
            SELECT account_email, COUNT(*) as count 
            FROM emails 
            GROUP BY account_email
            """
        )
        rows = await cursor.fetchall()
        stats["account_stats"] = {row["account_email"]: row["count"] for row in rows}
        
        return stats

    async def cleanup_old_data(
        self,
        emails_retention_days: int = 180,
        logs_retention_days: int = 30,
    ) -> Dict[str, int]:
        """
        清理会持续增长的历史数据。

        只删除已处理且已归档的邮件索引，未处理邮件会保留，避免影响补归档。
        """
        result = {
            "emails_deleted": 0,
            "logs_deleted": 0,
        }

        try:
            async with self.transaction() as conn:
                if emails_retention_days > 0:
                    cursor = await conn.execute(
                        """
                        DELETE FROM emails
                        WHERE processed = 1
                          AND archived_folder IS NOT NULL
                          AND archived_folder != ''
                          AND updated_at < datetime('now', ?)
                        """,
                        (f"-{emails_retention_days} days",),
                    )
                    result["emails_deleted"] = cursor.rowcount if cursor.rowcount >= 0 else 0

                if logs_retention_days > 0:
                    cursor = await conn.execute(
                        """
                        DELETE FROM logs
                        WHERE created_at < datetime('now', ?)
                        """,
                        (f"-{logs_retention_days} days",),
                    )
                    result["logs_deleted"] = cursor.rowcount if cursor.rowcount >= 0 else 0

            return result
        except Exception as e:
            logger.warning(f"清理历史数据失败: {e}")
            return result
    
    # ==================== 日志操作 ====================
    
    async def log_operation(
        self,
        level: str,
        message: str,
        account_email: Optional[str] = None,
        email_subject: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """记录操作日志"""
        try:
            await self._connection.execute(
                """
                INSERT INTO logs (level, message, account_email, email_subject, details)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    level,
                    message,
                    account_email,
                    email_subject[:100] if email_subject else None,
                    json.dumps(details) if details else None,
                )
            )
            await self._connection.commit()
        except Exception as e:
            logger.warning(f"记录日志失败: {e}")
    
    async def get_recent_logs(
        self,
        level: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """获取最近的日志"""
        if level:
            cursor = await self._connection.execute(
                """
                SELECT * FROM logs 
                WHERE level = ? 
                ORDER BY created_at DESC 
                LIMIT ?
                """,
                (level, limit)
            )
        else:
            cursor = await self._connection.execute(
                """
                SELECT * FROM logs 
                ORDER BY created_at DESC 
                LIMIT ?
                """,
                (limit,)
            )
        
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    
    async def close(self) -> None:
        """关闭数据库连接"""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("数据库连接已关闭")
