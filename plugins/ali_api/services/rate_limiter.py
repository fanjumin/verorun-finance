#!/usr/bin/env python3
"""
阿里巴巴API四层风控机制

1. 用户级限流：每个用户/API Key每日/每小时调用次数限制
2. 全局并发控制：全站并发请求数限制，防止过载
3. 熔断保护：API异常率超过阈值时自动熔断
4. 审计与告警：完整日志记录，异常时告警通知
"""

import time
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
import logging
from collections import defaultdict

from ..config import config

logger = logging.getLogger(__name__)

class RateLimiter:
    """用户级限流器"""
    
    def __init__(self):
        self.user_stats = {}  # user_id -> {'daily': count, 'hourly': count, 'last_reset': timestamp}
        self.lock = threading.Lock()
        
        # 从配置获取限制
        self.daily_limit = config['rate_limit']['user_daily_limit']
        self.hourly_limit = config['rate_limit']['user_hourly_limit']
    
    def _reset_if_needed(self, user_id: int, now: float) -> None:
        """检查并重置计数"""
        if user_id not in self.user_stats:
            self.user_stats[user_id] = {
                'daily': 0,
                'hourly': 0,
                'daily_reset': now,
                'hourly_reset': now,
            }
            return
        
        stats = self.user_stats[user_id]
        
        # 检查每日重置
        daily_reset_time = datetime.fromtimestamp(stats['daily_reset'])
        if datetime.fromtimestamp(now).date() > daily_reset_time.date():
            stats['daily'] = 0
            stats['daily_reset'] = now
        
        # 检查每小时重置
        hourly_reset_time = datetime.fromtimestamp(stats['hourly_reset'])
        if datetime.fromtimestamp(now) - hourly_reset_time > timedelta(hours=1):
            stats['hourly'] = 0
            stats['hourly_reset'] = now
    
    def check_user_limit(self, user_id: int) -> Tuple[bool, Optional[str]]:
        """检查用户是否超过限制"""
        with self.lock:
            now = time.time()
            self._reset_if_needed(user_id, now)
            
            stats = self.user_stats[user_id]
            
            # 检查限制
            if stats['daily'] >= self.daily_limit:
                return False, f"每日调用次数超过限制 ({self.daily_limit}次)"
            
            if stats['hourly'] >= self.hourly_limit:
                return False, f"每小时调用次数超过限制({self.hourly_limit}次)"
            
            # 增加计数
            stats['daily'] += 1
            stats['hourly'] += 1
            
            return True, None
    
    def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        """获取用户统计信息"""
        with self.lock:
            if user_id not in self.user_stats:
                return {'daily': 0, 'hourly': 0, 'daily_limit': self.daily_limit, 'hourly_limit': self.hourly_limit}
            
            stats = self.user_stats[user_id]
            now = time.time()
            self._reset_if_needed(user_id, now)
            
            return {
                'daily': stats['daily'],
                'hourly': stats['hourly'],
                'daily_limit': self.daily_limit,
                'hourly_limit': self.hourly_limit,
                'daily_remaining': self.daily_limit - stats['daily'],
                'hourly_remaining': self.hourly_limit - stats['hourly'],
            }

