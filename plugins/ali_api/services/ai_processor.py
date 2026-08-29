#!/usr/bin/env python3
"""
阿里巴巴商品AI内容生成器
功能：1. 商品标题优化
2. 营销文案重写
3. 集成 agent_matrix 引擎
4. 支持多种AI供应商（DeepSeek/OpenAI/本地模型）
"""

import json
import logging
from typing import Dict, Any, Optional, Tuple
import sys
import os

# 添加agent_matrix路径
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'agent_matrix'))

from ..config import config

logger = logging.getLogger(__name__)

class AIProcessor:
    """AI内容处理器"""
    
    _MODEL_POLICY = {
        'strategy': 'tier',
        'tier': 'standard',
        'allow_user_override': True,
        'fallback': 'inherit',
    }
    _SYSTEM_PROMPT = '你是一个专业的电商文案优化专家，擅长优化商品标题和描述，使其更具吸引力和营销力。'

    def __init__(self):
        """初始化AI处理器（懒连接统一网关）"""
        self.engine = None
        self._gw = None
        self._model_kwargs = {}
        self._init_engine()
    
    def _init_engine(self) -> None:
        """初始化统一 AI 网关（agent_matrix model_policy tier/standard，参考 content_factory 标准）"""
        try:
            from agent_matrix.engine import get_gateway
            from agent_matrix.model_resolver import resolve_model_args
            self._gw = get_gateway()
            self._model_kwargs = resolve_model_args(self._MODEL_POLICY)
            self.engine = self._gw
            logger.info(f"AI统一网关就绪，模型参数: {self._model_kwargs}")
        except ImportError as e:
            logger.error(f"无法导入agent_matrix: {e}")
            self.engine = None
        except Exception as e:
            logger.error(f"AI统一网关初始化失败: {e}")
            self.engine = None
    
    def _call_ai(self, prompt: str, max_tokens: int = None, temperature: float = None) -> Tuple[bool, Optional[str]]:
        """调用统一 AI 网关"""
        if not self.engine or self._gw is None:
            return False, "AI引擎未初始化"
        
        try:
            # 准备消息
            messages = [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
            
            # 走统一网关（model_policy 每次调用解析，支持配置热更新）
            from agent_matrix.model_resolver import resolve_model_args
            kwargs = resolve_model_args(self._MODEL_POLICY)
            text = self._gw.chat(
                messages=messages,
                temperature=temperature if temperature is not None else config['ai']['temperature'],
                max_tokens=max_tokens or config['ai']['max_tokens'],
                module='ali_api',
                **kwargs,
            )
            
            if text and text.strip():
                return True, text.strip()
            else:
                return False, "AI返回空内容"
                
        except Exception as e:
            logger.error(f"AI调用异常: {e}")
            return False, f"AI调用失败: {e}"
    
    def optimize_title(self, original_title: str, category: str = None, keywords: str = None) -> Tuple[bool, Optional[str]]:
        """优化商品标题"""
        if not original_title or len(original_title.strip()) == 0:
            return False, "原始标题不能为空"
        
        # 构建提示词
        prompt = config['ai']['title_prompt'].format(title=original_title)
        
        if category:
            prompt += f"\n商品类目: {category}"
        
        if keywords:
            prompt += f"\n关键词: {keywords}"
        
        prompt += "\n请输出优化后的标题，不要添加任何解释。"
        
        # 调用AI
        success, optimized = self._call_ai(prompt, max_tokens=config['ai']['title_max_length'])
        
        if success and optimized:
            # 清理结果
            optimized = optimized.strip().strip('"').strip("'")
            
            # 检查长度
            if len(optimized) > config['ai']['title_max_length']:
                optimized = optimized[:config['ai']['title_max_length']] + "..."
            
            return True, optimized
        
        return success, optimized
    
    def generate_title_options(self, product_info: Dict[str, Any]) -> Tuple[bool, Optional[list]]:
        """
        生成多版本标题选项（专业型、吸引力型、简洁型）
        返回: [(id, title, style, reason), ...]
        """
        original_title = product_info.get('title', '')
        description = product_info.get('description', '')
        specs = product_info.get('specs', {})
        category = product_info.get('category', '')
        
        if not original_title:
            return False, "原始标题不能为空"
        
        specs_text = json.dumps(specs, ensure_ascii=False, indent=2) if specs else '无'
        
        prompt = f"""你是一个电商标题优化专家。请根据以下1688商品信息，生成 3 个优化后的商品标题。
原始标题：{original_title}
商品描述：{description[:200] if description else '无'}
商品规格：{specs_text}
商品类目：{category}

要求：
1. 标题长度 20-40 字
2. 包含核心卖点和关键词
3. SEO 友好，适合搜索
4. 符合中国电商平台规范，不包含违规词
5. 3 个标题风格不同：①专业型 ②吸引力型 ③简洁型

请以 JSON 格式返回，不要包含任何其他文本：
[{{"id":1,"title":"标题1","style":"professional","reason":"选择理由..."}}, ...]"""
        
        success, response = self._call_ai(prompt, max_tokens=800, temperature=0.8)
        
        if not success:
            return False, response
        
        # 尝试从AI响应中提取JSON
        try:
            # 先尝试直接解析
            options = json.loads(response)
        except json.JSONDecodeError:
            # 尝试从markdown代码块中提取
            import re
            json_match = re.search(r'```(?:json)?\s*(\[[\s\S]*?\])\s*```', response)
            if json_match:
                try:
                    options = json.loads(json_match.group(1))
                except json.JSONDecodeError:
                    return False, "AI返回的JSON格式无效"
            else:
                return False, "AI返回的JSON格式无效"
        
        if not isinstance(options, list) or len(options) == 0:
            return False, "AI未生成有效的标题选项"
        
        # 规范化输出
        result = []
        for opt in options:
            result.append({
                'id': opt.get('id', len(result) + 1),
                'title': opt.get('title', ''),
                'style': opt.get('style', 'normal'),
                'reason': opt.get('reason', ''),
            })
        
        return True, result
    
    def optimize_description(self, original_description: str, product_features: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """优化商品描述"""
        if not original_description or len(original_description.strip()) == 0:
            return False, "原始描述不能为空"
        
        # 构建提示词
        prompt = config['ai']['description_prompt'].format(description=original_description)
        
        if product_features:
            features_text = json.dumps(product_features, ensure_ascii=False)
            prompt += f"\n商品特征: {features_text}"
        
        prompt += "\n请输出优化后的描述，突出产品卖点，适合电商平台展示。"
        
        # 调用AI
        success, optimized = self._call_ai(prompt)
        
        if success and optimized:
            # 清理结果
            optimized = optimized.strip()
            
            # 检查长度
            max_length = config['ai']['description_max_length']
            if len(optimized) > max_length:
                optimized = optimized[:max_length] + "..."
            
            return True, optimized
        
        return success, optimized
    
    def generate_marketing_copy(self, product_info: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """生成营销文案（标题+描述+卖点）"""
        result = {
            'original_title': product_info.get('title', ''),
            'original_description': product_info.get('description', ''),
            'optimized_title': '',
            'optimized_description': '',
            'selling_points': [],
            'tags': [],
        }
        
        # 优化标题
        if product_info.get('title'):
            success, optimized_title = self.optimize_title(
                product_info['title'],
                product_info.get('category'),
                product_info.get('keywords')
            )
            if success:
                result['optimized_title'] = optimized_title
        
        # 优化描述
        if product_info.get('description'):
            success, optimized_description = self.optimize_description(
                product_info['description'],
                product_info.get('specs', {})
            )
            if success:
                result['optimized_description'] = optimized_description
        
        # 生成卖点
        if product_info.get('specs') or product_info.get('features'):
            success, selling_points = self._generate_selling_points(product_info)
            if success:
                result['selling_points'] = selling_points
        
        # 生成标签
        if product_info.get('title') or product_info.get('category'):
            success, tags = self._generate_tags(product_info)
            if success:
                result['tags'] = tags
        
        return True, result
    
    def _generate_selling_points(self, product_info: Dict[str, Any]) -> Tuple[bool, Optional[list]]:
        """生成商品卖点"""
        prompt = f"请为以下商品生成3-5个核心卖点：\n"
        prompt += f"商品名称: {product_info.get('title', '')}\n"
        
        if product_info.get('description'):
            prompt += f"商品描述: {product_info.get('description', '')}\n"
        
        if product_info.get('specs'):
            specs_text = json.dumps(product_info['specs'], ensure_ascii=False)
            prompt += f"商品规格: {specs_text}\n"
        
        prompt += "请以列表形式输出卖点，每个卖点用短句描述，不要编号。"
        
        success, response = self._call_ai(prompt, max_tokens=300)
        
        if success:
            # 解析卖点列表
            selling_points = []
            lines = response.strip().split('\n')
            
            for line in lines:
                line = line.strip()
                if line and not line.startswith(('1.', '2.', '3.', '4.', '5.', '-', '*', '•')):
                    # 移除常见前缀
                    for prefix in ['- ', '* ', '•', '· ']:
                        if line.startswith(prefix):
                            line = line[len(prefix):]
                            break
                    selling_points.append(line)
                elif line and line.startswith(('- ', '* ', '•', '· ')):
                    selling_points.append(line[2:].strip())
            
            # 限制数量
            selling_points = selling_points[:5]
            
            return True, selling_points
        
        return False, []
    
    def _generate_tags(self, product_info: Dict[str, Any]) -> Tuple[bool, Optional[list]]:
        """生成商品标签"""
        prompt = f"请为以下商品生成5-8个相关标签：\n"
        prompt += f"商品名称: {product_info.get('title', '')}\n"
        
        if product_info.get('category'):
            prompt += f"商品类目: {product_info.get('category', '')}\n"
        
        if product_info.get('description'):
            # 只取描述的前100字
            desc = product_info['description'][:100] + "..." if len(product_info['description']) > 100 else product_info['description']
            prompt += f"商品描述: {desc}\n"
        
        prompt += "请输出标签，用逗号分隔，不要添加任何解释。"
        
        success, response = self._call_ai(prompt, max_tokens=100)
        
        if success:
            # 解析标签
            tags = []
            for tag in response.strip().split(','):
                tag = tag.strip()
                if tag:
                    tags.append(tag)
            
            # 限制数量
            tags = tags[:8]
            
            return True, tags
        
        return False, []
    
    def batch_process(self, products: list) -> Dict[str, Any]:
        """批量处理商品"""
        results = {
            'total': len(products),
            'processed': 0,
            'success': 0,
            'failed': 0,
            'results': [],
        }
        
        for i, product in enumerate(products):
            try:
                logger.info(f"处理商品 {i+1}/{len(products)}: {product.get('title', '未知')}")
                
                success, result = self.generate_marketing_copy(product)
                
                if success:
                    results['success'] += 1
                    results['results'].append({
                        'product_id': product.get('product_id', f'unknown_{i}'),
                        'success': True,
                        'result': result,
                    })
                else:
                    results['failed'] += 1
                    results['results'].append({
                        'product_id': product.get('product_id', f'unknown_{i}'),
                        'success': False,
                        'error': 'AI处理失败',
                    })
                
                results['processed'] += 1
                
            except Exception as e:
                logger.error(f"处理商品失败: {e}")
                results['failed'] += 1
                results['results'].append({
                    'product_id': product.get('product_id', f'unknown_{i}'),
                    'success': False,
                    'error': str(e),
                })
                results['processed'] += 1
        
        return results

# 全局AI处理器实例
_ai_processor = None

def get_ai_processor() -> AIProcessor:
    """获取AI处理器单例"""
    global _ai_processor
    if _ai_processor is None:
        _ai_processor = AIProcessor()
    return _ai_processor

def get_ai_status() -> dict:
    """从统一网关解析 AI 状态（provider/model 名称），不依赖插件自身 AI 配置。"""
    try:
        from agent_matrix.engine import get_gateway
        from agent_matrix.model_resolver import resolve_model_args
        kwargs = resolve_model_args(AIProcessor._MODEL_POLICY)
        cfg = get_gateway().resolve_model(**kwargs)
        return {'available': bool(cfg.get('api_key')),
                'provider': cfg.get('provider'), 'model': cfg.get('model')}
    except Exception as e:
        logger.warning(f"获取AI网关状态失败: {e}")
        return {'available': False, 'provider': None, 'model': None}


def is_ai_available() -> bool:
    """检查统一 AI 网关是否可用"""
    return get_ai_status().get('available', False)

if __name__ == "__main__":
    # 测试AI处理器
    import pprint
    
    print("AI处理器测试")
    
    processor = AIProcessor()
    
    if not processor.engine:
        print("AI引擎初始化失败，请检查配置")
    else:
        print(f"AI引擎初始化成功（统一网关）: {get_ai_status()}")
        
        # 测试标题优化
        print("\n1. 标题优化测试...")
        original_title = "2024新款智能手机 6.7英寸大屏 5000mAh电池 128GB存储"
        success, optimized = processor.optimize_title(original_title, "手机数码", "智能手机 大屏 长续航")
        print(f"   原始标题: {original_title}")
        print(f"   优化结果: {'成功' if success else '失败'} - {optimized}")
        
        # 测试描述优化
        print("\n2. 描述优化测试...")
        original_desc = "这是一款新款智能手机，屏幕大，电池容量大，存储空间大。"
        success, optimized_desc = processor.optimize_description(original_desc)
        print(f"   原始描述: {original_desc}")
        print(f"   优化结果: {'成功' if success else '失败'} - {optimized_desc[:50]}...")
        
        # 测试营销文案生成
        print("\n3. 营销文案生成测试...")
        product_info = {
            'title': "无线蓝牙耳机 降噪 运动",
            'description': "无线蓝牙耳机，支持降噪，适合运动使用。",
            'category': "数码配件",
            'specs': {"蓝牙版本": "5.0", "续航": "20小时", "防水等级": "IPX4"},
        }
        success, marketing_copy = processor.generate_marketing_copy(product_info)
        print(f"   生成结果: {'成功' if success else '失败'}")
        if success:
            pprint.pprint(marketing_copy)
        
        print("\n4. AI服务可用性检查")
        print(f"   AI可用: {is_ai_available()}")
