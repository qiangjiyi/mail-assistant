"""
状态管理模块
管理服务运行状态和邮件处理状态
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from ..config import get_config


class StateManager:
    """状态管理器"""
    
    def __init__(self, state_file: str = "./data/state.json"):
        """
        初始化状态管理器
        
        Args:
            state_file: 状态文件路径
        """
        self._state_file = Path(state_file)
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        
        self._state: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        
        # 加载状态
        self._load_state()
    
    def _load_state(self) -> None:
        """从文件加载状态"""
        if self._state_file.exists():
            try:
                with open(self._state_file, "r", encoding="utf-8") as f:
                    self._state = json.load(f)
                logger.debug(f"状态已加载: {self._state_file}")
            except Exception as e:
                logger.warning(f"加载状态失败: {e}")
                self._state = {}
        else:
            self._state = {}
    
    async def _save_state(self) -> None:
        """保存状态到文件"""
        async with self._lock:
            try:
                with open(self._state_file, "w", encoding="utf-8") as f:
                    json.dump(self._state, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"保存状态失败: {e}")
    
    # ==================== 通用状态操作 ====================
    
    async def get(self, key: str, default: Any = None) -> Any:
        """获取状态值"""
        return self._state.get(key, default)
    
    async def set(self, key: str, value: Any) -> None:
        """设置状态值"""
        self._state[key] = value
        await self._save_state()
    
    async def delete(self, key: str) -> None:
        """删除状态值"""
        if key in self._state:
            del self._state[key]
            await self._save_state()
    
    async def update(self, updates: Dict[str, Any]) -> None:
        """批量更新状态"""
        self._state.update(updates)
        await self._save_state()
    
    # ==================== 邮件处理状态 ====================
    
    async def get_last_uid(self, account_email: str) -> Optional[str]:
        """获取账户最后处理的UID"""
        key = f"last_uid_{account_email}"
        return self._state.get(key)
    
    async def set_last_uid(self, account_email: str, uid: str) -> None:
        """设置账户最后处理的UID"""
        key = f"last_uid_{account_email}"
        self._state[key] = uid
        await self._save_state()
    
    async def get_last_sync_time(self, account_email: str) -> Optional[float]:
        """获取账户最后同步时间"""
        key = f"last_sync_{account_email}"
        return self._state.get(key)
    
    async def set_last_sync_time(self, account_email: str) -> None:
        """更新账户最后同步时间"""
        key = f"last_sync_{account_email}"
        self._state[key] = time.time()
        await self._save_state()
    
    # ==================== 批处理状态 ====================
    
    async def get_pending_batch(self, account_email: str) -> List[str]:
        """获取待处理的邮件ID列表"""
        key = f"pending_batch_{account_email}"
        return self._state.get(key, [])
    
    async def add_to_pending_batch(
        self,
        account_email: str,
        email_ids: List[str],
    ) -> None:
        """添加到待处理批次"""
        key = f"pending_batch_{account_email}"
        current = self._state.get(key, [])
        current.extend(email_ids)
        self._state[key] = current
        await self._save_state()
    
    async def clear_pending_batch(self, account_email: str) -> None:
        """清空待处理批次"""
        key = f"pending_batch_{account_email}"
        self._state[key] = []
        await self._save_state()
    
    # ==================== 服务状态 ====================
    
    async def get_service_state(self) -> Dict[str, Any]:
        """获取服务状态"""
        return {
            "running": self._state.get("service_running", False),
            "started_at": self._state.get("service_started_at"),
            "last_heartbeat": self._state.get("last_heartbeat"),
            "total_processed": self._state.get("total_processed", 0),
            "errors_count": self._state.get("errors_count", 0),
        }
    
    async def set_service_running(self, running: bool) -> None:
        """设置服务运行状态"""
        self._state["service_running"] = running
        if running:
            self._state["service_started_at"] = time.time()
        await self._save_state()
    
    async def update_heartbeat(self) -> None:
        """更新心跳时间"""
        self._state["last_heartbeat"] = time.time()
        await self._save_state()
    
    async def increment_processed(self) -> None:
        """增加已处理邮件计数"""
        self._state["total_processed"] = self._state.get("total_processed", 0) + 1
        await self._save_state()
    
    async def increment_errors(self) -> None:
        """增加错误计数"""
        self._state["errors_count"] = self._state.get("errors_count", 0) + 1
        await self._save_state()
    
    # ==================== 统计信息 ====================
    
    async def get_processing_stats(self) -> Dict[str, Any]:
        """获取处理统计"""
        return {
            "total_processed": self._state.get("total_processed", 0),
            "errors_count": self._state.get("errors_count", 0),
            "accounts": self._get_account_states(),
        }
    
    def _get_account_states(self) -> Dict[str, Any]:
        """获取各账户状态"""
        accounts = {}
        for key, value in self._state.items():
            if key.startswith("last_uid_"):
                email = key[10:]  # 去掉前缀
                accounts[email] = {
                    "last_uid": value,
                    "last_sync": self._state.get(f"last_sync_{email}"),
                }
        return accounts
    
    # ==================== 重置操作 ====================
    
    async def reset_account_state(self, account_email: str) -> None:
        """重置账户状态"""
        keys_to_remove = [
            f"last_uid_{account_email}",
            f"last_sync_{account_email}",
            f"pending_batch_{account_email}",
        ]
        
        for key in keys_to_remove:
            if key in self._state:
                del self._state[key]
        
        await self._save_state()
        logger.info(f"已重置账户状态: {account_email}")
    
    async def reset_all_state(self) -> None:
        """重置所有状态"""
        self._state = {}
        await self._save_state()
        logger.info("已重置所有状态")
