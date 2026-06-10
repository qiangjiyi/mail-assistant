#!/usr/bin/env python3
"""
邮箱整理助手 - 入口脚本
支持命令行参数控制运行模式

Usage:
    python run.py                    # 正常启动（前台）
    python run.py --daemon           # 后台运行
    python run.py --once             # 只跑一次存量整理
    python run.py --config /path/to/config.yaml  # 指定配置文件
"""

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Optional

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="邮箱整理助手 - AI智能分类归档邮件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python run.py                    # 正常启动
    python run.py --daemon           # 后台运行
    python run.py --once             # 只处理存量邮件一次
    python run.py --config ./custom.yaml  # 使用自定义配置
        """
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="以后台守护进程模式运行"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只运行一次存量邮件整理，然后退出"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="指定配置文件路径（默认: ./config.yaml）"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="启用调试模式"
    )
    return parser.parse_args()


def setup_logging(debug: bool = False) -> None:
    """配置日志"""
    from loguru import logger
    
    # 移除默认处理器
    logger.remove()
    
    # 控制台输出
    level = "DEBUG" if debug else "INFO"
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=level,
        colorize=True
    )
    
    # 文件输出
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    
    logger.add(
        log_dir / "mail_assistant_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="30 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level="DEBUG"
    )


def daemonize() -> None:
    """将进程转为后台守护进程"""
    # 在Unix系统上实现守护进程化
    if os.name != "posix":
        print("警告: 守护进程模式仅在Unix系统上支持")
        return
    
    try:
        pid = os.fork()
        if pid > 0:
            # 父进程退出
            print(f"守护进程已启动，PID: {pid}")
            sys.exit(0)
    except OSError as e:
        print(f"fork失败: {e}")
        sys.exit(1)
    
    # 子进程：创建新会话
    os.setsid()
    
    # 改变工作目录
    os.chdir(str(PROJECT_ROOT))
    
    # 重定向标准文件描述符
    sys.stdout.flush()
    sys.stderr.flush()
    
    with open("/dev/null", "r") as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())
    with open("/dev/null", "a+") as devnull:
        os.dup2(devnull.fileno(), sys.stdout.fileno())
        os.dup2(devnull.fileno(), sys.stderr.fileno())


async def async_main(args: argparse.Namespace) -> None:
    """异步主函数"""
    from loguru import logger
    from src.main import MailAssistantService
    
    # 加载配置
    config_path = args.config or str(PROJECT_ROOT / "config.yaml")
    
    service = MailAssistantService(config_path=config_path)
    
    # 设置信号处理
    loop = asyncio.get_running_loop()
    service_task: Optional[asyncio.Task] = None
    stop_requested = False

    def request_stop(sig: signal.Signals) -> None:
        nonlocal stop_requested
        if stop_requested:
            logger.warning(f"再次收到信号 {sig.value}，强制退出")
            os._exit(128 + sig.value)

        logger.info(f"收到信号 {sig.value}，正在停止服务...")
        stop_requested = True
        if service_task and not service_task.done():
            service_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop, sig)
        except NotImplementedError:
            signal.signal(sig, lambda signum, _frame: request_stop(signal.Signals(signum)))
    
    try:
        service_task = loop.create_task(service.start(run_once=args.once))
        await service_task
    except asyncio.CancelledError:
        logger.info("服务主任务已取消")
    except Exception as e:
        logger.exception(f"服务异常: {e}")
        raise
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except NotImplementedError:
                signal.signal(sig, signal.SIG_DFL)


def main() -> None:
    """主入口"""
    args = parse_args()
    
    # 配置日志
    debug_mode = args.debug or os.getenv("DEBUG", "false").lower() == "true"
    setup_logging(debug=debug_mode)
    
    from loguru import logger
    logger.info("=" * 50)
    logger.info("邮箱整理助手启动")
    logger.info("=" * 50)
    
    # 守护进程模式
    if args.daemon:
        daemonize()
    
    try:
        # 运行异步主函数
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        logger.info("用户中断，退出")
    except Exception as e:
        logger.exception(f"启动失败: {e}")
        sys.exit(1)
    
    logger.info("服务已停止")


if __name__ == "__main__":
    main()
