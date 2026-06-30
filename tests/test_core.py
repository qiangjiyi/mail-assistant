"""
测试配置文件
"""

import pytest
import sys
import asyncio
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class TestConfig:
    """配置模块测试"""
    
    def test_config_load(self):
        """测试配置加载"""
        from src.config import get_config
        
        config = get_config()
        assert config is not None
        assert config.app is not None
    
    def test_config_get(self):
        """测试配置获取"""
        from src.config import get_config
        
        config = get_config()
        app_name = config.get("app.name")
        assert app_name is not None
    
    def test_accounts_loaded(self):
        """测试账户配置加载"""
        from src.config import get_config
        
        config = get_config()
        accounts = config.accounts
        assert isinstance(accounts, list)


class TestMailParser:
    """邮件解析器测试"""
    
    def test_decode_header(self):
        """测试头部解码"""
        from src.mail.parser import MailParser
        
        parser = MailParser()
        
        # 测试纯ASCII
        result = parser.decode_header_value("Hello World")
        assert result == "Hello World"
    
    def test_extract_email_address(self):
        """测试邮箱地址提取"""
        from src.mail.parser import MailParser
        
        parser = MailParser()
        
        # 测试标准格式
        name, email = parser.extract_email_address("John Doe <john@example.com>")
        assert name == "John Doe"
        assert email == "john@example.com"
        
        # 测试纯邮箱
        name, email = parser.extract_email_address("john@example.com")
        assert name == ""
        assert email == "john@example.com"
    
    def test_clean_html(self):
        """测试HTML清理"""
        from src.mail.parser import MailParser
        
        parser = MailParser()
        
        html = "<p>Hello<br/>World</p>"
        result = parser.clean_html(html)
        assert "Hello" in result
        assert "World" in result

    @pytest.mark.asyncio
    async def test_skip_non_mail_payload(self):
        """测试 IMAP 元数据不会被解析成空邮件"""
        from src.mail.parser import MailParser

        parser = MailParser()
        email = await parser.parse_raw_email(b"1 FETCH (RFC822 {1234}", "1", "test@example.com")

        assert email is None


