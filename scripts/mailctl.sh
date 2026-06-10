#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.mailassistant.service"
PLIST_NAME="${LABEL}.plist"
SOURCE_PLIST="${PROJECT_ROOT}/${PLIST_NAME}"
TARGET_PLIST="${HOME}/Library/LaunchAgents/${PLIST_NAME}"
UV_BIN="${UV_BIN:-/opt/homebrew/bin/uv}"

cd "${PROJECT_ROOT}"

usage() {
  cat <<'EOF'
邮箱整理助手管理脚本

用法:
  scripts/mailctl.sh once        只运行一次存量邮件整理
  scripts/mailctl.sh run         前台运行服务
  scripts/mailctl.sh start       安装/加载并启动 launchd 服务
  scripts/mailctl.sh stop        停止 launchd 服务
  scripts/mailctl.sh restart     重启 launchd 服务
  scripts/mailctl.sh status      查看 launchd 服务状态
  scripts/mailctl.sh logs        实时查看应用日志
  scripts/mailctl.sh errors      查看最近错误日志
  scripts/mailctl.sh install     安装/重新加载 launchd 服务
  scripts/mailctl.sh uninstall   停止并卸载 launchd 服务

可选环境变量:
  UV_BIN=/path/to/uv             指定 uv 路径
EOF
}

require_uv() {
  if [[ ! -x "${UV_BIN}" ]]; then
    echo "找不到 uv: ${UV_BIN}" >&2
    echo "可用 UV_BIN=/path/to/uv scripts/mailctl.sh <command> 指定路径" >&2
    exit 1
  fi
}

run_once() {
  require_uv
  "${UV_BIN}" run python run.py --once "$@"
}

run_foreground() {
  require_uv
  "${UV_BIN}" run python run.py "$@"
}

install_service() {
  mkdir -p "${HOME}/Library/LaunchAgents"
  cp "${SOURCE_PLIST}" "${TARGET_PLIST}"
  launchctl unload "${TARGET_PLIST}" >/dev/null 2>&1 || true
  launchctl load "${TARGET_PLIST}"
  echo "已安装并加载服务: ${LABEL}"
}

start_service() {
  if [[ ! -f "${TARGET_PLIST}" ]]; then
    install_service
  fi
  launchctl start "${LABEL}"
  echo "已启动服务: ${LABEL}"
}

stop_service() {
  launchctl stop "${LABEL}" >/dev/null 2>&1 || true
  echo "已停止服务: ${LABEL}"
}

restart_service() {
  stop_service
  sleep 1
  start_service
}

status_service() {
  if launchctl list | grep -F "${LABEL}" >/dev/null; then
    launchctl list | grep -F "${LABEL}"
  else
    echo "服务未加载: ${LABEL}"
    return 1
  fi
}

uninstall_service() {
  launchctl stop "${LABEL}" >/dev/null 2>&1 || true
  launchctl unload "${TARGET_PLIST}" >/dev/null 2>&1 || true
  rm -f "${TARGET_PLIST}"
  echo "已卸载服务: ${LABEL}"
}

tail_logs() {
  mkdir -p logs
  tail -f logs/mail_assistant_*.log | colorize_logs
}

show_errors() {
  mkdir -p logs
  grep -hE "ERROR|WARNING" logs/*.log 2>/dev/null | tail -100 | colorize_logs || true
}

colorize_logs() {
  awk '
    BEGIN {
      reset = "\033[0m"
      dim = "\033[2m"
      green = "\033[32m"
      cyan = "\033[36m"
      blue = "\033[34m"
      yellow = "\033[33m"
      red = "\033[31m"
    }
    {
      color = reset
      if ($0 ~ /\| DEBUG[ ]*\|/) color = dim
      else if ($0 ~ /\| INFO[ ]*\|/) color = blue
      else if ($0 ~ /\| WARNING[ ]*\|/) color = yellow
      else if ($0 ~ /\| ERROR[ ]*\|/) color = red

      line = $0
      if (match(line, /^[0-9-]+ [0-9:]+/)) {
        printf "%s%s%s", green, substr(line, RSTART, RLENGTH), reset
        line = substr(line, RSTART + RLENGTH)
      }

      gsub(/\| DEBUG[ ]*\|/, "|" dim " DEBUG   " reset "|", line)
      gsub(/\| INFO[ ]*\|/, "|" color " INFO    " reset "|", line)
      gsub(/\| WARNING[ ]*\|/, "|" yellow " WARNING " reset "|", line)
      gsub(/\| ERROR[ ]*\|/, "|" red " ERROR   " reset "|", line)

      dash = index(line, " - ")
      if (dash > 0) {
        printf "%s%s%s%s\n", cyan, substr(line, 1, dash - 1), reset, substr(line, dash)
      } else {
        print line
      }
      fflush()
    }
  '
}

cmd="${1:-help}"
shift || true

case "${cmd}" in
  once)
    run_once "$@"
    ;;
  run)
    run_foreground "$@"
    ;;
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    restart_service
    ;;
  status)
    status_service
    ;;
  logs)
    tail_logs
    ;;
  errors)
    show_errors
    ;;
  install)
    install_service
    ;;
  uninstall)
    uninstall_service
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "未知命令: ${cmd}" >&2
    usage
    exit 1
    ;;
esac
