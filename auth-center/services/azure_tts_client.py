#!/usr/bin/env python3
"""Azure TTS Client — Microsoft Cognitive Services Text-to-Speech

Uses subscription key authentication (not HMAC signature).
Supports 400+ neural voices across 140+ languages.
Voice name convention: {locale}-{name}Neural (e.g. zh-CN-XiaoxiaoNeural).

Endpoint: POST https://{region}.tts.speech.microsoft.com/cognitiveservices/v1
Auth:     Header Ocp-Apim-Subscription-Key
Body:     SSML XML (<speak><voice>...</voice></speak>)
Response: audio/mpeg binary
"""

import requests
import logging
from i18n import _

logger = logging.getLogger(__name__)


class AzureTTSClient:
    """Microsoft Azure Text-to-Speech REST client.

    Authentication uses subscription key via Ocp-Apim-Subscription-Key header.
    SSML is used as request body to allow voice, rate, pitch control.
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

    def __init__(self, subscription_key: str, region: str = 'eastasia'):
        """Initialize Azure TTS client.

        Args:
            subscription_key: Azure Cognitive Services subscription key.
            region: Azure region (e.g. eastasia, eastus, westeurope).
        """
        self.key = subscription_key
        self.region = region
        self.endpoint = (
            f'https://{region}.tts.speech.microsoft.com/cognitiveservices/v1'
        )

    def synthesize(self, text: str, voice_name: str = 'zh-CN-XiaoxiaoNeural',
                   output_format: str = 'audio-24khz-96kbitrate-mono-mp3',
                   output_path: str = '') -> dict:
        """Synthesize speech from text via Azure TTS REST API.

        Args:
            text: Plain text to synthesize (will be XML-escaped for SSML).
            voice_name: Neural voice name, e.g. 'zh-CN-XiaoxiaoNeural'.
            output_format: Audio output format identifier.
            output_path: If provided, also save audio to this file path.

        Returns:
            dict with keys:
                success (bool): Whether synthesis succeeded.
                audio_bytes (bytes): Raw audio data on success.
                file_path (str): File path if output_path was provided.
                error (str): Error message on failure.
        """
        headers = {
            'Ocp-Apim-Subscription-Key': self.key,
            'Content-Type': 'application/ssml+xml',
            'X-Microsoft-OutputFormat': output_format,
        }

        # SSML body with escaped text
        escaped_text = self._escape_xml(text)
        ssml = (
            f'<speak version="1.0" '
            f'xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xml:lang="{voice_name[:5]}">'
            f'<voice name="{self._escape_xml(voice_name)}">{escaped_text}</voice>'
            f'</speak>'
        )

        try:
            resp = requests.post(
                self.endpoint,
                headers=headers,
                data=ssml.encode('utf-8'),
                timeout=60
            )

            if resp.status_code == 200:
                audio_bytes = resp.content
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

            logger.error(
                'Azure TTS HTTP %d: %s', resp.status_code, resp.text[:500]
            )
            return {
                'success': False,
                'error': _('Azure TTS returned error code %d') % resp.status_code,
            }

        except requests.exceptions.Timeout:
            logger.error('Azure TTS request timed out (60s)')
            return {'success': False, 'error': _('Azure TTS request timed out')}
        except requests.exceptions.ConnectionError as e:
            logger.error('Azure TTS connection failed: %s', e)
            return {'success': False, 'error': _('Azure TTS connection failed')}
        except Exception as e:
            logger.error('Azure TTS request failed: %s', e)
            return {'success': False, 'error': str(e)}

    def list_voices(self, locale: str = '') -> list:
        """Fetch available neural voices from Azure.

        Args:
            locale: Filter by locale code (e.g. 'zh-CN', 'en-US').
                    Empty string returns all voices.

        Returns:
            List of voice dicts from Azure API, each containing:
            Name, DisplayName, LocalName, ShortName, Gender, Locale, etc.
        """
        headers = {'Ocp-Apim-Subscription-Key': self.key}
        try:
            resp = requests.get(
                f'https://{self.region}.tts.speech.microsoft.com'
                f'/cognitiveservices/voices/list',
                headers=headers,
                timeout=30
            )
            if resp.status_code == 200:
                voices = resp.json()
                if locale:
                    voices = [
                        v for v in voices
                        if v.get('Locale') == locale
                    ]
                return voices
            logger.error(
                'Azure list voices HTTP %d: %s',
                resp.status_code, resp.text[:200]
            )
            return []
        except Exception as e:
            logger.error('Azure list voices failed: %s', e)
            return []

    @staticmethod
    def _escape_xml(text: str) -> str:
        """Escape XML special characters for SSML body.

        Args:
            text: Raw text that may contain XML special chars.

        Returns:
            XML-safe string with &, <, > escaped.
        """
        return (
            text.replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&apos;')
        )
