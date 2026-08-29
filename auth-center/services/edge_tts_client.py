#!/usr/bin/env python3
"""Edge TTS Client — Microsoft Edge free TTS via WebSocket

No subscription key required. Uses the same neural voices as Azure TTS
via Microsoft Edge's public WebSocket endpoint.

Dependency: pip install edge-tts
Library:    https://github.com/rany2/edge-tts
"""

import asyncio
import concurrent.futures
import logging
from i18n import _

logger = logging.getLogger(__name__)


def _run_async(coro):
    """安全地运行异步协程，兼容已有事件循环的环境"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    else:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=60)


class EdgeTTSClient:
    """Microsoft Edge TTS client — free, no API key needed.

    Communicates with Microsoft Edge's TTS WebSocket endpoint.
    Same neural voice quality as Azure paid service.
    """

    # Default voices keyed by locale code
    DEFAULT_VOICES = {
        'zh-CN': 'zh-CN-XiaoxiaoNeural',
        'en-US': 'en-US-AriaNeural',
        'en-GB': 'en-GB-SoniaNeural',
        'fr-FR': 'fr-FR-DeniseNeural',
        'ja-JP': 'ja-JP-NanamiNeural',
        'ko-KR': 'ko-KR-SunHiNeural',
        'de-DE': 'de-DE-KatjaNeural',
        'es-ES': 'es-ES-ElviraNeural',
        'pt-BR': 'pt-BR-FranciscaNeural',
    }

    def __init__(self):
        """Initialize Edge TTS client. No authentication required."""
        pass

    def synthesize(self, text: str, voice_name: str = 'zh-CN-XiaoxiaoNeural',
                   output_path: str = '') -> dict:
        """Synthesize speech from text via Edge TTS WebSocket.

        Args:
            text: Plain text to synthesize.
            voice_name: Neural voice name, e.g. 'zh-CN-XiaoxiaoNeural'.
            output_path: If provided, also save audio to this file path.

        Returns:
            dict with keys:
                success (bool): Whether synthesis succeeded.
                audio_bytes (bytes): Raw audio data on success.
                file_path (str): File path if output_path was provided.
                error (str): Error message on failure.
        """
        try:
            audio_bytes = _run_async(
                self._synthesize_async(text, voice_name)
            )
            if not audio_bytes:
                return {
                    'success': False,
                    'error': _('Edge TTS returned empty audio')
                }
            if output_path:
                with open(output_path, 'wb') as f:
                    f.write(audio_bytes)
                return {
                    'success': True,
                    'file_path': output_path,
                    'audio_bytes': audio_bytes,
                }
            return {
                'success': True,
                'file_path': '',
                'audio_bytes': audio_bytes,
            }
        except ImportError:
            logger.error(
                'edge-tts package not installed. Run: pip install edge-tts'
            )
            return {
                'success': False,
                'error': _('edge-tts package not installed')
            }
        except Exception as e:
            logger.error('Edge TTS synthesis failed: %s', e)
            return {'success': False, 'error': str(e)}

    async def _synthesize_async(self, text: str, voice_name: str) -> bytes:
        """Internal async synthesis via edge-tts library.

        Args:
            text: Text to synthesize.
            voice_name: Neural voice name.

        Returns:
            Raw audio bytes (MP3 format).
        """
        import edge_tts

        communicate = edge_tts.Communicate(text, voice_name)
        audio_chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.append(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                # Word boundary events — ignored for now
                pass

        return b''.join(audio_chunks)

    def list_voices(self, locale: str = '') -> list:
        """Fetch available Edge TTS voices.

        Args:
            locale: Filter by locale code (e.g. 'zh-CN', 'en-US').
                    Empty string returns all voices.

        Returns:
            List of voice dicts with Name, Gender, Locale, etc.
        """
        try:
            voices = _run_async(self._list_voices_async())
            if locale:
                voices = [
                    v for v in voices
                    if locale.lower() in v.get('Locale', '').lower()
                ]
            return voices
        except ImportError:
            logger.error('edge-tts package not installed')
            return []
        except Exception as e:
            logger.error('Edge TTS list voices failed: %s', e)
            return []

    async def _list_voices_async(self) -> list:
        """Internal async voice list fetch."""
        import edge_tts

        voices = await edge_tts.list_voices()
        result = []
        for v in voices:
            result.append({
                'Name': v.get('Name', ''),
                'ShortName': v.get('ShortName', ''),
                'Gender': v.get('Gender', ''),
                'Locale': v.get('Locale', ''),
                'FriendlyName': v.get('FriendlyName', ''),
                'Status': v.get('Status', ''),
            })
        return result
