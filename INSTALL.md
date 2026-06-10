# 邮箱整理助手 - Launchd 安装说明

## 准备工作

`com.mailassistant.service.plist` 中的路径默认是占位符 `/path/to/mail-assistant`，
首次安装前请将其替换为本项目在你机器上的真实绝对路径（共 5 处）。
同时确认 `uv` 路径（默认 `/opt/homebrew/bin/uv`），如不同可在运行命令时用 `UV_BIN=/path/to/uv` 指定。

## 安装步骤

1. 安装并启动服务：

```bash
scripts/mailctl.sh start
```

2. 查看服务状态：

```bash
scripts/mailctl.sh status
```

3. 查看日志：

```bash
scripts/mailctl.sh logs
```

## 常用命令

### 只整理一次
```bash
scripts/mailctl.sh once
```

### 停止服务
```bash
scripts/mailctl.sh stop
```

### 卸载服务
```bash
scripts/mailctl.sh uninstall
```

### 查看服务状态
```bash
scripts/mailctl.sh status
```

### 重启服务
```bash
scripts/mailctl.sh restart
```

## 注意事项

1. 确保 `.env` 文件已正确配置
2. 确保 Python 路径和项目路径正确
3. 如果修改了 plist 文件，需要先卸载再重新加载