class TestMailFetcher:
    """邮件抓取器测试"""

    def test_filter_fetch_metadata(self):
        """测试只解析真正的 RFC822 邮件内容"""
        from src.mail.fetcher import MailFetcher

        assert MailFetcher._looks_like_rfc822_payload(b"1 FETCH (RFC822 {1234}") is False
        assert MailFetcher._looks_like_rfc822_payload(
            b"From: a@example.com\r\nSubject: hello\r\n\r\nbody"
        ) is True
        assert MailFetcher._looks_like_rfc822_payload(
            bytearray(b"From: a@example.com\r\nSubject: hello\r\n\r\nbody")
        ) is True

    def test_next_uid_criteria_skips_last_seen_uid(self):
        """测试增量搜索从 last_uid 的下一封开始"""
        from src.mail.fetcher import MailFetcher

        assert MailFetcher._next_uid_criteria("123") == "124"
        assert MailFetcher._next_uid_criteria("abc") == "abc"

    def test_filter_uids_after_last_skips_stale_uids(self):
        """测试本地过滤会跳过不大于 last_uid 的旧 UID。"""
        from src.mail.connection import IMAPAccount, IMAPConnection
        from src.mail.fetcher import MailFetcher
        from src.mail.parser import MailParser

        account = IMAPAccount(
            name="Gmail",
            provider="gmail",
            email="user@example.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            username="user@example.com",
            password="secret",
        )
        fetcher = MailFetcher(IMAPConnection(account), MailParser())
        fetcher._last_uid = "11312"

        assert fetcher._filter_uids_after_last(["11195", "11312", "11313"]) == ["11313"]

    @pytest.mark.asyncio
    async def test_load_and_remember_last_uid(self):
        """测试抓取器会从状态文件恢复并保存 last_uid"""
        from src.mail.connection import IMAPAccount, IMAPConnection
        from src.mail.fetcher import MailFetcher
        from src.mail.parser import MailParser

        class FakeStateManager:
            def __init__(self):
                self.saved = None

            async def get_last_uid(self, account_email):
                return "42"

            async def set_last_uid(self, account_email, uid):
                self.saved = (account_email, uid)

        account = IMAPAccount(
            name="Gmail",
            provider="gmail",
            email="user@example.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            username="user@example.com",
            password="secret",
        )
        fetcher = MailFetcher(
            IMAPConnection(account),
            MailParser(),
            state_manager=FakeStateManager(),
        )

        await fetcher._load_last_uid()
        assert fetcher._last_uid == "42"

        await fetcher._remember_last_uid("43")
        assert fetcher._last_uid == "43"
        assert fetcher._state_manager.saved == ("user@example.com", "43")

        await fetcher._remember_last_uid("41")
        assert fetcher._last_uid == "43"
        assert fetcher._state_manager.saved == ("user@example.com", "43")

    @pytest.mark.asyncio
    async def test_idle_restart_catches_up_before_listening(self):
        """测试 Gmail IDLE 重启后会先补抓漏掉的新 UID。"""
        from src.mail.connection import IMAPAccount
        from src.mail.fetcher import MailFetcher
        from src.mail.parser import MailParser

        account = IMAPAccount(
            name="Gmail",
            provider="gmail",
            email="user@example.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            username="user@example.com",
            password="secret",
        )

        class FakeConnection:
            def __init__(self):
                self.account = account
                self.connected = False
                self.events = []
                self.on_new_mail = None
                self.on_idle_timeout = None

            @property
            def is_connected(self):
                return self.connected

            async def reconnect(self):
                self.events.append("reconnect")
                self.connected = True
                return True

            async def select_folder(self):
                self.events.append("select")
                return True

            async def search(self, criteria):
                self.events.append(("search", criteria))
                return []

            async def idle_listen(self):
                self.events.append("idle")

        class FakeStateManager:
            async def get_last_uid(self, account_email):
                return "11310"

            async def set_last_uid(self, account_email, uid):
                pass

        connection = FakeConnection()
        fetcher = MailFetcher(
            connection,
            MailParser(),
            state_manager=FakeStateManager(),
        )

        task = await fetcher.start_idle(skip_existing=True, catch_up=True)
        await task

        assert connection.events == [
            "reconnect",
            "select",
            ("search", "UID 11311:*"),
            "idle",
        ]

    @pytest.mark.asyncio
    async def test_gmail_start_with_last_uid_uses_incremental_fetch(self):
        """测试 Gmail 正常启动时已有 last_uid 就不再扫描存量邮件。"""
        from src.mail.connection import IMAPAccount
        from src.mail.fetcher import MailFetcher
        from src.mail.parser import MailParser

        account = IMAPAccount(
            name="Gmail",
            provider="gmail",
            email="user@example.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            username="user@example.com",
            password="secret",
        )

        class FakeConnection:
            def __init__(self):
                self.account = account
                self.connected = True
                self.events = []
                self.on_new_mail = None
                self.on_idle_timeout = None

            @property
            def is_connected(self):
                return self.connected

            async def select_folder(self):
                self.events.append("select")
                return True

            async def search(self, criteria):
                self.events.append(("search", criteria))
                return []

            async def idle_listen(self):
                self.events.append("idle")

        class FakeStateManager:
            async def get_last_uid(self, account_email):
                return "11312"

            async def set_last_uid(self, account_email, uid):
                pass

        connection = FakeConnection()
        fetcher = MailFetcher(
            connection,
            MailParser(),
            state_manager=FakeStateManager(),
        )

        task = await fetcher.start_idle()
        await task

        assert connection.events == [
            "select",
            ("search", "UID 11313:*"),
            "idle",
        ]


