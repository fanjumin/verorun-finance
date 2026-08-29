#!/usr/bin/env python3
"""
AI Audio Interface — 语音输入/输出抽象层
========================================
定义标准接口，当前仅占位，不实现具体功能。
- AudioInputProcessor: 语音识别（ASR），预留 Vosk + 阿里云接口
- AudioOutputProcessor: 语音合成（TTS），预留阿里云接口
"""

from i18n import _
import os
import logging

logger = logging.getLogger(__name__)


class AudioInputProcessor:
    """语音输入处理器（ASR）—— 预留接口，暂不实现"""

    PROVIDERS = {
        'vosk': _('Offline Speech Recognition (vosk-model-small-cn-0.22)'),
        'aliyun_asr': _('Aliyun Real-time Speech Recognition'),
    }

    def __init__(self, provider: str = 'vosk', model_path: str = ''):
        """
        :param provider: ASR 提供商（vosk / aliyun_asr）
        :param model_path: Vosk 模型路径（仅 vosk 需要）
        """
        self.provider = provider
        self.model_path = model_path or os.environ.get('VOSK_MODEL_PATH', '')
        self._initialized = False
        logger.info(f'[AudioInput] 接口已创建（提供商: {provider}），待实现')

    def initialize(self) -> bool:
        """初始化语音识别引擎（需安装对应依赖后实现）"""
        logger.warning('[AudioInput] initialize() 未实现——需要安装 Vosk 或阿里云 SDK')
        return False

    def transcribe(self, audio_data: bytes) -> str:
        """将音频数据转换为文本"""
        logger.warning('[AudioInput] transcribe() 未实现')
        return ''

    def transcribe_file(self, file_path: str) -> str:
        """识别音频文件"""
        logger.warning('[AudioInput] transcribe_file() 未实现')
        return ''

    def start_stream(self):
        """启动实时语音识别流"""
        raise NotImplementedError(_('Real-time Speech Recognition Not Implemented'))

    def stop_stream(self):
        """停止实时语音识别流"""
        raise NotImplementedError


class AudioOutputProcessor:
    """Speech synthesis processor — delegates to provider-specific TTS clients.

    Currently supports Azure Cognitive Services TTS.
    API keys are resolved from provider_api_keys table at runtime.
    """

    PROVIDERS = {
        'edge_tts': 'Microsoft Edge TTS (Free, no key required)',
        'azure_tts': 'Microsoft Azure Neural TTS',
    }

    # Default voices per locale (fallback when not specified by caller)
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

    def __init__(self, provider: str = 'edge_tts',
                 voice: str = 'zh-CN-XiaoxiaoNeural'):
        """Initialize TTS processor.

        Args:
            provider: TTS provider slug ('edge_tts' or 'azure_tts').
            voice: Neural voice name, e.g. 'zh-CN-XiaoxiaoNeural'.
        """
        self.provider = provider
        self.voice = voice
        self._client = None
        logger.info(
            '[AudioOutput] Initialized (provider=%s, voice=%s)',
            provider, voice
        )

    def synthesize(self, text: str, output_path: str = '') -> bytes:
        """Synthesize speech and return raw audio bytes.

        Args:
            text: Text to convert to speech.
            output_path: Optional file path to save audio.

        Returns:
            bytes: Audio data on success, empty bytes on failure.
        """
        client = self._get_client()
        if not client:
            logger.error('[AudioOutput] No TTS client available')
            return b''
        result = client.synthesize(
            text, voice_name=self.voice, output_path=output_path
        )
        if result.get('success'):
            return result.get('audio_bytes', b'')
        logger.warning(
            '[AudioOutput] Synthesis failed: %s', result.get('error', 'unknown')
        )
        return b''

    def _get_client(self):
        """Lazy-init TTS client based on provider.

        Returns:
            EdgeTTSClient or AzureTTSClient, or None if unavailable.
        """
        if self._client is not None:
            return self._client

        if self.provider == 'edge_tts':
            return self._get_edge_client()
        elif self.provider == 'azure_tts':
            return self._get_azure_client()
        else:
            logger.error(
                '[AudioOutput] Unknown provider: %s', self.provider
            )
            return None

    def _get_edge_client(self):
        """Lazy-init Edge TTS client (no key required)."""
        try:
            import sys as _sys
            _sys.path.insert(
                0, os.path.join(os.path.dirname(__file__), '..', 'auth-center')
            )
            from services.edge_tts_client import EdgeTTSClient
        except ImportError as e:
            logger.error(
                '[AudioOutput] Failed to import EdgeTTSClient: %s', e
            )
            return None
        self._client = EdgeTTSClient()
        logger.info('[AudioOutput] Edge TTS client ready (free, no key)')
        return self._client

    def _get_azure_client(self):
        """Lazy-init Azure TTS client with subscription key."""
        try:
            import sys as _sys
            _sys.path.insert(
                0, os.path.join(os.path.dirname(__file__), '..', 'auth-center')
            )
            from services.azure_tts_client import AzureTTSClient
        except ImportError as e:
            logger.error(
                '[AudioOutput] Failed to import AzureTTSClient: %s', e
            )
            return None
        key = self._resolve_key('azure')
        if not key:
            logger.error(
                '[AudioOutput] Azure subscription key not configured '
                '(add to provider_api_keys with provider="azure")'
            )
            return None
        region = self._resolve_region()
        self._client = AzureTTSClient(subscription_key=key, region=region)
        logger.info('[AudioOutput] Azure TTS client ready (region=%s)', region)
        return self._client

    def _resolve_key(self, provider_slug: str) -> str:
        """Read API key from provider_api_keys table.

        Args:
            provider_slug: Provider identifier (e.g. 'azure').

        Returns:
            Decrypted key string or empty string if not found.
        """
        try:
            sys.path.insert(
                0, os.path.join(os.path.dirname(__file__), '..', 'auth-center')
            )
            from models import get_db
            with get_db() as conn:
                row = conn.execute(
                    "SELECT key_value_enc FROM provider_api_keys "
                    "WHERE provider=%s AND is_active=1 LIMIT 1",
                    (provider_slug,)
                ).fetchone()
                if row and row['key_value_enc']:
                    from services.crypto import decrypt
                    return decrypt(row['key_value_enc'])
        except Exception as e:
            logger.error(
                'Failed to resolve key for provider=%s: %s',
                provider_slug, e
            )
        return ''

    def _resolve_region(self) -> str:
        """Read Azure region from system_config, default to 'eastasia'.

        Returns:
            Azure region string.
        """
        try:
            sys.path.insert(
                0, os.path.join(os.path.dirname(__file__), '..', 'auth-center')
            )
            from models import get_db
            with get_db() as conn:
                row = conn.execute(
                    "SELECT value FROM system_config "
                    "WHERE key='azure_tts_region'"
                ).fetchone()
                if row and row['value']:
                    return row['value']
        except Exception:
            pass
        return 'eastasia'


def get_default_asr() -> AudioInputProcessor:
    """获取默认 ASR 处理器"""
    return AudioInputProcessor(provider='vosk')


def get_default_tts() -> AudioOutputProcessor:
    """Get default TTS processor (Edge TTS — free, no key)."""
    return AudioOutputProcessor(provider='edge_tts')
