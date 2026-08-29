"""RavoRun AI Edge-TTS 语音合成服务

基于 Microsoft Edge TTS (免费，无需 API Key)
服务器已验证: Python 3.12.3 + edge-tts 7.2.8 + 网络正常
"""

import asyncio
import os
import tempfile
import logging
from typing import Optional
import edge_tts

logger = logging.getLogger(__name__)

# 声音映射: 场景 -> 推荐语音
VOICE_PRESETS = {
    "female":     "zh-CN-XiaoxiaoNeural",   # 温柔女声，默认
    "male":       "zh-CN-YunxiNeural",      # 稳重男声
    "news":       "zh-CN-YunyangNeural",    # 新闻播报
    "education":  "zh-CN-YunjianNeural",   # 教育培训
    "lively":     "zh-CN-XiaoyiNeural",     # 活泼女声
    "english":    "en-US-JennyNeural",      # 英语女声
    "english_male":"en-US-GuyNeural",         # 英语男声
}

async def text_to_speech(
    text: str,
    voice: str = "zh-CN-XiaoxiaoNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
    output_file: Optional[str] = None
) -> str:
    """
    文字转语音核心函数

    Args:
        text: 要合成的文本内容
        voice: 语音名称 (默认 zh-CN-XiaoxiaoNeural)
        rate: 语速 -50% 到 +100%
        pitch: 音调 -20Hz 到 +20Hz
        output_file: 输出文件路径 (默认生成临时文件)

    Returns:
        mp3 文件路径
    """
    if not text or not text.strip():
        raise ValueError("Text cannot be empty")

    if output_file is None:
        fd, output_file = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice=voice,
            rate=rate,
            pitch=pitch
        )
        await communicate.save(output_file)

        file_size = os.path.getsize(output_file)
        logger.info(
            f"TTS generated: {len(text)} chars -> {file_size} bytes "
            f"[voice={voice}, rate={rate}]"
        )
        return output_file

    except Exception as e:
        logger.error(f"TTS failed: {e}")
        raise


async def text_to_speech_bytes(
    text: str,
    voice: str = "zh-CN-XiaoxiaoNeural",
    rate: str = "+0%",
) -> bytes:
    """
    文字转语音 -> 直接返回字节流 (适合 HTTP 响应)
    """
    import io
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)

    buffer = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.write(chunk["data"])

    return buffer.getvalue()


async def list_available_voices(locale: str = "zh-CN"):
    """列出可用的语音"""
    voices = await edge_tts.list_voices()
    return [
        {
            "name": v["ShortName"],
            "gender": v["Gender"],
            "locale": v["Locale"],
        }
        for v in voices
        if v["Locale"].startswith(locale)
    ]


def tts_sync(text: str, voice: str = "zh-CN-XiaoxiaoNeural", output_file: str = None) -> str:
    """同步包装器, 方便在同步代码中调用"""
    return asyncio.run(text_to_speech(text, voice, output_file=output_file))
