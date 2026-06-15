# 邮箱整理助手

AI智能分类归档邮件服务，支持多邮箱账户管理。

## 功能特性

- **多邮箱支持**: 可配置多个 163 邮箱 (IMAP 轮询) 与 Gmail 邮箱 (IDLE 实时监听)
- **AI智能分类**: 基于 OpenAI 兼容 LLM API，自动将邮件分类到6大类别
- **自动归档**: 根据分类自动移动邮件到对应文件夹
- **实时通知**: 新邮件通过飞书Webhook即时推送
- **本地处理**: 邮件原文不外传，保护隐私
- **断线自愈**: 连接断开自动重连，指数退避策略
- **开机自启**: macOS launchd 服务支持

## 工作原理

```
IMAP 抓取  ──▶  AI 分类  ──▶  自动归档  ──▶  飞书通知
(轮询/IDLE)    (LLM + 规则)    (移动文件夹)    (Webhook 推送)
```

1. **抓取**：163 邮箱按固定间隔轮询，Gmail 用 IMAP IDLE 实时监听新邮件。
2. **分类**：邮件先过本地规则引擎，未命中再调用 LLM，归入 6 大类别。邮件原文不外传，只发送必要的标题/摘要给模型。
3. **归档**：按分类结果将邮件移动到对应文件夹，文件夹不存在时自动创建。
4. **通知**：新邮件实时推送到飞书，验证码类邮件会自动提取关键信息。
5. **状态**：本地 SQLite 仅保存邮件索引、分类结果和操作日志（不存正文），并按配置定期清理。

## 项目结构

```
mail-assistant/
├── config.yaml              # 主配置文件
├── .env.example             # 环境变量模板
├── requirements.txt         # Python依赖
├── run.py                   # 入口脚本
├── scripts/
│   └── mailctl.sh           # 服务管理脚本
├── src/
│   ├── main.py              # 主服务入口
│   ├── config.py            # 配置加载
│   ├── mail/                # 邮件模块
│   │   ├── connection.py    # IMAP连接管理
│   │   ├── fetcher.py       # 邮件抓取
│   │   └── parser.py        # 邮件解析
│   ├── classifier/          # 分类器模块
│   │   ├── ai_client.py     # LLM API
│   │   └── rules.py         # 规则引擎
│   ├── notifier/            # 通知模块
│   │   └── feishu.py        # 飞书通知
│   ├── archiver/            # 归档模块
│   │   └── mover.py         # 邮件归档
│   └── storage/             # 存储模块
│       ├── database.py      # SQLite操作
│       └── state.py         # 状态管理
├── logs/                    # 日志目录
├── tests/                   # 测试目录
└── data/                    # 数据目录
```

## 快速开始

> 环境要求：Python 3.14+。推荐使用 [uv](https://github.com/astral-sh/uv) 管理依赖与运行。

### 1. 安装依赖

```bash
cd mail-assistant

# 推荐：uv（与下文运行命令、launchd 服务一致）
uv pip install -r requirements.txt

# 或使用 pip
pip install -r requirements.txt
```

### 2. 配置凭据

邮箱授权码、LLM API Key、飞书 Webhook 等**敏感信息统一写在 `.env` 中**（不会被提交）：

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件，填入真实值
```

### 3. 配置账户

`config.yaml` 通过 `{{占位符}}` 引用 `.env` 中的凭据，**不要把密码直接写进 config.yaml**。
在 `config.yaml` 的 `accounts` 部分按需增删账户、调整 `enabled`、`folder_prefix` 等非敏感项。

**163 邮箱需要开启 IMAP 服务并获取授权码**
**Gmail 需要开启 IMAP 并生成 App Password**

### 4. 运行服务

```bash
# 前台运行
scripts/mailctl.sh run

# 启动后台服务（launchd）
scripts/mailctl.sh start

# 只处理存量邮件一次
scripts/mailctl.sh once

# 停止/重启/查看状态
scripts/mailctl.sh stop
scripts/mailctl.sh restart
scripts/mailctl.sh status

# 指定配置文件
uv run python run.py --config /path/to/config.yaml
```

### 5. 开机自启 (macOS)

```bash
# 安装并启动服务
scripts/mailctl.sh start

# 卸载服务
scripts/mailctl.sh uninstall
```

## 配置说明

### 邮件分类

支持6种分类标签：
- 工作沟通
- 通知提醒
- 垃圾邮件
- 社交动态
- 个人事务
- 技术·文档

### AI分类参数

```yaml
ai:
  base_url: ""                  # 留空时读取 LLM_API_BASE
  model: ""                     # 留空时读取 LLM_MODEL
  temperature: 0.1              # 温度参数
  confidence_threshold: 0.70    # 置信度阈值
```

### 数据库清理

数据库只保存邮件索引、分类结果和操作日志，不保存完整邮件正文。服务会按配置定期清理历史数据：

```yaml
database:
  cleanup:
    enabled: true
    interval_hours: 24
    processed_emails_retention_days: 180
    logs_retention_days: 30
```

清理范围只包含“已处理且已归档”的邮件索引和旧操作日志，未处理邮件会保留。

### IMAP连接策略

| 邮箱 | 模式 | 策略 |
|------|------|------|
| Gmail | IDLE | 12分钟心跳，30分钟超时重连 |
| 163 | 轮询 | 60秒间隔，≤3次/分钟 |

## 故障排查

### 连接失败
1. 检查网络连接
2. 确认IMAP服务已开启
3. 验证用户名/密码/授权码
4. 检查防火墙设置

### 分类不准确
1. 调低 `confidence_threshold`
2. 在 `config.yaml` 中新增 `rules` 段添加自定义规则（默认无此段，纯 AI 分类）
3. 检查邮件样本质量

### 飞书通知失败
1. 确认Webhook URL正确
2. 检查机器人是否被禁言
3. 验证签名密钥（如配置）

## 日志查看

```bash
# 实时查看日志
scripts/mailctl.sh logs

# 查看错误日志
scripts/mailctl.sh errors
```

## License

MIT License
