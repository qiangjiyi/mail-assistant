"""
IMAP连接管理模块
负责邮箱连接、重连和连接池管理
"""

import asyncio
import base64
import re
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable, Awaitable

import aioimaplib
from aioimaplib import IMAP4_SSL
from aioimaplib.aioimaplib import Command
from loguru import logger

from ..config import get_config


@dataclass
class IMAPAccount:
    """邮箱账户配置"""
    name: str
    provider: str
    email: str
    imap_host: str
    imap_port: int
    username: str
    password: str
    folder_prefix: str = ""
    idle_timeout: int = 540  # Gmail IDLE超时（秒）
    reconnect_timeout: int = 1800  # 重连超时
    fetch_interval: int = 60  # 轮询间隔
    initial_days: int = 30  # 首次同步天数
    batch_size: int = 50  # 批次大小
    batch_delay: int = 5  # 批次延迟
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IMAPAccount":
        """从字典创建实例"""
        return cls(
            name=data.get("name", ""),
            provider=data.get("provider", "unknown"),
            email=data.get("email", ""),
            imap_host=data.get("imap_host", ""),
            imap_port=data.get("imap_port", 993),
            username=data.get("username", ""),
            password=data.get("password", ""),
            folder_prefix=data.get("folder_prefix", ""),
            idle_timeout=data.get("idle_timeout_seconds", 540),
            reconnect_timeout=data.get("reconnect_timeout_seconds", 1800),
            fetch_interval=data.get("fetch_interval_seconds", 60),
            initial_days=data.get("initial_days", 30),
            batch_size=data.get("batch_size", 50),
            batch_delay=data.get("batch_delay_seconds", 5),
        )


@dataclass
class ConnectionState:
    """连接状态"""
    connected: bool = False
    last_connected: Optional[float] = None
    last_error: Optional[str] = None
    reconnect_count: int = 0


