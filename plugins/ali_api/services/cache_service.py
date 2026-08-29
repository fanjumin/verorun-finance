#!/usr/bin/env python3
"""
阿里巴巴API缓存服务

支持：
1. Redis缓存（如果可用）
2. 内存缓存（备用）
3. 商品数据缓存
4. API响应缓存
"""

import json
import time
import hashlib
from typing import Any, Optional, Dict, Tuple
import logging

from ..config import config

logger = logging.getLogger(__name__)


def _cache_key_hash(raw: str) -> str:
    """生成稳定、跨进程一致的缓存键（替代内建 hash()，避免 PYTHONHASHSEED 随机化导致缓存失效）"""
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

class MemoryCache:
    """内存缓存实现"""
    
    def __init__(self, maxsize: int = None, ttl: int = None):
        self.maxsize = maxsize or config['cache']['memory_cache_maxsize']
        self.ttl = ttl or config['cache']['memory_cache_ttl']
        self.cache = {}
        self.timestamps = {}
    
    def get(self, key: str) -> Tuple[bool, Optional[Any]]:
        """获取缓存值"""
        if key not in self.cache:
            return False, None
        
        # 检查是否过期
        if time.time() - self.timestamps[key] > self.ttl:
            self.delete(key)
            return False, None
        
        return True, self.cache[key]
    
    def set(self, key: str, value: Any, ttl: int = None) -> None:
        """设置缓存值"""
        # 检查缓存大小
        if len(self.cache) >= self.maxsize:
            # 删除最旧的条目
            oldest_key = min(self.timestamps.items(), key=lambda x: x[1])[0]
            self.delete(oldest_key)
        
        self.cache[key] = value
        self.timestamps[key] = time.time()
    
    def delete(self, key: str) -> None:
        """删除缓存值"""
        if key in self.cache:
            del self.cache[key]
            del self.timestamps[key]
    
    def clear(self) -> None:
        """清空缓存"""
        self.cache.clear()
        self.timestamps.clear()
    
    def stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        now = time.time()
        expired = sum(1 for ts in self.timestamps.values() if now - ts > self.ttl)
        
        return {
            'size': len(self.cache),
            'maxsize': self.maxsize,
            'ttl': self.ttl,
            'expired_entries': expired,
        }

class RedisCache:
    """Redis缓存实现"""
    
    def __init__(self):
        self.client = None
        self.connected = False
        self._connect()
    
    def _connect(self) -> None:
        """连接Redis"""
        try:
            import redis
            redis_config = config['cache']
            
            self.client = redis.Redis(
                host=redis_config['redis_host'],
                port=redis_config['redis_port'],
                db=redis_config['redis_db'],
                password=redis_config['redis_password'] or None,
                socket_timeout=5,
                socket_connect_timeout=5,
                decode_responses=True,
            )
            
            # 测试连接
            self.client.ping()
            self.connected = True
            logger.info("Redis缓存连接成功")
            
        except ImportError:
            logger.warning("Redis模块未安装，使用内存缓存")
            self.connected = False
        except Exception as e:
            logger.warning(f"Redis连接失败: {e}，使用内存缓存")
            self.connected = False
    
    def get(self, key: str) -> Tuple[bool, Optional[Any]]:
        """获取缓存值"""
        if not self.connected:
            return False, None
        
        try:
            value = self.client.get(key)
            if value is None:
                return False, None
            
            # 尝试解析JSON
            try:
                return True, json.loads(value)
            except json.JSONDecodeError:
                # 如果不是JSON，返回原始值
                return True, value
                
        except Exception as e:
            logger.error(f"Redis获取失败: {e}")
            return False, None
    
    def set(self, key: str, value: Any, ttl: int = None) -> bool:
        """设置缓存值"""
        if not self.connected:
            return False
        
        try:
            # 序列化值
            if isinstance(value, (dict, list, tuple)):
                serialized = json.dumps(value, ensure_ascii=False)
            else:
                serialized = str(value)
            
            if ttl:
                self.client.setex(key, ttl, serialized)
            else:
                self.client.set(key, serialized)
            
            return True
        except Exception as e:
            logger.error(f"Redis设置失败: {e}")
            return False
    
    def delete(self, key: str) -> bool:
        """删除缓存值"""
        if not self.connected:
            return False
        
        try:
            return bool(self.client.delete(key))
        except Exception as e:
            logger.error(f"Redis删除失败: {e}")
            return False
    
    def clear_pattern(self, pattern: str) -> int:
        """清除匹配模式的键"""
        if not self.connected:
            return 0
        
        try:
            keys = self.client.keys(pattern)
            if keys:
                return self.client.delete(*keys)
            return 0
        except Exception as e:
            logger.error(f"Redis清除模式失败: {e}")
            return 0
    
    def stats(self) -> Dict[str, Any]:
        """获取Redis统计"""
        if not self.connected:
            return {'connected': False}
        
        try:
            info = self.client.info()
            return {
                'connected': True,
                'used_memory': info.get('used_memory_human', 'N/A'),
                'connected_clients': info.get('connected_clients', 0),
                'total_commands_processed': info.get('total_commands_processed', 0),
            }
        except Exception as e:
            logger.error(f"获取Redis统计失败: {e}")
            return {'connected': False, 'error': str(e)}

