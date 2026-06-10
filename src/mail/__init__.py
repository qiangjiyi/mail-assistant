"""
邮件模块
"""

from .connection import IMAPConnection, IMAPAccount, ConnectionManager
from .fetcher import MailFetcher
from .parser import MailParser, EmailData

__all__ = [
    "IMAPConnection",
    "IMAPAccount",
    "ConnectionManager",
    "MailFetcher",
    "MailParser",
    "EmailData",
]