class IMAPConnection:
    """IMAP连接封装"""
    
    def __init__(
        self,
        account: IMAPAccount,
        on_new_mail: Optional[Callable[[List[str]], Awaitable[None]]] = None,
        on_idle_timeout: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        """
        初始化IMAP连接
        
        Args:
            account: 邮箱账户配置
            on_new_mail: 新邮件回调函数
            on_idle_timeout: IDLE超时回调
        """
        self.account = account
        self.on_new_mail = on_new_mail
        self.on_idle_timeout = on_idle_timeout
        
        self._connection: Optional[IMAP4_SSL] = None
        self._state = ConnectionState()
        self._idle_task: Optional[asyncio.Task] = None
        self._running = False
        self._special_use_cache: Dict[str, str] = {}
        
        # 重连配置
        config = get_config()
        self._reconnect_config = config.reconnect
        self._operation_timeout = config.get("imap.operation_timeout_seconds", 30)
    
    @property
    def is_connected(self) -> bool:
        """检查是否已连接"""
        if self._connection is None or not self._state.connected:
            return False
        try:
            return self._connection.protocol.state != 'LOGOUT'
        except Exception:
            return self._state.connected
    
    async def connect(self) -> bool:
        """
        建立IMAP连接
        
        Returns:
            连接是否成功
        """
        try:
            logger.info(f"正在连接 {self.account.name} ({self.account.imap_host})...")
            
            # 创建SSL上下文
            ssl_context = ssl.create_default_context()
            
            # 创建连接
            self._connection = IMAP4_SSL(
                host=self.account.imap_host,
                port=self.account.imap_port,
                ssl_context=ssl_context,
                timeout=30,
            )
            
            # 等待连接就绪
            await self._connection.wait_hello_from_server()
            
            # 登录
            await self._connection.login(
                user=self.account.username,
                password=self.account.password,
            )

            await self._send_client_id_if_required()
            
            self._state.connected = True
            self._state.last_connected = time.time()
            self._state.reconnect_count = 0
            self._state.last_error = None
            
            logger.info(f"成功连接 {self.account.name}")
            return True
            
        except Exception as e:
            logger.error(f"连接 {self.account.name} 失败: {e}")
            self._state.last_error = str(e)
            self._connection = None
            return False

    async def _send_client_id_if_required(self) -> None:
        """网易系邮箱需要客户端 ID，否则可能在 SELECT 阶段拒绝访问。"""
        if not self._requires_client_id():
            return

        if self._connection is None:
            return

        client_id = (
            '("name" "mail_assistant" '
            '"version" "1.0.0" '
            '"vendor" "local" '
            '"contact" "user")'
        )
        protocol = self._connection.protocol
        command = Command("ID", protocol.new_tag(), client_id, loop=protocol.loop)
        result = await asyncio.wait_for(protocol.execute(command), self._connection.timeout)

        if result.result == "OK":
            logger.debug(f"{self.account.name} 已发送 IMAP ID")
        else:
            logger.warning(f"{self.account.name} 发送 IMAP ID 失败: {result}")

    def _requires_client_id(self) -> bool:
        """判断账号是否属于需要 IMAP ID 的网易系邮箱。"""
        provider = self.account.provider.lower()
        host = self.account.imap_host.lower()
        email = self.account.email.lower()

        netease_providers = {"163", "126", "188", "yeah", "netease"}
        netease_domains = (
            "imap.163.com",
            "imap.126.com",
            "imap.188.com",
            "imap.yeah.net",
        )
        netease_email_suffixes = (
            "@163.com",
            "@126.com",
            "@188.com",
            "@yeah.net",
        )

        return (
            provider in netease_providers
            or host in netease_domains
            or email.endswith(netease_email_suffixes)
        )
    
    async def disconnect(self) -> None:
        """断开连接"""
        self._running = False
        
        if self._idle_task:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None
        
        if self._connection is not None and self.is_connected:
            try:
                await asyncio.wait_for(self._connection.logout(), timeout=2)
                logger.info(f"已断开 {self.account.name}")
            except asyncio.TimeoutError:
                logger.warning(f"{self.account.name} 断开连接超时，强制关闭本地连接")
            except Exception as e:
                logger.warning(f"断开连接时出错: {e}")
        
        self._connection = None
        self._state.connected = False

    def _mark_disconnected(self, reason: str) -> None:
        """标记当前连接不可用，让轮询或心跳触发重连。"""
        self._state.connected = False
        self._state.last_error = reason
    
    async def reconnect(self) -> bool:
        """
        重新连接（带指数退避）
        
        Returns:
            重连是否成功
        """
        self._state.reconnect_count += 1
        
        attempt = self._state.reconnect_count
        max_attempts = self._reconnect_config.get("max_attempts", 10)
        
        if attempt > max_attempts:
            logger.error(f"{self.account.name} 达到最大重连次数 ({max_attempts})")
            return False
        
        # 计算退避延迟
        base_delay = self._reconnect_config.get("base_delay_seconds", 1)
        max_delay = self._reconnect_config.get("max_delay_seconds", 300)
        exp_base = self._reconnect_config.get("exponential_base", 2)
        
        delay = min(base_delay * (exp_base ** (attempt - 1)), max_delay)
        
        logger.info(f"{self.account.name} 将在 {delay} 秒后重连 (第 {attempt} 次)")
        
        await asyncio.sleep(delay)
        
        # 先断开旧连接
        await self.disconnect()
        
        # 重新连接
        return await self.connect()
    
    async def select_folder(self, folder: str = "INBOX") -> bool:
        """
        选择邮箱文件夹
        
        Args:
            folder: 文件夹名称
            
        Returns:
            选择是否成功
        """
        if not self.is_connected:
            logger.warning(f"{self.account.name} 未连接，无法选择文件夹")
            return False
        
        try:
            # INBOX 是 IMAP 保留名，不需要加前缀
            if folder.upper() == "INBOX":
                folder_with_prefix = folder
            else:
                folder_with_prefix = f"{self.account.folder_prefix}{folder}" if self.account.folder_prefix else folder
            result = await asyncio.wait_for(
                self._connection.select(self._format_mailbox_name(folder_with_prefix)),
                timeout=self._operation_timeout,
            )
            
            if result.result == "OK":
                logger.debug(f"已选择文件夹: {folder_with_prefix}")
                return True
            else:
                logger.warning(f"选择文件夹失败: {result}")
                return False
                
        except asyncio.TimeoutError:
            self._mark_disconnected(f"SELECT {folder} timeout")
            logger.error(f"{self.account.name} 选择文件夹超时: {folder_with_prefix}")
            return False
        except Exception as e:
            self._mark_disconnected(f"{type(e).__name__}: {e}")
            logger.error(f"{self.account.name} 选择文件夹时出错: {type(e).__name__}: {e!r}")
            return False

    async def folder_exists(self, folder: str) -> bool:
        """
        检查文件夹是否存在，不改变当前选中的邮箱文件夹。

        Args:
            folder: 文件夹名称

        Returns:
            文件夹是否存在
        """
        if not self.is_connected:
            return False

        try:
            result = await self._connection.status(
                self._format_mailbox_name(folder),
                "(MESSAGES)",
            )
            return result.result == "OK"
        except Exception as e:
            logger.debug(f"检查文件夹是否存在时出错: {folder} - {e}")
            return False

    async def get_special_use_folder(self, flag: str) -> Optional[str]:
        """
        获取服务器声明的特殊用途文件夹。

        Gmail 的系统目录会随账号语言本地化，例如垃圾邮件可能不是 [Gmail]/Spam。
        """
        normalized_flag = flag if flag.startswith("\\") else f"\\{flag}"
        normalized_flag = normalized_flag.lower()

        if normalized_flag in self._special_use_cache:
            return self._special_use_cache[normalized_flag]

        if not self.is_connected:
            return None

        try:
            result = await self._connection.list('""', '"*"')
            if result.result != "OK":
                logger.debug(f"获取特殊用途文件夹失败: {result}")
                return None

            for line in result.lines:
                text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
                flags_match = re.match(r"\((?P<flags>[^)]*)\)", text)
                if not flags_match:
                    continue

                flags = {item.lower() for item in flags_match.group("flags").split()}
                if normalized_flag not in flags:
                    continue

                mailbox_match = re.search(r'"(?P<mailbox>(?:[^"\\]|\\.)*)"\s*$', text)
                if not mailbox_match:
                    continue

                mailbox = mailbox_match.group("mailbox").replace(r"\"", '"').replace(r"\\", "\\")
                decoded_mailbox = self._decode_imap_utf7(mailbox)
                self._special_use_cache[normalized_flag] = decoded_mailbox
                return decoded_mailbox

            return None
        except Exception as e:
            logger.debug(f"获取特殊用途文件夹时出错: {e}")
            return None
    
    async def search(
        self,
        criteria: str = "ALL",
        charset: Optional[str] = None,
    ) -> List[str]:
        """
        搜索邮件
        
        Args:
            criteria: 搜索条件
            charset: 字符集
            
        Returns:
            邮件UID列表
        """
        if not self.is_connected:
            logger.warning(f"{self.account.name} 未连接，无法搜索")
            return []
        
        try:
            if charset:
                result = await self._connection.uid_search(criteria, charset=charset)
            else:
                result = await self._connection.uid_search(criteria, charset=None)
            
            if result.result == "OK":
                uids = result.lines[0].decode().split()
                return uids
            else:
                logger.warning(f"搜索失败: {result}")
                return []
                
        except Exception as e:
            logger.error(f"搜索邮件时出错: {e}")
            return []
    
    async def fetch(
        self,
        uids: List[str],
        parts: str = "(RFC822)",
    ) -> Dict[str, bytes]:
        """
        获取邮件内容
        
        Args:
            uids: 邮件UID列表
            parts: 获取的数据部分
            
        Returns:
            UID到邮件数据的映射
        """
        if not uids or not self.is_connected:
            return {}
        
        try:
            result = await self._connection.fetch(",".join(uids), parts)
            
            messages = {}
            if result.result == "OK":
                for line in result.lines:
                    if isinstance(line, bytes):
                        # 解析 FETCH 响应
                        line_str = line.decode()
                        if "FETCH" in line_str:
                            # 提取UID和数据
                            parts_match = line_str.split(b"FETCH")[1] if isinstance(line_str, str) else line
                            # 简化处理：假设第一部分是UID
                            uid = uids[0] if uids else None
                            messages[uid] = line
                            
            return messages
            
        except Exception as e:
            logger.error(f"获取邮件时出错: {e}")
            return {}
    
    async def move_email(self, uid: str, target_folder: str) -> bool:
        """
        移动邮件到目标文件夹
        
        Args:
            uid: 邮件UID
            target_folder: 目标文件夹
            
        Returns:
            移动是否成功
        """
        if not self.is_connected:
            return False
        
        try:
            mailbox = self._format_mailbox_name(target_folder)

            # Gmail 和不支持 MOVE 扩展的服务器使用 COPY + STORE 删除
            if self.account.provider == "gmail" or not self._supports_move():
                result = await self._connection.uid("COPY", uid, mailbox)
                if result.result == "OK":
                    if not await self._delete_copied_source(uid):
                        logger.warning(f"邮件 {uid} 已复制到 {target_folder}，但源邮件删除失败")
                        return False

                    if await self._uid_exists_in_selected_folder(uid):
                        logger.warning(f"邮件 {uid} 已复制到 {target_folder}，但源邮件仍在当前文件夹")
                        return False

                    logger.info(f"邮件 {uid} 已移动到 {target_folder}")
                    return True
            else:
                # 163等使用 MOVE 命令
                result = await self._connection.uid("MOVE", uid, mailbox)
                if result.result == "OK":
                    logger.info(f"邮件 {uid} 已移动到 {target_folder}")
                    return True
            
            logger.warning(f"移动邮件 {uid} 失败: {result}")
            return False
            
        except Exception as e:
            logger.error(f"移动邮件时出错: {e}")
            return False

    async def _delete_copied_source(self, uid: str) -> bool:
        """COPY 成功后删除源邮件。"""
        store_result = await self._connection.uid(
            "STORE",
            uid,
            "+FLAGS.SILENT",
            "(\\Deleted)",
        )
        if store_result.result != "OK":
            logger.warning(f"标记邮件 {uid} 删除失败: {store_result}")
            return False

        if self._supports_uidplus():
            expunge_result = await self._connection.uid("EXPUNGE", uid)
        else:
            expunge_result = await self._connection.expunge()

        if expunge_result.result != "OK":
            logger.warning(f"清除邮件 {uid} 失败: {expunge_result}")
            return False

        return True

    async def _uid_exists_in_selected_folder(self, uid: str) -> bool:
        """检查 UID 是否仍存在于当前选中的文件夹。"""
        result = await self._connection.uid_search(f"UID {uid}", charset=None)
        if result.result != "OK" or not result.lines:
            return False

        return uid in result.lines[0].decode().split()

    def _supports_move(self) -> bool:
        """检查服务器是否支持 IMAP MOVE 扩展。"""
        if self._connection is None:
            return False

        try:
            capabilities = self._connection.protocol.capabilities
        except Exception:
            return False

        return "MOVE" in {cap.upper() for cap in capabilities}

    def _supports_uidplus(self) -> bool:
        """检查服务器是否支持 UIDPLUS。"""
        if self._connection is None:
            return False

        try:
            capabilities = self._connection.protocol.capabilities
        except Exception:
            return False

        return "UIDPLUS" in {cap.upper() for cap in capabilities}
    
    async def create_folder(self, folder: str) -> bool:
        """
        创建邮箱文件夹
        
        Args:
            folder: 文件夹名称
            
        Returns:
            创建是否成功
        """
        if not self.is_connected:
            return False
        
        try:
            result = await self._connection.create(self._format_mailbox_name(folder))
            if result.result == "OK":
                logger.info(f"已创建文件夹: {folder}")
                return True
            else:
                logger.warning(f"创建文件夹失败: {result}")
                return False
                
        except Exception as e:
            logger.error(f"创建文件夹时出错: {e}")
            return False

    def _format_mailbox_name(self, folder: str) -> str:
        """把邮箱文件夹名转换成 IMAP 可解析的 quoted modified UTF-7 参数。"""
        if folder.upper() == "INBOX":
            return "INBOX"

        encoded = self._encode_imap_utf7(folder)
        escaped = encoded.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _encode_imap_utf7(value: str) -> str:
        """
        编码 IMAP mailbox 使用的 modified UTF-7。

        Python 标准库没有直接暴露该编码；IMAP 文件夹名包含中文时必须使用它。
        """
        result = []
        buffer = []

        def flush_buffer() -> None:
            if not buffer:
                return
            raw = "".join(buffer).encode("utf-16-be")
            encoded = base64.b64encode(raw).decode("ascii").rstrip("=")
            result.append("&" + encoded.replace("/", ",") + "-")
            buffer.clear()

        for char in value:
            codepoint = ord(char)
            if 0x20 <= codepoint <= 0x7E:
                flush_buffer()
                if char == "&":
                    result.append("&-")
                else:
                    result.append(char)
            else:
                buffer.append(char)

        flush_buffer()
        return "".join(result)

    @staticmethod
    def _decode_imap_utf7(value: str) -> str:
        """解码 IMAP mailbox 使用的 modified UTF-7。"""
        result = []
        index = 0

        while index < len(value):
            if value[index] != "&":
                result.append(value[index])
                index += 1
                continue

            end = value.find("-", index)
            if end == -1:
                result.append(value[index:])
                break

            token = value[index + 1:end]
            if token == "":
                result.append("&")
            else:
                padding = "=" * ((4 - len(token) % 4) % 4)
                raw = base64.b64decode(token.replace(",", "/") + padding)
                result.append(raw.decode("utf-16-be"))

            index = end + 1

        return "".join(result)
    
    async def idle_listen(self) -> None:
        """
        开始IDLE监听（Gmail专用）
        """
        if self.account.provider != "gmail":
            logger.warning(f"{self.account.name} 不是Gmail，不支持IDLE模式")
            return
        
        self._running = True
        idle_timeout = self.account.idle_timeout
        
        while self._running:
            try:
                if not self.is_connected:
                    logger.info(f"{self.account.name} 重新连接...")
                    if not await self.reconnect():
                        await asyncio.sleep(5)
                        continue
                    
                    await self.select_folder()
                
                logger.debug(f"{self.account.name} 开始IDLE监听 (超时: {idle_timeout}秒)")
                
                # 发送 IDLE 命令
                idle_result = await self._connection.idle()
                if idle_result.result != "OK":
                    logger.warning(f"{self.account.name} 进入IDLE失败: {idle_result}")
                    await asyncio.sleep(5)
                    continue
                
                # 等待IDLE响应或超时
                try:
                    while self._running:
                        # 检查是否有新数据
                        response = await self._connection.wait_server_push(timeout=idle_timeout)
                        
                        if response:
                            # 解析 IDLE 响应
                            for line in getattr(response, "lines", []):
                                if isinstance(line, bytes):
                                    resp_str = line.decode(errors="replace")
                                    if "EXISTS" in resp_str or "RECENT" in resp_str:
                                        logger.info(f"{self.account.name} 检测到新邮件")
                                        if self.on_new_mail:
                                            await self.on_new_mail([])
                
                except asyncio.TimeoutError:
                    # IDLE 超时，发送 NOOP 保持连接
                    logger.debug(f"{self.account.name} IDLE超时，发送 NOOP")
                    self._connection.idle_done()
                    await self._connection.noop()
                    
                    # 检查是否超时需要重连
                    if self._state.last_connected:
                        elapsed = time.time() - self._state.last_connected
                        if elapsed > self.account.reconnect_timeout:
                            logger.warning(f"{self.account.name} 超过重连超时时间")
                            if self.on_idle_timeout:
                                await self.on_idle_timeout()
                            continue
                finally:
                    try:
                        self._connection.idle_done()
                    except Exception:
                        pass
                
            except asyncio.CancelledError:
                logger.info(f"{self.account.name} IDLE监听被取消")
                break
            except Exception as e:
                logger.error(f"{self.account.name} IDLE监听出错: {e}")
                if self._running:
                    await asyncio.sleep(5)
        
        logger.info(f"{self.account.name} IDLE监听结束")
    
    def start_idle(self) -> asyncio.Task:
        """启动IDLE监听任务"""
        if self._idle_task:
            self._idle_task.cancel()
        
        self._idle_task = asyncio.create_task(self.idle_listen())
        return self._idle_task


class ConnectionManager:
    """连接管理器 - 管理多个邮箱账户的连接"""
    
    def __init__(self):
        self._connections: Dict[str, IMAPConnection] = {}
    
    def add_connection(self, account: IMAPAccount) -> IMAPConnection:
        """
        添加邮箱账户连接
        
        Args:
            account: 账户配置
            
        Returns:
            IMAPConnection实例
        """
        conn = IMAPConnection(account)
        self._connections[account.email] = conn
        logger.debug(f"已添加连接: {account.name}")
        return conn
    
    def get_connection(self, email: str) -> Optional[IMAPConnection]:
        """获取指定邮箱的连接"""
        return self._connections.get(email)
    
    def remove_connection(self, email: str) -> None:
        """移除连接"""
        if email in self._connections:
            conn = self._connections[email]
            asyncio.create_task(conn.disconnect())
            del self._connections[email]
    
    async def connect_all(self) -> Dict[str, bool]:
        """
        连接所有账户
        
        Returns:
            账户邮箱到连接结果的映射
        """
        results = {}
        
        async def connect_one(conn: IMAPConnection) -> tuple:
            success = await conn.connect()
            return conn.account.email, success
        
        # 并发连接所有账户
        tasks = [
            connect_one(conn) 
            for conn in self._connections.values()
        ]
        
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results_list:
            if isinstance(result, tuple):
                email, success = result
                results[email] = success
            else:
                logger.error(f"连接异常: {result}")
        
        return results
    
    async def disconnect_all(self) -> None:
        """断开所有连接"""
        tasks = [conn.disconnect() for conn in self._connections.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._connections.clear()
        logger.info("所有连接已断开")
    
    async def reconnect_all(self) -> Dict[str, bool]:
        """
        重新连接所有断开的账户
        
        Returns:
            账户邮箱到重连结果的映射
        """
        results = {}
        
        async def reconnect_one(conn: IMAPConnection) -> tuple:
            success = await conn.reconnect()
            return conn.account.email, success
        
        tasks = [
            reconnect_one(conn)
            for conn in self._connections.values()
            if not conn.is_connected
        ]
        if not tasks:
            return results

        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results_list:
            if isinstance(result, tuple):
                email, success = result
                results[email] = success
        
        return results
    
    @property
    def connections(self) -> Dict[str, IMAPConnection]:
        """获取所有连接"""
        return self._connections.copy()
    
    @property
    def connected_count(self) -> int:
        """已连接数量"""
        return sum(1 for conn in self._connections.values() if conn.is_connected)
