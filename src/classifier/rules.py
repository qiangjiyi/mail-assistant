"""
规则引擎模块
基于规则的邮件分类作为AI分类的补充
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from ..config import get_config
from ..mail.parser import EmailData


@dataclass
class ClassificationRule:
    """分类规则"""
    name: str
    priority: int  # 优先级，数字越大优先级越高
    category: str
    keyword: Optional[str] = None  # 主题/正文中匹配的关键词
    sender_pattern: Optional[str] = None  # 发件人正则匹配
    subject_pattern: Optional[str] = None  # 主题正则匹配
    body_pattern: Optional[str] = None  # 正文正则匹配
    confidence_boost: float = 0.0  # 置信度加成
    enabled: bool = True


class RuleEngine:
    """规则引擎"""
    
    def __init__(self):
        self._config = get_config()
        self._rules: List[ClassificationRule] = []
        self._load_rules()
    
    def _load_rules(self) -> None:
        """从配置加载规则"""
        rules_config = self._config.get("rules", [])
        
        for rule_data in rules_config:
            rule = ClassificationRule(
                name=rule_data.get("name", ""),
                priority=rule_data.get("priority", 0),
                category=rule_data.get("category", ""),
                keyword=rule_data.get("keyword"),
                sender_pattern=rule_data.get("sender_pattern"),
                subject_pattern=rule_data.get("subject_pattern"),
                body_pattern=rule_data.get("body_pattern"),
                confidence_boost=rule_data.get("confidence_boost", 0.0),
                enabled=rule_data.get("enabled", True),
            )
            self._rules.append(rule)
        
        # 按优先级排序
        self._rules.sort(key=lambda r: r.priority, reverse=True)
        
        logger.debug(f"已加载 {len(self._rules)} 条分类规则")
    
    def add_rule(self, rule: ClassificationRule) -> None:
        """
        添加规则
        
        Args:
            rule: 分类规则
        """
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority, reverse=True)
        logger.info(f"添加规则: {rule.name}")
    
    def remove_rule(self, name: str) -> bool:
        """
        移除规则
        
        Args:
            name: 规则名称
            
        Returns:
            是否成功移除
        """
        for i, rule in enumerate(self._rules):
            if rule.name == name:
                del self._rules[i]
                logger.info(f"移除规则: {name}")
                return True
        return False
    
    def get_rule(self, name: str) -> Optional[ClassificationRule]:
        """获取指定规则"""
        for rule in self._rules:
            if rule.name == name:
                return rule
        return None
    
    def enable_rule(self, name: str) -> bool:
        """启用规则"""
        rule = self.get_rule(name)
        if rule:
            rule.enabled = True
            return True
        return False
    
    def disable_rule(self, name: str) -> bool:
        """禁用规则"""
        rule = self.get_rule(name)
        if rule:
            rule.enabled = False
            return True
        return False
    
    def match(self, email: EmailData) -> List[Tuple[ClassificationRule, float]]:
        """
        匹配邮件规则
        
        Args:
            email: 邮件数据
            
        Returns:
            匹配的规则列表及匹配分数 [(rule, score), ...]
        """
        matches = []
        
        for rule in self._rules:
            if not rule.enabled:
                continue
            
            score = self._calculate_rule_score(email, rule)
            
            if score > 0:
                matches.append((rule, score))
        
        # 按分数排序
        matches.sort(key=lambda x: x[1], reverse=True)
        
        return matches
    
    def _calculate_rule_score(self, email: EmailData, rule: ClassificationRule) -> float:
        """
        计算规则匹配分数
        
        Args:
            email: 邮件数据
            rule: 分类规则
            
        Returns:
            匹配分数 (0-1)
        """
        score = 0.0
        matched = False
        
        # 发件人匹配
        if rule.sender_pattern:
            pattern = re.compile(rule.sender_pattern, re.IGNORECASE)
            if pattern.search(email.sender_email) or pattern.search(email.sender):
                score += 0.4
                matched = True
        
        # 主题匹配
        if rule.subject_pattern:
            pattern = re.compile(rule.subject_pattern, re.IGNORECASE)
            if pattern.search(email.subject):
                score += 0.3
                matched = True
        
        # 正文匹配
        if rule.body_pattern:
            pattern = re.compile(rule.body_pattern, re.IGNORECASE)
            if pattern.search(email.body_plain):
                score += 0.3
                matched = True
        
        # 关键词匹配（简单包含）
        if rule.keyword:
            keyword_lower = rule.keyword.lower()
            subject_lower = email.subject.lower()
            body_lower = email.body_plain.lower()
            
            if keyword_lower in subject_lower:
                score += 0.2
                matched = True
            if keyword_lower in body_lower:
                score += 0.2
        
        # 归一化分数
        if matched:
            score = min(score + rule.confidence_boost, 1.0)
        
        return score
    
    def classify_by_rules(self, email: EmailData) -> Optional[Dict[str, Any]]:
        """
        仅使用规则对邮件进行分类
        
        Args:
            email: 邮件数据
            
        Returns:
            分类结果或None
        """
        matches = self.match(email)
        
        if not matches:
            return None
        
        # 取最高分的规则
        best_rule, best_score = matches[0]
        
        # 分数阈值
        if best_score < 0.5:
            return None
        
        return {
            "category": best_rule.category,
            "confidence": min(best_score, 0.99),
            "summary": f"规则匹配: {best_rule.name}",
            "rule_name": best_rule.name,
        }
    
    def enhance_ai_result(
        self,
        email: EmailData,
        ai_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        用规则增强AI分类结果
        
        Args:
            email: 邮件数据
            ai_result: AI分类结果
            
        Returns:
            增强后的分类结果
        """
        # 如果AI置信度已经很高，不需要增强
        if ai_result.get("confidence", 0) >= 0.85:
            return ai_result
        
        # 尝试用规则匹配
        rule_result = self.classify_by_rules(email)
        
        if rule_result:
            # 如果规则匹配分数高于AI分数，使用规则结果
            if rule_result.get("confidence", 0) > ai_result.get("confidence", 0):
                logger.info(
                    f"规则覆盖AI分类: {email.subject[:30]}... "
                    f"(AI: {ai_result['confidence']:.2f}, 规则: {rule_result['confidence']:.2f})"
                )
                return rule_result
        
        return ai_result
    
    def get_all_rules(self) -> List[ClassificationRule]:
        """获取所有规则"""
        return self._rules.copy()
    
    def get_rules_by_category(self, category: str) -> List[ClassificationRule]:
        """获取指定分类的规则"""
        return [r for r in self._rules if r.category == category]