class ConcurrentController:
    """全局并发控制器"""
    
    def __init__(self):
        self.active_requests = 0
        self.max_concurrent = config['rate_limit']['global_concurrent_limit']
        self.qps_limit = config['rate_limit']['global_qps_limit']
        self.lock = threading.Lock()
        
        # QPS控制
        self.request_timestamps = []
        self.qps_window = 1.0  # 1秒窗口
    
    def acquire(self) -> Tuple[bool, Optional[str]]:
        """获取并发许可"""
        with self.lock:
            now = time.time()
            
            # 检查并发数
            if self.active_requests >= self.max_concurrent:
                return False, f"并发请求数超过限制({self.max_concurrent})"
            
            # 检查QPS
            # 清理过期的时间戳
            self.request_timestamps = [ts for ts in self.request_timestamps if now - ts < self.qps_window]
            
            if len(self.request_timestamps) >= self.qps_limit:
                return False, f"QPS超过限制 ({self.qps_limit}/秒)"
            
            # 记录请求
            self.active_requests += 1
            self.request_timestamps.append(now)
            
            return True, None
    
    def release(self) -> None:
        """释放并发许可"""
        with self.lock:
            if self.active_requests > 0:
                self.active_requests -= 1
    
    def release_safe(self) -> None:
        """安全释放并发许可（可重复调用）"""
        with self.lock:
            self.active_requests = max(0, self.active_requests - 1)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取并发统计"""
        with self.lock:
            now = time.time()
            self.request_timestamps = [ts for ts in self.request_timestamps if now - ts < self.qps_window]
            
            return {
                'active_requests': self.active_requests,
                'max_concurrent': self.max_concurrent,
                'current_qps': len(self.request_timestamps),
                'qps_limit': self.qps_limit,
            }

class CircuitBreaker:
    """熔断保护器"""
    
    def __init__(self):
        self.threshold = config['rate_limit']['circuit_breaker_threshold']
        self.window = config['rate_limit']['circuit_breaker_window']
        self.timeout = config['rate_limit']['circuit_breaker_timeout']
        
        self.states = {}  # endpoint -> {'failures': 0, 'total': 0, 'state': 'closed', 'opened_at': None}
        self.lock = threading.Lock()
    
    def record_result(self, endpoint: str, success: bool) -> None:
        """记录API调用结果"""
        with self.lock:
            if endpoint not in self.states:
                self.states[endpoint] = {
                    'failures': 0,
                    'total': 0,
                    'state': 'closed',
                    'opened_at': None,
                    'history': [],
                }
            
            state = self.states[endpoint]
            state['total'] += 1
            
            if not success:
                state['failures'] += 1
            
            # 记录历史（滑动窗口）
            now = time.time()
            state['history'].append((now, success))
            
            # 清理过期记录
            cutoff = now - self.window
            state['history'] = [(ts, s) for ts, s in state['history'] if ts > cutoff]
            
            # 重新计算失败率
            if state['history']:
                failures = sum(1 for _, s in state['history'] if not s)
                total = len(state['history'])
                failure_rate = failures / total if total > 0 else 0
                
                # 检查是否需要熔断
                if state['state'] == 'closed' and failure_rate >= self.threshold:
                    state['state'] = 'open'
                    state['opened_at'] = now
                    logger.warning(f"熔断器打开: {endpoint}, 失败率: {failure_rate:.2%}")
                
                # 检查是否可以恢复
                elif state['state'] == 'open':
                    if now - state['opened_at'] >= self.timeout:
                        state['state'] = 'half-open'
                        logger.info(f"熔断器进入半开状态: {endpoint}")
    
    def is_allowed(self, endpoint: str) -> Tuple[bool, Optional[str]]:
        """检查是否允许调用"""
        with self.lock:
            if endpoint not in self.states:
                return True, None
            
            state = self.states[endpoint]
            
            if state['state'] == 'open':
                elapsed = time.time() - (state['opened_at'] or 0)
                # 超时自动恢复：进入半开状态，放行本次试探请求（修复永久卡死 bug）
                if elapsed >= self.timeout:
                    state['state'] = 'half-open'
                    logger.info(f"熔断器超时恢复，进入半开状态: {endpoint}")
                    return True, None
                remaining = max(0, self.timeout - elapsed)
                return False, f"API熔断中，请等待{int(remaining)} 秒后重试"
            
            return True, None
    
    def reset(self, endpoint: str) -> None:
        """重置熔断器"""
        with self.lock:
            if endpoint in self.states:
                self.states[endpoint] = {
                    'failures': 0,
                    'total': 0,
                    'state': 'closed',
                    'opened_at': None,
                    'history': [],
                }
                logger.info(f"熔断器重置: {endpoint}")
    
    def get_stats(self, endpoint: str = None) -> Dict[str, Any]:
        """获取熔断器统计"""
        with self.lock:
            if endpoint:
                if endpoint not in self.states:
                    return {'state': 'closed', 'failure_rate': 0, 'total_calls': 0}
                
                state = self.states[endpoint]
                failure_rate = state['failures'] / state['total'] if state['total'] > 0 else 0
                
                return {
                    'state': state['state'],
                    'failure_rate': failure_rate,
                    'total_calls': state['total'],
                    'failed_calls': state['failures'],
                    'opened_at': state['opened_at'],
                }
            else:
                # 返回所有端点统计
                result = {}
                for ep, state in self.states.items():
                    failure_rate = state['failures'] / state['total'] if state['total'] > 0 else 0
                    result[ep] = {
                        'state': state['state'],
                        'failure_rate': failure_rate,
                        'total_calls': state['total'],
                        'failed_calls': state['failures'],
                    }
                return result

class AuditLogger:
    """审计日志记录器"""
    
    def __init__(self):
        self.logs = []
        self.max_logs = 1000
        self.lock = threading.Lock()
    
    def log(self, level: str, message: str, details: Dict[str, Any] = None) -> None:
        """记录审计日志"""
        with self.lock:
            log_entry = {
                'timestamp': datetime.now().isoformat(),
                'level': level,
                'message': message,
                'details': details or {},
            }
            
            self.logs.append(log_entry)
            
            # 限制日志数量
            if len(self.logs) > self.max_logs:
                self.logs = self.logs[-self.max_logs:]
            
            # 根据级别记录到不同地方
            if level == 'ERROR':
                logger.error(f"审计错误: {message}")
                # TODO: 发送告警通知
            elif level == 'WARNING':
                logger.warning(f"审计警告: {message}")
    
    def get_logs(self, level: str = None, limit: int = 100) -> list:
        """获取审计日志"""
        with self.lock:
            filtered = self.logs
            if level:
                filtered = [log for log in filtered if log['level'] == level]
            
            return filtered[-limit:]

# 全局风控管理器
class RateLimitManager:
    """四层风控管理器"""
    
    def __init__(self):
        self.user_limiter = RateLimiter()
        self.concurrent_controller = ConcurrentController()
        self.circuit_breaker = CircuitBreaker()
        self.audit_logger = AuditLogger()
    
    def check_all_limits(self, user_id: int, endpoint: str) -> Tuple[bool, Optional[str]]:
        """检查所有限制"""
        # 1. 检查用户限流
        allowed, reason = self.user_limiter.check_user_limit(user_id)
        if not allowed:
            self.audit_logger.log('WARNING', f"用户限流阻止: {reason}", {'user_id': user_id, 'endpoint': endpoint})
            return False, reason
        
        # 2. 检查并发控制
        allowed, reason = self.concurrent_controller.acquire()
        if not allowed:
            self.audit_logger.log('WARNING', f"并发控制阻止: {reason}", {'user_id': user_id, 'endpoint': endpoint})
            return False, reason
        
        # 3. 检查熔断保护
        allowed, reason = self.circuit_breaker.is_allowed(endpoint)
        if not allowed:
            self.audit_logger.log('WARNING', f"熔断保护阻止: {reason}", {'user_id': user_id, 'endpoint': endpoint})
            self.concurrent_controller.release()  # 释放并发许可
            return False, reason
        
        return True, None
    
    def record_api_result(self, endpoint: str, success: bool, response_time: int = None) -> None:
        """记录API调用结果"""
        # 记录到熔断器
        self.circuit_breaker.record_result(endpoint, success)
        
        # 安全释放并发许可
        self.concurrent_controller.release_safe()
        
        # 记录审计日志
        level = 'ERROR' if not success else 'INFO'
        message = f"API调用{'成功' if success else '失败'}: {endpoint}"
        details = {'endpoint': endpoint, 'success': success}
        if response_time is not None:
            details['response_time'] = response_time
        
        self.audit_logger.log(level, message, details)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取所有统计信息"""
        return {
            'user_limits': self.user_limiter.get_stats(0),  # 示例用户ID
            'concurrent': self.concurrent_controller.get_stats(),
            'circuit_breaker': self.circuit_breaker.get_stats(),
            'audit_logs': len(self.audit_logger.logs),
        }

