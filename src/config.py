"""
配置加载模块
负责加载和解析 YAML 配置文件和环境变量
"""

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv
from loguru import logger

# 尝试加载 .env 文件
_project_root = Path(__file__).parent.parent
_env_path = _project_root / ".env"
if _env_path.exists():
    load_dotenv(_env_path)
    logger.debug(f"已加载环境变量文件: {_env_path}")


class Config:
    """配置管理类"""
    
    def __init__(self, config_path: Optional[str] = None):
        """
        初始化配置
        
        Args:
            config_path: 配置文件路径，默认使用项目根目录下的 config.yaml
        """
        if config_path:
            self._config_path = Path(config_path)
        else:
            self._config_path = _project_root / "config.yaml"
        
        self._config: Dict[str, Any] = {}
        self._load_config()
    
    def _load_config(self) -> None:
        """加载配置文件"""
        if not self._config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self._config_path}")
        
        logger.info(f"加载配置文件: {self._config_path}")
        
        with open(self._config_path, "r", encoding="utf-8") as f:
            raw_config = yaml.safe_load(f)
        
        # 替换环境变量占位符
        self._config = self._substitute_env_vars(raw_config)
        logger.debug("配置加载完成")
    
    def _substitute_env_vars(self, obj: Any) -> Any:
        """
        递归替换配置中的环境变量占位符
        格式: {{ENV_VAR_NAME}}
        """
        if isinstance(obj, dict):
            return {k: self._substitute_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._substitute_env_vars(item) for item in obj]
        elif isinstance(obj, str):
            # 替换 {{VAR_NAME}} 格式的环境变量
            pattern = r'\{\{(\w+)\}\}'
            matches = re.findall(pattern, obj)
            for var_name in matches:
                env_value = os.getenv(var_name, "")
                if not env_value:
                    logger.warning(f"环境变量未设置: {var_name}")
                obj = obj.replace(f"{{{{{var_name}}}}}", env_value)
            return obj
        else:
            return obj
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值，支持点号路径
        例如: config.get("app.debug")
        """
        keys = key.split(".")
        value = self._config
        
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        
        return value
    
    def get_section(self, section: str) -> Dict[str, Any]:
        """获取配置节"""
        return self.get(section, {})
    
    @property
    def app(self) -> Dict[str, Any]:
        """应用配置"""
        return self.get_section("app")
    
    @property
    def database(self) -> Dict[str, Any]:
        """数据库配置"""
        return self.get_section("database")
    
    @property
    def accounts(self) -> List[Dict[str, Any]]:
        """邮箱账户配置列表"""
        return self.get("accounts", [])
    
    @property
    def ai(self) -> Dict[str, Any]:
        """AI配置"""
        return self.get_section("ai")
    
    @property
    def feishu(self) -> Dict[str, Any]:
        """飞书配置"""
        return self.get_section("feishu")
    
    @property
    def archive(self) -> Dict[str, Any]:
        """归档配置"""
        return self.get_section("archive")
    
    @property
    def reconnect(self) -> Dict[str, Any]:
        """重连配置"""
        return self.get_section("reconnect")
    
    @property
    def fetcher(self) -> Dict[str, Any]:
        """抓取配置"""
        return self.get_section("fetcher")
    
    @property
    def categories(self) -> List[str]:
        """分类标签列表"""
        return self.get("categories", [])
    
    @property
    def category_folders(self) -> Dict[str, str]:
        """分类到文件夹的映射"""
        return self.get("category_folders", {})
    
    def get_enabled_accounts(self) -> List[Dict[str, Any]]:
        """获取已启用的邮箱账户列表"""
        return [acc for acc in self.accounts if acc.get("enabled", True)]
    
    def validate(self) -> List[str]:
        """
        验证配置完整性
        Returns:
            错误信息列表，空列表表示验证通过
        """
        errors = []
        
        # 检查必要的配置节
        required_sections = ["app", "database", "accounts", "ai", "feishu"]
        for section in required_sections:
            if not self.get_section(section):
                errors.append(f"缺少配置节: {section}")
        
        # 检查账户配置
        for i, account in enumerate(self.accounts):
            required_fields = ["name", "email", "imap_host", "imap_port", "username", "password"]
            for field in required_fields:
                if not account.get(field):
                    errors.append(f"账户 {i} 缺少必要字段: {field}")
        
        # 检查飞书配置
        if self.feishu.get("enabled"):
            if not self.feishu.get("webhook_url"):
                errors.append("飞书Webhook URL未配置")
        
        # 检查AI配置
        if not self.ai.get("api_key") and not os.getenv("LLM_API_KEY"):
            errors.append("LLM API Key未配置")
        if not self.ai.get("base_url") and not os.getenv("LLM_API_BASE"):
            errors.append("LLM API Base未配置")
        if not self.ai.get("model") and not os.getenv("LLM_MODEL"):
            errors.append("LLM Model未配置")
        
        return errors
    
    def __repr__(self) -> str:
        return f"Config(path={self._config_path})"


# 全局配置实例
_config_instance: Optional[Config] = None


def get_config(config_path: Optional[str] = None) -> Config:
    """获取配置单例"""
    global _config_instance
    if _config_instance is None:
        _config_instance = Config(config_path)
    return _config_instance


def reload_config(config_path: Optional[str] = None) -> Config:
    """重新加载配置"""
    global _config_instance
    _config_instance = Config(config_path)
    return _config_instance