class CacheService:
    """缓存服务（Redis + 内存回退）"""
    
    def __init__(self):
        self.redis = RedisCache()
        self.memory = MemoryCache()
        self.use_redis = self.redis.connected
    
    def get(self, key: str) -> Tuple[bool, Optional[Any]]:
        """获取缓存值（优先Redis）"""
        # 先尝试Redis
        if self.use_redis:
            found, value = self.redis.get(key)
            if found:
                # 同时更新内存缓存
                self.memory.set(key, value)
                return True, value
        
        # 再尝试内存缓存
        found, value = self.memory.get(key)
        return found, value
    
    def set(self, key: str, value: Any, ttl: int = None) -> bool:
        """设置缓存值（双写）"""
        success = True
        
        # 写入Redis
        if self.use_redis:
            if not self.redis.set(key, value, ttl):
                success = False
        
        # 写入内存
        self.memory.set(key, value, ttl)
        
        return success
    
    def delete(self, key: str) -> bool:
        """删除缓存值"""
        success = True
        
        if self.use_redis:
            if not self.redis.delete(key):
                success = False
        
        self.memory.delete(key)
        
        return success
    
    def clear_product_cache(self, product_id: str = None) -> int:
        """清除商品缓存"""
        if product_id:
            # 清除特定商品
            pattern = f"product:{product_id}:*"
        else:
            # 清除所有商品缓存
            pattern = "product:*"
        
        deleted = 0
        if self.use_redis:
            deleted = self.redis.clear_pattern(pattern)
        
        # 内存缓存需要遍历
        keys_to_delete = []
        for key in list(self.memory.cache.keys()):
            if product_id:
                if key.startswith(f"product:{product_id}:"):
                    keys_to_delete.append(key)
            else:
                if key.startswith("product:"):
                    keys_to_delete.append(key)
        
        for key in keys_to_delete:
            self.memory.delete(key)
        
        return deleted + len(keys_to_delete)
    
    def get_product(self, product_id: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """获取商品缓存"""
        key = f"product:{product_id}:data"
        return self.get(key)
    
    def set_product(self, product_id: str, product_data: Dict[str, Any]) -> bool:
        """设置商品缓存"""
        key = f"product:{product_id}:data"
        ttl = config['cache']['product_cache_ttl']
        return self.set(key, product_data, ttl)
    
    def get_category(self, category_id: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """获取类目缓存"""
        key = f"category:{category_id}:data"
        return self.get(key)
    
    def set_category(self, category_id: str, category_data: Dict[str, Any]) -> bool:
        """设置类目缓存"""
        key = f"category:{category_id}:data"
        ttl = config['cache']['category_cache_ttl']
        return self.set(key, category_data, ttl)
    
    def get_api_response(self, endpoint: str, params: Dict[str, Any]) -> Tuple[bool, Optional[Any]]:
        """获取API响应缓存"""
        # 生成缓存键
        param_str = json.dumps(params, sort_keys=True)
        key = f"api:{endpoint}:{_cache_key_hash(param_str)}"
        return self.get(key)
    
    def set_api_response(self, endpoint: str, params: Dict[str, Any], response: Any) -> bool:
        """设置API响应缓存"""
        # 生成缓存键
        param_str = json.dumps(params, sort_keys=True)
        key = f"api:{endpoint}:{_cache_key_hash(param_str)}"
        
        # API响应缓存时间较短
        ttl = 300  # 5分钟
        return self.set(key, response, ttl)
    
    def stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        redis_stats = self.redis.stats()
        memory_stats = self.memory.stats()
        
        return {
            'redis': redis_stats,
            'memory': memory_stats,
            'use_redis': self.use_redis,
        }

# 缓存装饰器
def cached(ttl: int = None, key_prefix: str = ""):
    """缓存装饰器"""
    def decorator(func):
        cache_service = CacheService()
        
        def wrapper(*args, **kwargs):
            # 生成缓存键
            if key_prefix:
                cache_key = f"{key_prefix}:{func.__name__}"
            else:
                cache_key = f"{func.__module__}:{func.__name__}"
            
            # 添加参数到缓存键
            if args:
                cache_key += f":{_cache_key_hash(str(args))}"
            if kwargs:
                cache_key += f":{_cache_key_hash(str(sorted(kwargs.items())))}"
            
            # 尝试获取缓存
            found, cached_value = cache_service.get(cache_key)
            if found:
                logger.debug(f"缓存命中: {cache_key}")
                return cached_value
            
            # 执行函数
            result = func(*args, **kwargs)
            
            # 缓存结果
            cache_service.set(cache_key, result, ttl)
            logger.debug(f"缓存设置: {cache_key}")
            
            return result
        
        return wrapper
    return decorator

# 全局缓存服务实例
_cache_service = None

def get_cache_service() -> CacheService:
    """获取缓存服务单例"""
    global _cache_service
    if _cache_service is None:
        _cache_service = CacheService()
    return _cache_service

if __name__ == "__main__":
    # 测试缓存服务
    import pprint
    
    cache = CacheService()
    print("缓存服务测试:")
    
    # 测试基本缓存
    print("1. 基本缓存测试...")
    cache.set("test:key", {"name": "测试", "value": 123}, 10)
    found, value = cache.get("test:key")
    print(f"   获取缓存: {'成功' if found else '失败'} - {value}")
    
    # 测试商品缓存
    print("\n2. 商品缓存测试...")
    product_data = {
        "product_id": "12345",
        "title": "测试商品",
        "price": 99.99,
    }
    cache.set_product("12345", product_data)
    found, cached_product = cache.get_product("12345")
    print(f"   获取商品缓存: {'成功' if found else '失败'} - {cached_product}")
    
    # 测试API响应缓存
    print("\n3. API响应缓存测试...")
    params = {"keyword": "手机", "page": 1}
    response = {"success": True, "data": []}
    cache.set_api_response("product.search", params, response)
    found, cached_response = cache.get_api_response("product.search", params)
    print(f"   获取API缓存: {'成功' if found else '失败'} - {cached_response}")
    
    # 测试缓存装饰器
    print("\n4. 缓存装饰器测试...")
    
    @cached(ttl=10, key_prefix="test")
    def expensive_operation(x, y):
        print(f"   执行昂贵操作: {x} + {y}")
        return x + y
    
    result1 = expensive_operation(10, 20)
    print(f"   第一次调用结果: {result1}")
    
    result2 = expensive_operation(10, 20)
    print(f"   第二次调用结果: {result2} (应该来自缓存)")
    
    print("\n5. 缓存统计:")
    stats = cache.stats()
    pprint.pprint(stats)