# 全局单例
_rate_limit_manager = None

def get_rate_limit_manager() -> RateLimitManager:
    """获取风控管理器单例"""
    global _rate_limit_manager
    if _rate_limit_manager is None:
        _rate_limit_manager = RateLimitManager()
    return _rate_limit_manager


def reload_rate_limits() -> None:
    """保存风控配置后调用：重载内存配置并重建单例，新配置立即生效。

    重建会清空内存中的限流计数与熔断状态——保存配置时触发，可接受。
    """
    global _rate_limit_manager
    from ..config import reload_config
    reload_config()
    _rate_limit_manager = None

if __name__ == "__main__":
    # 测试风控机制
    manager = RateLimitManager()
    
    print("风控管理器测试")
    print("1. 用户限流测试...")
    for i in range(5):
        allowed, reason = manager.user_limiter.check_user_limit(1)
        print(f"   请求 {i+1}: {'允许' if allowed else '拒绝'} - {reason}")
    
    print("\n2. 并发控制测试...")
    for i in range(15):
        allowed, reason = manager.concurrent_controller.acquire()
        print(f"   并发请求 {i+1}: {'允许' if allowed else '拒绝'} - {reason}")
        if allowed:
            manager.concurrent_controller.release()
    
    print("\n3. 熔断保护测试...")
    endpoint = "test.endpoint"
    for i in range(10):
        manager.circuit_breaker.record_result(endpoint, i < 3)  # 前3次成功，后7次失败
        allowed, reason = manager.circuit_breaker.is_allowed(endpoint)
        print(f"   调用 {i+1}: {'允许' if allowed else '拒绝'} - {reason}")
    
    print("\n4. 完整检查测试...")
    allowed, reason = manager.check_all_limits(1, endpoint)
    print(f"   完整检查: {'允许' if allowed else '拒绝'} - {reason}")
    
    if allowed:
        manager.record_api_result(endpoint, True, 100)
    
    print("\n5. 统计信息:")
    stats = manager.get_stats()
    import pprint
    pprint.pprint(stats)
