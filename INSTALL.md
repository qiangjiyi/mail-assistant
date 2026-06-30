# 邮箱整理助手 - Launchd 安装说明

## 准备工作

`scripts/mailctl.sh` 安装 launchd 服务时会根据本项目所在路径自动渲染 plist，无需手动维护。
`uv` 路径默认 `/opt/homebrew/bin/uv`，如不同可用 `UV_BIN=/path/to/uv scripts/mailctl.sh start` 指定。

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
2. 移动项目目录后，运行 `scripts/mailctl.sh restart` 即可，plist 会按新路径自动重新渲染