class TestIMAPConnection:
    """IMAP连接测试"""

    def test_requires_client_id_for_netease_account(self):
        """测试网易系账号需要发送 IMAP ID"""
        from src.mail.connection import IMAPAccount, IMAPConnection

        account = IMAPAccount(
            name="网易邮箱",
            provider="163",
            email="user@example.com",
            imap_host="imap.163.com",
            imap_port=993,
            username="user@example.com",
            password="secret",
        )
        connection = IMAPConnection(account)

        assert connection._requires_client_id() is True

    @pytest.mark.asyncio
    async def test_send_client_id_for_netease_account(self):
        """测试网易系账号登录后会发送 IMAP ID"""
        from src.mail.connection import IMAPAccount, IMAPConnection

        class FakeResponse:
            result = "OK"

        class FakeProtocol:
            def __init__(self):
                self.command = None
                self.loop = None

            def new_tag(self):
                return "A001"

            async def execute(self, command):
                self.command = command
                return FakeResponse()

        class FakeIMAP:
            def __init__(self):
                self.protocol = FakeProtocol()
                self.timeout = 30

        account = IMAPAccount(
            name="网易邮箱",
            provider="163",
            email="user@example.com",
            imap_host="imap.163.com",
            imap_port=993,
            username="user@example.com",
            password="secret",
        )
        connection = IMAPConnection(account)
        fake_imap = FakeIMAP()
        connection._connection = fake_imap

        await connection._send_client_id_if_required()

        assert fake_imap.protocol.command.name == "ID"
        assert '"name" "mail_assistant"' in fake_imap.protocol.command.args[0]

    def test_format_chinese_mailbox_name(self):
        """测试中文文件夹名会转成 IMAP modified UTF-7 并加引号"""
        from src.mail.connection import IMAPAccount, IMAPConnection

        account = IMAPAccount(
            name="Gmail",
            provider="gmail",
            email="user@example.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            username="user@example.com",
            password="secret",
        )
        connection = IMAPConnection(account)

        assert connection._format_mailbox_name("技术文档") == '"&YoBnL2WHaGM-"'
        assert connection._format_mailbox_name("[Gmail]/Spam") == '"[Gmail]/Spam"'
        assert connection._format_mailbox_name("INBOX") == "INBOX"
        assert connection._decode_imap_utf7("[Gmail]/&V4NXPpCuTvY-") == "[Gmail]/垃圾邮件"

    @pytest.mark.asyncio
    async def test_move_email_falls_back_to_copy_when_move_unsupported(self):
        """测试不支持 MOVE 的服务器会退回 COPY + 删除"""
        from src.mail.connection import IMAPAccount, IMAPConnection

        class FakeResponse:
            def __init__(self, lines=None):
                self.result = "OK"
                self.lines = lines or []

        class FakeProtocol:
            state = "SELECTED"
            capabilities = {"IMAP4rev1", "UIDPLUS"}

        class FakeIMAP:
            def __init__(self):
                self.protocol = FakeProtocol()
                self.copied_to = None
                self.stored = None
                self.uid_expunged = None
                self.uid_searches = []

            async def uid(self, command, *args):
                if command == "COPY":
                    self.copied_to = args
                elif command == "STORE":
                    self.stored = args
                elif command == "EXPUNGE":
                    self.uid_expunged = args
                return FakeResponse()

            async def uid_search(self, criteria, charset=None):
                self.uid_searches.append((criteria, charset))
                return FakeResponse([b""])

        account = IMAPAccount(
            name="网易邮箱",
            provider="163",
            email="user@example.com",
            imap_host="imap.163.com",
            imap_port=993,
            username="user@example.com",
            password="secret",
        )
        connection = IMAPConnection(account)
        fake_imap = FakeIMAP()
        connection._connection = fake_imap
        connection._state.connected = True

        assert await connection.move_email("272", "通知提醒") is True
        assert fake_imap.copied_to == ("272", '"&kBp35WPQkZI-"')
        assert fake_imap.stored == ("272", "+FLAGS.SILENT", "(\\Deleted)")
        assert fake_imap.uid_expunged == ("272",)
        assert fake_imap.uid_searches == [("UID 272", None)]

    @pytest.mark.asyncio
    async def test_get_special_use_folder_decodes_gmail_junk(self):
        """测试能解析 Gmail 本地化的垃圾邮件目录"""
        from src.mail.connection import IMAPAccount, IMAPConnection

        class FakeResponse:
            result = "OK"
            lines = [
                b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
                b'(\\HasNoChildren \\Junk) "/" "[Gmail]/&V4NXPpCuTvY-"',
            ]

        class FakeProtocol:
            state = "SELECTED"

        class FakeIMAP:
            protocol = FakeProtocol()

            async def list(self, reference_name, mailbox_pattern):
                return FakeResponse()

        account = IMAPAccount(
            name="Gmail",
            provider="gmail",
            email="user@example.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            username="user@example.com",
            password="secret",
        )
        connection = IMAPConnection(account)
        connection._connection = FakeIMAP()
        connection._state.connected = True

        assert await connection.get_special_use_folder("\\Junk") == "[Gmail]/垃圾邮件"

    @pytest.mark.asyncio
    async def test_idle_new_mail_callback_runs_after_idle_done(self):
        """测试 Gmail 新邮件回调会在退出 IDLE 后执行"""
        from src.mail.connection import IMAPAccount, IMAPConnection

        class FakeResponse:
            result = "OK"

        class FakeProtocol:
            state = "SELECTED"

        class FakeIMAP:
            def __init__(self):
                self.protocol = FakeProtocol()
                self.idle_done_called = False
                self.noop_called = False

            async def select(self, folder):
                return FakeResponse()

            async def idle_start(self, timeout):
                future = asyncio.get_running_loop().create_future()
                future.set_result(FakeResponse())
                return future

            def has_pending_idle(self):
                return True

            async def wait_server_push(self, timeout):
                return [b"* 2 EXISTS"]

            def idle_done(self):
                self.idle_done_called = True

            async def noop(self):
                self.noop_called = True

        account = IMAPAccount(
            name="Gmail",
            provider="gmail",
            email="user@example.com",
            imap_host="imap.gmail.com",
            imap_port=993,
            username="user@example.com",
            password="secret",
        )
        connection = IMAPConnection(account)
        fake_imap = FakeIMAP()
        callback_seen_idle_done = []

        async def on_new_mail(_uids):
            callback_seen_idle_done.append(fake_imap.idle_done_called)
            connection._running = False

        connection._connection = fake_imap
        connection._state.connected = True
        connection.on_new_mail = on_new_mail

        await connection.idle_listen()

        assert callback_seen_idle_done == [True]
        assert fake_imap.noop_called is True


