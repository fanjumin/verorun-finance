#!/usr/bin/env python3
"""
AI Audio Interface — 语音输入/输出抽象层
========================================
- AudioInputProcessor: 语音识别（ASR），已实现 Vosk 本地识别（aliyun_asr 待实现）
- AudioOutputProcessor: 语音合成（TTS），Azure / Edge TTS
"""

from i18n import _
import io
import json
import os
import shutil
import subprocess
import wave
import logging

logger = logging.getLogger(__name__)

# Vosk 只接受 16kHz / 16bit / 单声道 PCM
_ASR_SAMPLE_RATE = 16000


def _wav_pcm16k(audio_data: bytes) -> bytes:
    """已是 16kHz/单声道/16bit 的 WAV 时直接取帧，避免调用 ffmpeg。

    其它格式（含其它采样率/声道的 WAV）返回 b''，交由 ffmpeg 归一化。
    """
    try:
        with wave.open(io.BytesIO(audio_data), 'rb') as w:
            if (w.getnchannels() == 1
                    and w.getframerate() == _ASR_SAMPLE_RATE
                    and w.getsampwidth() == 2):
                return w.readframes(w.getnframes())
    except Exception:
        pass
    return b''


def _ffmpeg_pcm16k(audio_data: bytes) -> bytes:
    """用系统 ffmpeg 把任意容器音频转成 16kHz/16bit/单声道 PCM。

    前端用 MediaRecorder 录的是 audio/webm;codecs=opus（见
    src/hooks/useAudioRecorder.ts:20），必须转码后才能喂给 Vosk。
    本机无 ffmpeg 或转码失败时返回 b''（调用方按"无识别结果"处理）。
    """
    if not shutil.which('ffmpeg'):
        return b''
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-i', 'pipe:0',
        '-f', 's16le', '-acodec', 'pcm_s16le',
        '-ac', '1', '-ar', str(_ASR_SAMPLE_RATE),
        'pipe:1',
    ]
    try:
        proc = subprocess.run(
            cmd, input=audio_data, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=120
        )
    except Exception as e:
        logger.warning('[AudioInput] ffmpeg 转码异常: %s', e)
        return b''
    if proc.returncode != 0 or not proc.stdout:
        logger.warning(
            '[AudioInput] ffmpeg 转码失败: %s',
            proc.stderr.decode('utf-8', 'ignore')[:200]
        )
        return b''
    return proc.stdout


def _to_pcm16k(audio_data: bytes) -> bytes:
    """把上传的音频字节归一化为 16kHz/16bit/单声道 PCM；无法处理时返回 b''。"""
    pcm = _wav_pcm16k(audio_data)
    if pcm:
        return pcm
    return _ffmpeg_pcm16k(audio_data)


class AudioInputProcessor:
    """语音输入处理器（ASR）。

    对外契约（与前端 useSpeech.ts / handlers.ts 对齐）：
      - transcribe(audio_data) / transcribe_file(path) 返回识别文本；
      - **未配置 provider 或识别失败一律返回 ''，不抛异常**（前端已按空文案容错）。

    provider 解析复用既有机制，不新造配置：
      - provider 由构造参数决定（默认 'vosk'）；
      - Vosk 模型路径取既有环境变量 VOSK_MODEL_PATH（原实现即如此）。
    本地无 Vosk 依赖/模型时 initialize() 返回 False，端点降级为空文案。
    """

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
        self._model = None
        self._recognizer = None
        logger.info(f'[AudioInput] 接口已创建（提供商: {provider}）')

    def initialize(self) -> bool:
        """初始化语音识别引擎；依赖缺失/未配置时返回 False（不抛异常）。"""
        if self._initialized:
            return True
        if self.provider == 'vosk':
            self._initialized = self._init_vosk()
        else:
            # 阿里云实时识别是 WebSocket 流式协议，本同步 base64 接口无法驱动
            logger.warning(
                '[AudioInput] 提供商 %s 未实现（本接口仅接收 base64 音频），返回空文案',
                self.provider
            )
        return self._initialized

    def transcribe(self, audio_data: bytes) -> str:
        """将音频数据转换为文本；未配置或失败返回 ''。"""
        if not audio_data:
            return ''
        if not self.initialize():
            return ''
        try:
            if self.provider == 'vosk':
                return self._transcribe_vosk(audio_data)
        except Exception as e:
            logger.error('[AudioInput] transcribe 失败: %s', e)
        return ''

    def transcribe_file(self, file_path: str) -> str:
        """识别音频文件；文件不可读时返回 ''。"""
        try:
            with open(file_path, 'rb') as f:
                return self.transcribe(f.read())
        except OSError as e:
            logger.error('[AudioInput] 读取音频文件失败: %s', e)
            return ''

    def _init_vosk(self) -> bool:
        """加载 Vosk 模型；未安装依赖或未配置模型路径时返回 False。"""
        try:
            import vosk
        except ImportError:
            logger.warning(
                '[AudioInput] 未安装 vosk（pip install vosk），ASR 不可用，返回空文案'
            )
            return False
        if not self.model_path or not os.path.isdir(self.model_path):
            logger.warning(
                '[AudioInput] 未配置 VOSK_MODEL_PATH（或目录不存在），'
                'ASR 不可用，返回空文案'
            )
            return False
        try:
            self._model = vosk.Model(self.model_path)
            self._recognizer = vosk.KaldiRecognizer(self._model, _ASR_SAMPLE_RATE)
            logger.info('[AudioInput] vosk 已就绪（model=%s）', self.model_path)
            return True
        except Exception as e:
            logger.error('[AudioInput] vosk 模型加载失败: %s', e)
            return False

    def _transcribe_vosk(self, audio_data: bytes) -> str:
        """单段音频整段识别（非实时流），返回文本。"""
        pcm = _to_pcm16k(audio_data)
        if not pcm:
            logger.warning(
                '[AudioInput] 音频无法归一化为 16kHz PCM（需本机 ffmpeg），返回空文案'
            )
            return ''
        self._recognizer.Reset()
        self._recognizer.AcceptWaveform(pcm)
        text = json.loads(self._recognizer.FinalResult()).get('text', '')
        return (text or '').strip()

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
