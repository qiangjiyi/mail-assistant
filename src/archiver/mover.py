"""
邮件归档模块
将邮件移动到对应分类的文件夹
"""

import asyncio
from typing import Dict, Optional

from loguru import logger

from ..config import get_config
from ..mail.connection import IMAPConnection
from ..mail.parser import EmailData


class MailArchiver:
    """邮件归档器"""
    
    def __init__(self, connection_manager):
        self._config = get_config()
        self._connection_manager = connection_manager
        
        self._archive_config = self._config.archive
        self._enabled = self._archive_config.get("enabled", True)
        self._default_folder = self._archive_config.get("default_folder", "INBOX")
        self._auto_create_folders = self._archive_config.get("auto_create_folders", True)
        
        self._category_folders = self._config.category_folders
    
    def get_target_folder(self, category: Optional[str], provider: str = "163") -> str:
        if not category:
            return self._default_folder
        
        provider_folders = self._category_folders.get(provider, {})
        folder = provider_folders.get(category)
        
        if not folder:
            for prov, folders in self._category_folders.items():
                if category in folders:
                    folder = folders[category]
                    break
        
        if not folder:
            logger.warning(f"分类 '{category}' 没有对应的文件夹配置，使用默认")
            return self._default_folder
        
        return folder
    
    def get_folder_for_email(self, email: EmailData) -> str:
        connection = self._connection_manager.get_connection(email.account_email)
        provider = connection.account.provider if connection else "163"
        return self.get_target_folder(email.ai_category, provider)
    
    async def archive_email(self, email: EmailData) -> bool:
        if not self._enabled:
            logger.debug("归档功能未启用")
            return False
        
        connection = self._connection_manager.get_connection(email.account_email)
        
        if not connection or not connection.is_connected:
            logger.error(f"无法获取连接: {email.account_email}")
            return False
        
        target_folder = await self._resolve_target_folder(connection, email)
        
        try:
            if self._auto_create_folders:
                await self._ensure_folder_exists(connection, target_folder)
            
            success = await connection.move_email(email.uid, target_folder)
            
            if success:
                email.archived_folder = target_folder
                logger.info(f"邮件已归档: {email.subject[:30]}... -> {target_folder}")
                return True
            else:
                logger.warning(f"邮件归档失败: {email.subject[:30]}...")
                return False
                
        except Exception as e:
            logger.error(f"归档邮件时出错: {e}")
            return False
    
    async def _ensure_folder_exists(self, connection: IMAPConnection, folder: str) -> bool:
        try:
            if await connection.folder_exists(folder):
                return True
            return await connection.create_folder(folder)
        except Exception as e:
            logger.debug(f"检查文件夹时出错: {folder} - {e}")
            return await connection.create_folder(folder)

    async def _resolve_target_folder(self, connection: IMAPConnection, email: EmailData) -> str:
        target_folder = self.get_folder_for_email(email)

        if connection.account.provider == "gmail" and email.ai_category == "垃圾邮件":
            junk_folder = await connection.get_special_use_folder("\\Junk")
            if junk_folder:
                return junk_folder

        return target_folder
    
    async def archive_batch(self, emails: list) -> Dict[str, int]:
        if not emails:
            return {"success": 0, "failed": 0}
        
        logger.info(f"开始批量归档 {len(emails)} 封邮件")
        
        success_count = 0
        failed_count = 0
        
        for email in emails:
            if await self.archive_email(email):
                success_count += 1
            else:
                failed_count += 1
            await asyncio.sleep(0.3)
        
        logger.info(f"批量归档完成: 成功 {success_count}, 失败 {failed_count}")
        return {"success": success_count, "failed": failed_count}
    
    def set_category_folder(self, category: str, folder: str, provider: str = "163") -> None:
        if provider not in self._category_folders:
            self._category_folders[provider] = {}
        self._category_folders[provider][category] = folder
        logger.info(f"已设置分类映射: {provider}/{category} -> {folder}")
    
    def get_all_mappings(self) -> Dict[str, str]:
        return self._category_folders.copy()