class TestRuleEngine:
    """规则引擎测试"""
    
    def test_rule_matching(self):
        """测试规则匹配"""
        from src.classifier.rules import RuleEngine, ClassificationRule
        from src.mail.parser import EmailData
        from datetime import datetime
        
        # 创建测试规则
        rule = ClassificationRule(
            name="测试规则",
            priority=10,
            category="工作沟通",
            sender_pattern=r"hr@company\.com",
        )
        
        # 创建测试邮件
        email = EmailData(
            message_id="test-123",
            uid="1",
            account_email="test@example.com",
            sender="HR Department",
            sender_email="hr@company.com",
            recipients=["user@example.com"],
            subject="面试邀请",
            date=datetime.now(),
            date_str="",
            body_plain="您好，面试邀请...",
            body_html="",
            body_preview="您好，面试邀请...",
            has_attachments=False,
        )
        
        engine = RuleEngine()
        engine.add_rule(rule)
        matches = engine.match(email)

        # 验证匹配
        assert len(matches) > 0
        matched_rule, score = matches[0]
        assert score > 0


class TestFeishuNotifier:
    """飞书通知器测试"""

    def test_signature_is_base64(self):
        """测试飞书签名使用 base64 格式"""
        from src.notifier.feishu import FeishuNotifier

        notifier = FeishuNotifier()
        signature = notifier._generate_signature("1234567890", "secret")

        assert isinstance(signature, str)
        assert len(signature) > 0
        assert not all(char in "0123456789abcdef" for char in signature.lower())

    def test_add_signature_to_message_body(self):
        """测试飞书签名字段会放入消息体"""
        from src.notifier.feishu import FeishuNotifier

        notifier = FeishuNotifier()
        notifier._hmac_enabled = True
        notifier._secret = "secret"

        message = {"msg_type": "text", "content": {"text": "hello"}}
        signed = notifier._add_signature(message)

        assert "timestamp" in signed
        assert "sign" in signed
        assert "timestamp" not in message
        assert "sign" not in message

    def test_http_200_business_error_is_not_success(self):
        """测试 HTTP 200 但业务失败不能算发送成功"""
        from src.notifier.feishu import FeishuNotifier

        assert FeishuNotifier._is_success_response({"code": 19021}) is False
        assert FeishuNotifier._is_success_response({"StatusCode": 0}) is True

    def test_extract_verification_code(self):
        """测试验证码提取"""
        from src.notifier.feishu import FeishuNotifier

        assert FeishuNotifier._extract_verification_code_from_text("您的验证码是 839201，5分钟内有效") == "839201"
        assert FeishuNotifier._extract_verification_code_from_text("Your verification code: A19Z8") == "A19Z8"
        assert FeishuNotifier._extract_verification_code_from_text("普通通知，没有验证码") is None

    def test_extract_verification_code_bitget_template(self):
        """测试 Bitget 风格的“关键词在描述中 + 验证码独立成行”模板"""
        from src.notifier.feishu import FeishuNotifier

        text = (
            "授权新设备\n"
            "Hi,user****@example.com\n\n"
            "您的账户正在新设备登录，为了您的账户安全，本次登录需要授权验证。"
            "如信息无误，您可以输入验证码授权新设备。\n\n"
            "858545 有效期10分钟\n\n"
            "登录地点: Japan-Tokyo-Tokyo\n"
            "IP地址: 203.0.113.42\n"
            "设备: iPhone 14 Pro Max"
        )

        assert FeishuNotifier._extract_verification_code_from_text(text) == "858545"

    def test_extract_verification_code_skips_ip_and_short_numbers(self):
        """测试段落回退时不会把 IP / 时长 / 设备编号误当成验证码"""
        from src.notifier.feishu import FeishuNotifier

        # 仅有 IP、2 位时长、2 位设备号，没有真正的验证码
        ip_only = (
            "您的账户正在新设备登录，请输入验证码授权新设备。\n"
            "登录地点: Japan-Tokyo-Tokyo\n"
            "IP地址: 203.0.113.42\n"
            "设备: iPhone 14 Pro Max\n"
            "有效期10分钟"
        )
        assert FeishuNotifier._extract_verification_code_from_text(ip_only) is None

        # IP 段后面才是真正的验证码
        ip_then_code = ip_only + "\n您的验证码: 839201"
        assert FeishuNotifier._extract_verification_code_from_text(ip_then_code) == "839201"

    def test_extract_verification_code_long_gap_between_keyword_and_code(self):
        """测试关键词和验证码之间被大段描述隔开时仍能匹配"""
        from src.notifier.feishu import FeishuNotifier

        text = (
            "为了您的账户安全，本次登录需要授权验证，"
            "本次操作需要您输入下方验证码完成身份核验后方可继续使用相关服务，"
            "请妥善保管不要向他人泄露您的本次验证码。458213 是 6 位数字验证码"
        )

        assert FeishuNotifier._extract_verification_code_from_text(text) == "458213"

    def test_build_card_message_uses_compact_layout_with_code(self):
        """测试新版飞书卡片结构会突出摘要和验证码"""
        from src.notifier.feishu import FeishuNotifier
        from src.mail.parser import EmailData
        from datetime import datetime

        notifier = FeishuNotifier()
        email = EmailData(
            message_id="test-code",
            uid="3",
            account_email="user@example.com",
            sender="Apple",
            sender_email="noreply@apple.com",
            recipients=["user@example.com"],
            subject="Apple Account 验证码",
            date=datetime(2026, 6, 5, 7, 0, 0),
            date_str="",
            body_plain="您的验证码是 839201，请勿泄露。",
            body_html="",
            body_preview="您的验证码是 839201，请勿泄露。",
            has_attachments=False,
            ai_category="通知提醒",
            ai_confidence=0.96,
            ai_summary="Apple 账号登录验证。",
            archived_folder="通知提醒",
        )

        message = notifier._build_card_message(email)
        card = message["card"]
        contents = str(card)

        assert card["header"]["title"]["content"].startswith("[通知提醒]")
        assert card["header"]["template"] == "orange"
        assert "Apple 账号登录验证" in contents
        assert "839201" in contents
        assert "**置信度**: 96% · 高" in contents
        assert "**归档**" not in contents
        assert "账号: user@example.com · 收到时间: 2026-06-05 15:00" in contents

    def test_format_received_time_uses_beijing_time(self):
        """测试收件时间会按北京时间展示"""
        from datetime import datetime, timezone
        from src.notifier.feishu import FeishuNotifier

        assert FeishuNotifier._format_received_time(
            datetime(2026, 6, 5, 7, 0, 0, tzinfo=timezone.utc)
        ) == "2026-06-05 15:00"
        assert FeishuNotifier._format_received_time(
            datetime(2026, 6, 5, 7, 0, 0)
        ) == "2026-06-05 15:00"
    
    @pytest.mark.asyncio
    async def test_block_keywords(self):
        """测试关键词屏蔽"""
        from src.notifier.feishu import FeishuNotifier
        from src.mail.parser import EmailData
        from datetime import datetime
        
        notifier = FeishuNotifier()
        notifier._keyword_filter_enabled = True
        notifier._blocked_keywords = ["钓鱼", "木马"]
        
        # 创建包含屏蔽词的邮件
        email = EmailData(
            message_id="test-456",
            uid="2",
            account_email="test@example.com",
            sender="Unknown",
            sender_email="unknown@test.com",
            recipients=["user@example.com"],
            subject="钓鱼邮件测试",
            date=datetime.now(),
            date_str="",
            body_plain="这是一封钓鱼邮件",
            body_html="",
            body_preview="这是一封钓鱼邮件",
            has_attachments=False,
        )
        
        should_block = notifier._should_block(email)
        assert should_block is True


