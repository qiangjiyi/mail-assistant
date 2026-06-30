"""
IMAP连接管理模块
负责邮箱连接、重连和连接池管理
"""

import asyncio
import base64
import re
import socket
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
    last_idle_heartbeat: float = 0.0
    restart_in_progress: bool = False


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
        self._connect_timeout = config.get("imap.connect_timeout_seconds", 60)
    
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

            # 设置 TCP keepalive：NAT/防火墙会杀掉空闲 TCP 连接，
            # keepalive 每 60s 发探测包，3 次无响应判定断开。
            try:
                transport = self._connection.protocol.transport
                sock = transport.get_extra_info('socket')
                if sock is not None:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    # macOS TCP_KEEPALIVE = 0x10
                    if hasattr(socket, 'TCP_KEEPALIVE'):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 60)
                    elif hasattr(socket, 'TCP_KEEPIDLE'):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
                    if hasattr(socket, 'TCP_KEEPINTVL'):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                    if hasattr(socket, 'TCP_KEEPCNT'):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                    logger.debug(f"{self.account.name} TCP keepalive 已启用 (idle=60s)")
            except Exception as e:
                logger.debug(f"{self.account.name} 设置 TCP keepalive 失败（非致命）: {e}")

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
        """断开连接（不改变 idle_listen 的 _running 标志，由 idle_listen 自行管理生命周期）"""
        
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
        
        # 重新连接。休眠/代理切换后 TLS hello 或 login 偶尔会永久卡住；
        # 重连路径必须有硬超时，否则 Gmail IDLE 看门狗会被一起拖死。
        try:
            return await asyncio.wait_for(self.connect(), timeout=self._connect_timeout)
        except asyncio.TimeoutError:
            self._mark_disconnected("connect timeout during reconnect")
            self._kill_transport()
            self._connection = None
            logger.error(f"{self.account.name} 重连超时 ({self._connect_timeout}s)")
            return False
    
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
            folder_with_prefix = self._apply_prefix(folder)
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
            result = await asyncio.wait_for(
                self._connection.status(
                    self._format_mailbox_name(self._apply_prefix(folder)),
                    "(MESSAGES)",
                ),
                timeout=self._operation_timeout,
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
            mailbox = self._format_mailbox_name(self._apply_prefix(target_folder))

            # Gmail 和不支持 MOVE 扩展的服务器使用 COPY + STORE 删除
            if self.account.provider == "gmail" or not self._supports_move():
                result = await asyncio.wait_for(
                    self._connection.uid("COPY", uid, mailbox),
                    timeout=self._operation_timeout,
                )
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
                result = await asyncio.wait_for(
                    self._connection.uid("MOVE", uid, mailbox),
                    timeout=self._operation_timeout,
                )
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
        try:
            store_result = await asyncio.wait_for(
                self._connection.uid(
                    "STORE",
                    uid,
                    "+FLAGS.SILENT",
                    "(\\Deleted)",
                ),
                timeout=self._operation_timeout,
            )
        except Exception as e:
            logger.warning(f"标记邮件 {uid} 删除超时或失败: {e}")
            return False
        if store_result.result != "OK":
            logger.warning(f"标记邮件 {uid} 删除失败: {store_result}")
            return False

        try:
            if self._supports_uidplus():
                expunge_result = await asyncio.wait_for(
                    self._connection.uid("EXPUNGE", uid),
                    timeout=self._operation_timeout,
                )
            else:
                expunge_result = await asyncio.wait_for(
                    self._connection.expunge(),
                    timeout=self._operation_timeout,
                )
        except Exception as e:
            logger.warning(f"清除邮件 {uid} 超时或失败: {e}")
            return False

        if expunge_result.result != "OK":
            logger.warning(f"清除邮件 {uid} 失败: {expunge_result}")
            return False

        return True

    async def _uid_exists_in_selected_folder(self, uid: str) -> bool:
        """检查 UID 是否仍存在于当前选中的文件夹。"""
        try:
            result = await asyncio.wait_for(
                self._connection.uid_search(f"UID {uid}", charset=None),
                timeout=self._operation_timeout,
            )
        except Exception as e:
            logger.debug(f"检查邮件 {uid} 残留状态超时: {e}")
            return False
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
            result = await asyncio.wait_for(
                self._connection.create(self._format_mailbox_name(self._apply_prefix(folder))),
                timeout=self._operation_timeout,
            )
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

    def _apply_prefix(self, folder: str) -> str:
        """对非 INBOX 文件夹拼上 account.folder_prefix。

        select_folder/folder_exists/create_folder/move_email 共用，避免出现"select 走前缀、
        其余命令不走前缀"的不一致——之前 163 配 folder_prefix='私人/' 时这个 bug 会让归档
        命中错误的目录。
        """
        if folder.upper() == "INBOX" or not self.account.folder_prefix:
            return folder
        return f"{self.account.folder_prefix}{folder}"

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

        使用 aioimaplib 的 idle_start() + wait_server_push() API：
        - idle_start(timeout)：发送 IDLE 命令，收到 "+ idling" 后立即返回，
          同时设置 call_later 自动停止（超时后自动调 stop_wait_server_push）。
        - wait_server_push(timeout)：阻塞等待服务器推送（新邮件通知等）。
        - idle_done()：发送 DONE 结束 IDLE 模式。

        切勿使用 idle()——它会阻塞直到收到 tagged response（即 idle_done 被调用），
        在没人调 idle_done 的情况下必然超时。
        """
        if self.account.provider != "gmail":
            logger.warning(f"{self.account.name} 不是Gmail，不支持IDLE模式")
            return

        self._running = True
        idle_timeout = self.account.idle_timeout
        self._state.last_idle_heartbeat = 0.0
        # IDLE 启动退避：连续失败指数退避，成功进入 IDLE 后清零。
        _MIN_REENTRY_DELAY = 5
        _MAX_REENTRY_DELAY = 60
        _reentry_delay = _MIN_REENTRY_DELAY
        _last_idle_attempt = 0.0

        while self._running:
            idle_future = None
            has_new_mail = False
            try:
                if not self.is_connected:
                    logger.info(f"{self.account.name} 重新连接...")
                    if not await self.reconnect():
                        max_attempts = self._reconnect_config.get("max_attempts", 10)
                        if self._state.reconnect_count >= max_attempts:
                            logger.error(f"{self.account.name} 重连耗尽，退出IDLE循环")
                            break
                        await asyncio.sleep(_reentry_delay)
                        continue

                # 每轮都重新 SELECT INBOX：存量邮件处理阶段的 COPY+EXPUNGE
                # 可能改变 IMAP 会话状态，直接用脏状态发 IDLE 会失败或挂死。
                if not await self.select_folder():
                    await asyncio.sleep(_reentry_delay)
                    continue

                # 退避：距离上次 idle_start 尝试不足 _reentry_delay 就让出事件循环
                now = time.time()
                wait = _last_idle_attempt + _reentry_delay - now
                if wait > 0:
                    await asyncio.sleep(wait)
                _last_idle_attempt = time.time()

                logger.debug(f"{self.account.name} 开始IDLE监听 (超时: {idle_timeout}秒)")

                # idle_start：发送 IDLE → 等服务器回复 "+ idling" → 立即返回 Future
                # timeout 参数控制 call_later 自动停止时间，防止 IDLE 永远挂着
                try:
                    idle_future = await self._connection.idle_start(timeout=idle_timeout)
                except Exception as e:
                    logger.warning(f"{self.account.name} idle_start() 失败: {e}")
                    self._mark_disconnected("idle_start failed")
                    _reentry_delay = min(_reentry_delay * 2, _MAX_REENTRY_DELAY)
                    continue

                if not self._connection.has_pending_idle():
                    logger.warning(f"{self.account.name} idle_start 未建立 IDLE 状态")
                    _reentry_delay = min(_reentry_delay * 2, _MAX_REENTRY_DELAY)
                    continue

                # 成功进入 IDLE：清零退避，记录心跳
                _reentry_delay = _MIN_REENTRY_DELAY
                self._state.last_idle_heartbeat = time.time()
                logger.info(f"{self.account.name} 已进入IDLE模式")

                # 等待服务器推送（新邮件通知 / IDLE 超时自动停止 / 外部取消）
                # 每个周期只做一次 wait_server_push，由 idle_start 的 call_later 兜底
                # wait_server_push 的 timeout 设为 idle_timeout + 30，比 idle_start 的
                # call_later(idle_timeout) 多 30 秒，确保 call_later 先触发
                # stop_wait_server_push → wait_server_push 正常返回，避免竞态残留
                # 外层 asyncio.wait_for(idle_timeout + 60) 做硬超时保险
                try:
                    try:
                        response = await asyncio.wait_for(
                            self._connection.wait_server_push(timeout=idle_timeout + 30),
                            timeout=idle_timeout + 60,
                        )
                    except asyncio.TimeoutError:
                        # wait_server_push 超时：连接可能已死，退出本轮 IDLE
                        logger.debug(f"{self.account.name} wait_server_push 超时，退出IDLE")
                        response = None

                    if response:
                        # wait_server_push 返回的是 idle_queue 中的原始数据：
                        # - 服务器推送：list[bytes]，如 [b'* 5 EXISTS']
                        # - 超时信号：[b'stop_wait_server_push']
                        # 不是 Response namedtuple，直接遍历即可
                        self._state.last_idle_heartbeat = time.time()
                        lines = response if isinstance(response, list) else getattr(response, "lines", [])
                        is_stop_signal = False
                        for line in lines:
                            if isinstance(line, bytes):
                                resp_str = line.decode(errors="replace")
                                if "stop_wait_server_push" in resp_str:
                                    is_stop_signal = True
                                    continue
                                if "EXISTS" in resp_str or "RECENT" in resp_str:
                                    logger.info(f"{self.account.name} 检测到新邮件")
                                    has_new_mail = True
                        if is_stop_signal:
                            logger.debug(f"{self.account.name} IDLE 周期结束，准备轮转")
                    else:
                        # 空响应（不应出现）
                        logger.debug(f"{self.account.name} IDLE 空响应，准备轮转")
                    # idle_start 的 call_later 超时后会自动 stop_wait_server_push，
                    # wait_server_push 返回 STOP_WAIT_SERVER_PUSH 标记，本轮 IDLE 结束
                finally:
                    # 无论什么路径退出，都确保发送 DONE 结束 IDLE
                    try:
                        self._connection.idle_done()
                    except Exception:
                        pass
                    # 等待 idle Future 完成（获取 tagged response）
                    if idle_future is not None and not idle_future.done():
                        try:
                            await asyncio.wait_for(idle_future, timeout=5)
                        except (asyncio.TimeoutError, Exception):
                            pass

                # IMAP IDLE 状态下不能发送 SELECT/FETCH。先 DONE 退出 IDLE，
                # 再把新邮件信号交给抓取器，避免 Gmail 连接在唤醒后被并发命令卡死。
                if has_new_mail and self.on_new_mail:
                    await self.on_new_mail([])

                # IDLE 正常轮转后发 NOOP 保活
                logger.debug(f"{self.account.name} IDLE 轮转结束，发 NOOP 保活")
                try:
                    await self._connection.noop()
                except Exception:
                    pass

                # 检查是否需要重连
                if self._state.last_connected:
                    elapsed = time.time() - self._state.last_connected
                    if elapsed > self.account.reconnect_timeout:
                        logger.warning(f"{self.account.name} 超过重连超时时间")
                        if self.on_idle_timeout:
                            await self.on_idle_timeout()

            except asyncio.CancelledError:
                logger.info(f"{self.account.name} IDLE监听被取消")
                # 确保退出 IDLE
                try:
                    self._connection.idle_done()
                except Exception:
                    pass
                break
            except Exception as e:
                logger.error(f"{self.account.name} IDLE监听出错: {e}")
                if self._running:
                    _reentry_delay = min(_reentry_delay * 2, _MAX_REENTRY_DELAY)
                    await asyncio.sleep(_reentry_delay)

        logger.info(f"{self.account.name} IDLE监听结束")

    def mark_idle_unhealthy(self) -> None:
        """看门狗检测到 IDLE 僵死时打上标记，由 force_restart_idle 清理。"""
        self._state.restart_in_progress = True
        self._state.last_idle_heartbeat = 0.0

    def clear_idle_restart_flag(self) -> None:
        """force_restart_idle 完成（成功或失败）后清掉标记，避免 watchdog 反复触发。"""
        self._state.restart_in_progress = False

    def _kill_transport(self) -> None:
        """直接关闭底层 SSL transport，打断阻塞的 socket 读写。

        当 IDLE task 卡在 aioimaplib 的 socket read 上时，优雅的
        logout()/cancel() 都无法让它退出；只有关掉 transport 才能让
        底层 StreamReader 收到 EOF，从而让 await 返回。
        """
        if self._connection is not None:
            try:
                transport = self._connection.protocol.transport
                transport.close()
                logger.debug(f"{self.account.name} 已强制关闭底层 transport")
            except Exception:
                pass

    async def force_restart_idle(self) -> None:
        """
        看门狗调用：终止当前 IDLE 协程、断连、清状态，让外层用新 task 接管。

        必须在 idle_listen 还没自然退出的情况下被调用。
        """
        self._state.restart_in_progress = True
        try:
            # 让 idle_listen 的外层 while 退出
            self._running = False

            # 直接关闭底层 transport（比 logout 快且可靠）
            # logout() 在连接僵死时会卡住，transport.close() 直接中断 socket
            self._kill_transport()

            self._state.connected = False
            self._connection = None
            self._state.last_connected = None
        finally:
            self.clear_idle_restart_flag()

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
            try:
                success = await asyncio.wait_for(conn.connect(), timeout=conn._connect_timeout)
            except asyncio.TimeoutError:
                logger.error(f"连接 {conn.account.name} 超时 ({conn._connect_timeout}s)")
                success = False
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
            if not conn.is_connected and conn.account.provider != "gmail"
        ]
        # 跳过 Gmail：其重连由 IMAPConnection.idle_listen 自治（看门狗触发的
        # self.reconnect() 路径），manager 层不应横插一脚。
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
