"""
OpenAI兼容LLM客户端（OpenRouter/DeepSeek/Kimi/通义/Gemini等通用）
供应商配置读原项目 config/config.json 的 ai_provider 字段：
    "ai_provider": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-xxxx",
        "model": "厂商/模型ID",
        "fallback_models": [],
        "timeout": 180
    }
API key 只存本地该文件（已在.gitignore）或环境变量，不进代码、不进git。
"""

import logging
import os
import time
from typing import Dict, List, Optional

import requests

from ..utils.app_config import load_config

logger = logging.getLogger(__name__)

_DEFAULTS = {
    'base_url': 'https://openrouter.ai/api/v1',
    'api_key': '',
    'model': '',
    'fallback_models': [],
    'timeout': 180,
}


def load_ai_provider_config() -> Dict:
    """读取AI供应商配置，缺失字段用默认值补齐（任何异常都降级为未配置状态）
    兼容两种位置：顶层 ai_provider 或 api_keys.ai_provider，并支持环境变量覆盖"""
    try:
        cfg = load_config()
        p = cfg.get('ai_provider') or (cfg.get('api_keys') or {}).get('ai_provider') or {}
        env_key = (
            os.environ.get('AI_API_KEY')
            or os.environ.get('OPENROUTER_API_KEY')
            or os.environ.get('GEMINI_API_KEY')
        )
        env_model = os.environ.get('AI_MODEL') or os.environ.get('GEMINI_MODEL')
        env_base = os.environ.get('AI_BASE_URL')

        api_key = env_key or p.get('api_key') or ''
        model = env_model or p.get('model') or ('gemini-2.5-flash' if os.environ.get('GEMINI_API_KEY') and not p.get('model') else '')
        base_url = (env_base or p.get('base_url') or _DEFAULTS['base_url']).rstrip('/')
        fallback_models = p.get('fallback_models')
        if not isinstance(fallback_models, list):
            fallback_models = []

        return {
            'base_url': base_url,
            'api_key': api_key,
            'model': model,
            'fallback_models': [str(m) for m in fallback_models if m],
            'timeout': int(p.get('timeout', _DEFAULTS['timeout'])),
            'universe': p.get('universe'),   # 可选：自定义候选池 [["NVDA","Nvidia"],...]
        }
    except Exception as e:
        logger.warning(f"读取AI供应商配置失败: {e}")
        return dict(_DEFAULTS)


def is_ai_configured() -> bool:
    c = load_ai_provider_config()
    return bool(c['api_key'] and c['model'])


def chat_completion(messages: List[Dict], temperature: float = 0.4,
                    max_tokens: int = 4000, model: Optional[str] = None) -> str:
    """调用OpenAI兼容的chat/completions接口，返回assistant文本"""
    c = load_ai_provider_config()
    if not (c['api_key'] and c['model']):
        raise RuntimeError(
            'AI供应商未配置：请在 config/config.json 添加 ai_provider 字段'
            '（base_url/api_key/model），参考README说明')

    url = f"{c['base_url']}/chat/completions"
    headers = {
        'Authorization': f"Bearer {c['api_key']}",
        'Content-Type': 'application/json',
    }

    target_model = model or c['model']
    models_to_try = [target_model]

    # 容灾降级梯队：优先使用用户在 config.json 配置的 fallback_models；
    # 若未显式配置且主模型属于 Gemini 系列，则才追加同系轻量备用模型（避免非 Gemini 渠道报 404 模型不存在）
    configured_fallbacks = c.get('fallback_models') or []
    if configured_fallbacks:
        for fb in configured_fallbacks:
            if fb not in models_to_try:
                models_to_try.append(fb)
    elif 'gemini' in target_model.lower():
        for fb in ['gemini-2.5-flash', 'gemini-3.6-flash']:
            if fb not in models_to_try:
                models_to_try.append(fb)

    resp = None
    req_timeout = min(max(int(c.get('timeout', 30)), 10), 180)
    for current_model in models_to_try:
        payload = {
            'model': current_model,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
        }
        for attempt in range(2):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=req_timeout)
                if resp.status_code in (429, 503) and attempt == 0:
                    time.sleep(1.0)
                    continue
                if resp.status_code == 200:
                    break
            except Exception:
                time.sleep(1.0)
        if resp is not None and resp.status_code == 200:
            break

    if resp is None or resp.status_code != 200:
        err_msg = resp.text[:300] if resp is not None else "No response"
        raise RuntimeError(f'LLM API返回{resp.status_code if resp else "None"}: {err_msg}')
    data = resp.json()
    try:
        choice = data['choices'][0]
        msg = choice.get('message') or {}
        content = msg.get('content')
    except (KeyError, IndexError) as e:
        raise RuntimeError(f'LLM响应结构异常: {e} | {str(data)[:300]}')
    if not content:
        # 推理类模型可能把max_tokens耗在reasoning上导致content为空
        raise RuntimeError(
            f"LLM返回空内容(finish_reason={choice.get('finish_reason')})，"
            f"疑似输出token不足，请增大max_tokens后重试")
    return content