class TestDatabase:
    """数据库测试"""
    
    @pytest.mark.asyncio
    async def test_database_init(self, tmp_path):
        """测试数据库初始化"""
        from src.storage.database import Database
        
        db_path = str(tmp_path / "test.db")
        db = await Database.create(db_path)
        
        assert db is not None
        
        # 验证表已创建
        cursor = await db._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = await cursor.fetchall()
        table_names = [t[0] for t in tables]
        
        assert "emails" in table_names
        assert "logs" in table_names
        assert "accounts" not in table_names
        assert "rules" not in table_names
        
        await db.close()

    @pytest.mark.asyncio
    async def test_database_cleanup_old_data(self, tmp_path):
        """测试数据库历史数据清理"""
        from src.storage.database import Database

        db_path = str(tmp_path / "cleanup.db")
        db = Database(db_path)
        await db.initialize()

        await db._connection.execute(
            """
            INSERT INTO emails (
                message_id, uid, account_email, subject,
                archived_folder, processed, updated_at
            ) VALUES
                ('old-processed', '1', 'test@example.com', 'old', '通知提醒', 1, datetime('now', '-10 days')),
                ('old-unprocessed', '2', 'test@example.com', 'pending', NULL, 0, datetime('now', '-10 days')),
                ('recent-processed', '3', 'test@example.com', 'recent', '通知提醒', 1, datetime('now'))
            """
        )
        await db._connection.execute(
            """
            INSERT INTO logs (level, message, created_at) VALUES
                ('INFO', 'old log', datetime('now', '-10 days')),
                ('INFO', 'recent log', datetime('now'))
            """
        )
        await db._connection.commit()

        result = await db.cleanup_old_data(
            emails_retention_days=7,
            logs_retention_days=7,
        )

        assert result == {"emails_deleted": 1, "logs_deleted": 1}

        cursor = await db._connection.execute("SELECT message_id FROM emails ORDER BY message_id")
        rows = await cursor.fetchall()
        assert [row["message_id"] for row in rows] == ["old-unprocessed", "recent-processed"]

        cursor = await db._connection.execute("SELECT message FROM logs")
        rows = await cursor.fetchall()
        assert [row["message"] for row in rows] == ["recent log"]

        await db.close()


