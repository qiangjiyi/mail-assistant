"""
AI分类客户端
调用 OpenAI 兼容的 LLM API 进行邮件分类
"""

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from ..config import get_config
from ..mail.parser import EmailData


class LLMClient:
    """OpenAI 兼容 LLM 客户端"""
    
    def __init__(self, api_key: str, base_url: str):
        """
        初始化客户端
        
        Args:
            api_key: API密钥
            base_url: API基础URL
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
    
    async def __aenter__(self) -> "LLMClient":
        """异步上下文管理器入口"""
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器出口"""
        if self._client:
            await self._client.aclose()
            self._client = None
    
    async def chat_completion(
        self,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 500,
    ) -> Dict[str, Any]:
        """
        调用聊天补全API
        
        Args:
            model: 模型名称
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            
        Returns:
            API响应
        """
        if not self._client:
            raise RuntimeError("客户端未初始化，请使用 async with 上下文")
        
        try:
            response = await self._client.post(
                "/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
            )
            
            response.raise_for_status()
            return response.json()
            
        except httpx.HTTPStatusError as e:
            logger.error(f"API请求失败: {e.response.status_code} - {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"API请求异常: {e}")
            raise


class AIClassifier:
    """AI邮件分类器"""
    
    # Few-shot 示例
    FEW_SHOT_EXAMPLES = """
示例分类：

示例1:
发件人: hr@company.com
主题: 面试邀请通知
正文: 您好，我们收到了您的简历，现邀请您于下周一参加技术岗位面试...
分类结果: {"category": "工作沟通", "confidence": 0.92, "summary": "收到公司HR的面试邀请"}

示例2:
发件人: system@alipay.com
主题: 您的账户安全验证
正文: 尊敬的用户，您的账户检测到异常登录...
分类结果: {"category": "通知提醒", "confidence": 0.95, "summary": "支付宝账户安全验证通知"}

示例3:
主题: 您有一条新的微信消息
正文: [有人@你] 张三: 今晚一起吃饭吗？
分类结果: {"category": "社交动态", "confidence": 0.88, "summary": "微信收到朋友消息"}

示例4:
发件人: noreply@github.com
主题: [repo] Pull request #123 opened
正文: A new pull request has been opened...
分类结果: {"category": "技术·文档", "confidence": 0.85, "summary": "GitHub项目有新的Pull Request"}
"""
    
    SYSTEM_PROMPT = f"""你是一个专业的邮件分类助手，负责将邮件分类到以下类别：
- 工作沟通：工作相关的邮件，如项目讨论、会议通知、工作汇报等
- 通知提醒：系统通知、账单、订单、物流等提醒类邮件
- 垃圾邮件：广告、推广、垃圾信息等
- 社交动态：社交媒体、好友消息、社区通知等
- 个人事务：私人通信、个人订阅、生活相关等
- 技术·文档：技术文档、代码相关、技术新闻等

分类规则：
1. 根据邮件内容和发件人综合判断
2. 工作邮箱的发件人邮件通常属于工作沟通
3. 系统自动发送的通知通常属于通知提醒
4. 社交平台的消息属于社交动态
5. 带有明显广告性质的属于垃圾邮件

{FEW_SHOT_EXAMPLES}

请对以下邮件进行分类，并返回JSON格式结果：
{{"category": "分类名称", "confidence": 置信度(0-1), "summary": "一句话摘要"}}

重要：只返回JSON，不要包含其他文字。"""
    
    def __init__(self):
        self._config = get_config()
        self._ai_config = self._config.ai
        self._classification_config = self._ai_config.get("classification", {})
        
        self._confidence_threshold = self._classification_config.get("confidence_threshold", 0.70)
        self._batch_window = self._classification_config.get("batch_window_seconds", 300)
        self._stock_batch_size = self._classification_config.get("stock_batch_size", 5)
        
        # 待处理邮件队列（用于批量处理）
        self._pending_emails: List[EmailData] = []
        self._pending_task: Optional[asyncio.Task] = None
    
    async def classify_email(
        self,
        email: EmailData,
        include_secondary: bool = False,
    ) -> Dict[str, Any]:
        """
        对单封邮件进行分类
        
        Args:
            email: 邮件数据
            include_secondary: 是否包含正文200-500字片段
            
        Returns:
            分类结果 {"category": str, "confidence": float, "summary": str}
        """
        # 获取API配置
        api_key = self._get_llm_config_value("api_key", "LLM_API_KEY")
        base_url = self._get_llm_config_value("base_url", "LLM_API_BASE")
        model = self._get_llm_config_value("model", "LLM_MODEL")
        temperature = self._ai_config.get("temperature", 0.1)
        max_tokens = self._ai_config.get("max_tokens", 500)
        timeout = self._ai_config.get("timeout_seconds", 30)
        retry_attempts = self._ai_config.get("retry_attempts", 3)
        retry_delay = self._ai_config.get("retry_delay_seconds", 2)
        
        if not api_key:
            logger.error("未配置 LLM API Key")
            return self._get_default_result()
        if not base_url:
            logger.error("未配置 LLM API Base")
            return self._get_default_result()
        if not model:
            logger.error("未配置 LLM Model")
            return self._get_default_result()
        
        # 构建消息
        body_text = self._get_body_for_classification(email, include_secondary)
        
        user_message = f"""发件人: {email.display_sender} <{email.sender_email}>
主题: {email.subject}
正文: {body_text}"""
        
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        
        # 调用API，带重试
        for attempt in range(retry_attempts):
            try:
                async with LLMClient(api_key, base_url) as client:
                    response = await asyncio.wait_for(
                        client.chat_completion(
                            model=model,
                            messages=messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                        ),
                        timeout=timeout,
                    )
                    
                    # 解析响应
                    result = self._parse_response(response)
                    
                    # 验证分类是否有效
                    if result and self._is_valid_category(result.get("category")):
                        logger.debug(
                            f"邮件分类成功: {email.subject[:30]}... -> "
                            f"{result['category']} ({result['confidence']:.2f})"
                        )
                        return result
                    else:
                        logger.warning(f"分类结果无效: {result}")
                        return self._get_default_result()
                        
            except asyncio.TimeoutError:
                logger.warning(f"API超时 (尝试 {attempt + 1}/{retry_attempts})")
            except Exception as e:
                logger.error(f"API调用失败: {e} (尝试 {attempt + 1}/{retry_attempts})")
            
            if attempt < retry_attempts - 1:
                await asyncio.sleep(retry_delay)
        
        logger.error(f"邮件分类失败，已达到最大重试次数: {email.subject[:30]}")
        return self._get_default_result()

    def _get_llm_config_value(self, config_key: str, env_name: str) -> Optional[str]:
        """读取通用 LLM 配置。"""
        value = self._ai_config.get(config_key)
        if value:
            return value

        return os.getenv(env_name)
    
    def _get_body_for_classification(
        self,
        email: EmailData,
        include_secondary: bool = False,
    ) -> str:
        """
        获取用于分类的正文内容
        
        Args:
            email: 邮件数据
            include_secondary: 是否包含200-500字片段
            
        Returns:
            处理后的正文
        """
        body = email.body_plain
        
        if include_secondary and len(body) > 200:
            # 返回200-500字片段
            return body[200:500]
        
        # 返回前200字
        if len(body) > 200:
            return body[:200]
        return body
    
    def _parse_response(self, response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        解析API响应
        
        Args:
            response: API响应
            
        Returns:
            分类结果或None
        """
        try:
            choices = response.get("choices", [])
            if not choices:
                return None
            
            content = choices[0].get("message", {}).get("content", "")
            
            # 尝试提取JSON
            content = content.strip()
            
            # 方法1: 直接解析
            try:
                result = json.loads(content)
                if "category" in result and "confidence" in result:
                    return {
                        "category": str(result["category"]),
                        "confidence": float(result["confidence"]),
                        "summary": str(result.get("summary", "")),
                    }
            except json.JSONDecodeError:
                pass
            
            # 方法2: 尝试用正则提取 ```json ... ``` 块
            import re
            json_block_pattern = r'```json\s*\n?(.*?)\n?```'
            matches = re.findall(json_block_pattern, content, re.DOTALL)
            
            for match in matches:
                try:
                    result = json.loads(match.strip())
                    if "category" in result and "confidence" in result:
                        return {
                            "category": str(result["category"]),
                            "confidence": float(result["confidence"]),
                            "summary": str(result.get("summary", "")),
                        }
                except json.JSONDecodeError:
                    continue
            
            # 方法3: 尝试从推理文字后提取JSON对象
            json_pattern = r'\{[^{}]*"category"\s*:\s*"[^"]+"\s*,\s*"confidence"\s*:\s*[\d.]+\s*,\s*"summary"\s*:\s*"[^"]*"[^{}]*\}'
            matches = re.findall(json_pattern, content, re.DOTALL)
            
            for match in matches:
                try:
                    result = json.loads(match)
                    if "category" in result and "confidence" in result:
                        return {
                            "category": str(result["category"]),
                            "confidence": float(result["confidence"]),
                            "summary": str(result.get("summary", "")),
                        }
                except json.JSONDecodeError:
                    continue
            
            logger.warning(f"未能从响应中提取JSON: {content[:200]}...")
            return None
            
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"解析响应失败: {e}, 内容: {response}")
            return None
    
    def _is_valid_category(self, category: str) -> bool:
        """验证分类是否在允许的列表中"""
        if not category:
            return False
        
        valid_categories = self._config.categories
        return category in valid_categories
    
    def _get_default_result(self) -> Dict[str, Any]:
        """获取默认分类结果"""
        return {
            "category": self._classification_config.get("default_category", "个人事务"),
            "confidence": 0.5,
            "summary": "未能自动分类",
        }
    
    async def classify_email_with_retry(
        self,
        email: EmailData,
    ) -> Dict[str, Any]:
        """
        对邮件进行分类，低置信度时二次判断
        
        Args:
            email: 邮件数据
            
        Returns:
            分类结果
        """
        # 首次分类
        result = await self.classify_email(email, include_secondary=False)
        
        # 置信度低于阈值，补充正文片段二次判断
        if result["confidence"] < self._confidence_threshold:
            logger.info(
                f"置信度低 ({result['confidence']:.2f})，补充正文片段二次判断: "
                f"{email.subject[:30]}..."
            )
            
            # 获取正文200-500字片段
            secondary_body = self._get_body_for_classification(email, include_secondary=True)
            
            if len(secondary_body) > 50:  # 有足够的次要内容
                # 二次分类
                result2 = await self.classify_email(email, include_secondary=True)
                
                # 合并结果：取较高置信度的结果
                if result2["confidence"] > result["confidence"]:
                    result = result2
        
        return result
    
    async def classify_batch(
        self,
        emails: List[EmailData],
    ) -> List[Dict[str, Any]]:
        """
        批量分类邮件
        
        Args:
            emails: 邮件列表
            
        Returns:
            分类结果列表
        """
        if not emails:
            return []
        
        logger.info(f"开始批量分类 {len(emails)} 封邮件")
        
        results = []
        for email in emails:
            result = await self.classify_email_with_retry(email)
            results.append(result)
            
            # 避免请求过快
            await asyncio.sleep(0.5)
        
        logger.info(f"批量分类完成: {len(results)} 封")
        return results
    
    def apply_result_to_email(self, email: EmailData, result: Dict[str, Any]) -> None:
        """
        将分类结果应用到邮件对象
        
        Args:
            email: 邮件数据
            result: 分类结果
        """
        email.ai_category = result.get("category")
        email.ai_confidence = result.get("confidence")
        email.ai_summary = result.get("summary")