class TestMailAssistantService:
    """主服务编排测试"""

    @pytest.mark.asyncio
    async def test_recover_stale_unprocessed_emails_fetches_uids_before_last_uid(self):
        """测试 UID 已落后于 last_uid 的未处理邮件会被回收处理。"""
        from src.main import MailAssistantService
        from src.mail.parser import EmailData

        class FakeStateManager:
            def __init__(self):
                self.errors = 0

            async def get_last_uid(self, account_email):
                return "100"

            async def increment_errors(self):
                self.errors += 1

        class FakeDatabase:
            async def get_unprocessed_emails(self, account_email=None, limit=100):
                return [
                    {"uid": "98", "subject": "stale"},
                    {"uid": "101", "subject": "newer"},
                ]

        class FakeFetcher:
            def __init__(self):
                self.fetched_uids = None

            async def _fetch_and_parse_emails(self, uids):
                self.fetched_uids = uids
                return [
                    EmailData(
                        message_id="m-98",
                        uid="98",
                        account_email="user@example.com",
                        sender="sender",
                        sender_email="sender@example.com",
                        recipients=["user@example.com"],
                        subject="stale",
                        date=None,
                        date_str="",
                        body_plain="body",
                        body_html="",
                        body_preview="body",
                        has_attachments=False,
                    )
                ]

        service = MailAssistantService()
        fetcher = FakeFetcher()
        processed = []

        async def fake_process_emails(emails, send_individual_notifications=True):
            processed.extend(emails)
            assert send_individual_notifications is False
            return emails

        service._database = FakeDatabase()
        service._state_manager = FakeStateManager()
        service._fetchers = {"user@example.com": fetcher}
        service._process_emails = fake_process_emails

        await service._recover_stale_unprocessed_emails()

        assert fetcher.fetched_uids == ["98"]
        assert [email.uid for email in processed] == ["98"]


class TestStateManager:
    """状态管理器测试"""
    
    @pytest.mark.asyncio
    async def test_state_operations(self, tmp_path):
        """测试状态操作"""
        from src.storage.state import StateManager
        
        state_file = str(tmp_path / "state.json")
        manager = StateManager(state_file)
        
        # 测试设置和获取
        await manager.set("test_key", "test_value")
        value = await manager.get("test_key")
        assert value == "test_value"
        
        # 测试账户UID
        await manager.set_last_uid("test@example.com", "12345")
        uid = await manager.get_last_uid("test@example.com")
        assert uid == "12345"

        await manager.set_last_uid("test@example.com", "12344")
        uid = await manager.get_last_uid("test@example.com")
        assert uid == "12345"
        
        # 测试服务状态
        await manager.set_service_running(True)
        state = await manager.get_service_state()
        assert state["running"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
