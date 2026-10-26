"""Server-side neural TTS fallback; this is not native speech-to-speech."""
import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack

import httpx

TOTAL_TIMEOUT_SECONDS = 60.0
MAX_AUDIO_BYTES = 5 * 1024 * 1024


class NeuralSpeechError(RuntimeError):
    """Public-safe speech failure without upstream response details."""


class NeuralSpeech:
    """Read server environment once; never accept browser credentials or URLs.

    TTS base/key optionally override the existing LLM base/key. Callers must
    authenticate/rate-limit their endpoint. Close the generator on disconnect
    (``contextlib.aclosing``); an early ``break`` alone does not close it.
    """

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        self._base = os.getenv('JARVIS_TTS_BASE_URL', os.getenv('JARVIS_LLM_BASE_URL', '')).rstrip('/')
        self._key = os.getenv('JARVIS_TTS_API_KEY', os.getenv('JARVIS_LLM_API_KEY', ''))
        self._model = os.getenv('JARVIS_TTS_MODEL', 'tts-mai-voice-2')
        self._voice = os.getenv('JARVIS_TTS_VOICE', 'alloy')
        self._transport = transport
        try:
            url = httpx.URL(self._base)
        except (httpx.InvalidURL, ValueError):
            raise NeuralSpeechError('Invalid speech provider configuration.') from None
        if (url.scheme != 'https' or not url.host or url.userinfo or url.query or url.fragment
                or not self._key or any(not 33 <= ord(char) <= 126 for char in self._key)):
            raise NeuralSpeechError('Invalid speech provider configuration.')

    async def synthesize_bytes(self, text: str) -> bytes:
        """Collect one complete bounded MP3, raising instead of returning partial audio."""
        return b''.join([chunk async for chunk in self.synthesize(text)])

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield MP3 chunks; errors may follow earlier audio. Cancellation propagates.

        Input errors raise ValueError; provider/configuration errors raise the
        public-safe NeuralSpeechError. No browser speech synthesis is used.
        The 60-second deadline is enforced at network operations, never while
        suspended at yield executing the consumer's unrelated code.
        """
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError('Speech text must contain 1 to 4000 characters.')
        deadline = asyncio.get_running_loop().time() + TOTAL_TIMEOUT_SECONDS
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout_at(deadline):
                    client = await stack.enter_async_context(httpx.AsyncClient(
                        transport=self._transport, timeout=httpx.Timeout(60.0, connect=10.0),
                        follow_redirects=False, trust_env=False,
                    ))
                    response = await stack.enter_async_context(client.stream(
                        'POST', self._base + '/audio/speech',
                        headers={'Authorization': 'Bearer ' + self._key, 'Accept-Encoding': 'identity'},
                        json={'model': self._model, 'voice': self._voice,
                              'input': text, 'response_format': 'mp3'},
                    ))
                if response.status_code != 200:
                    raise NeuralSpeechError('Speech provider rejected the request.')
                content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                if content_type not in {'audio/mpeg', 'audio/mp3'}:
                    raise NeuralSpeechError('Speech provider returned an invalid audio type.')
                if response.headers.get('content-encoding', 'identity').strip().lower() != 'identity':
                    raise NeuralSpeechError('Speech provider returned encoded audio.')
                length = response.headers.get('content-length')
                if length is not None:
                    if not length.isascii() or not length.isdecimal() or len(length) > 10 or int(length) > MAX_AUDIO_BYTES:
                        raise NeuralSpeechError('Speech audio exceeds the size limit.')
                total = 0
                chunks = response.aiter_bytes()
                while True:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError
                    try:
                        async with asyncio.timeout_at(deadline):
                            chunk = await anext(chunks)
                    except StopAsyncIteration:
                        break
                    total += len(chunk)
                    if total > MAX_AUDIO_BYTES:
                        raise NeuralSpeechError('Speech audio exceeds the size limit.')
                    if chunk:
                        yield chunk
                if not total:
                    raise NeuralSpeechError('Speech provider returned empty audio.')
        except (TimeoutError, httpx.TimeoutException):
            raise NeuralSpeechError('Speech request timed out.') from None
        except httpx.HTTPError:
            raise NeuralSpeechError('Speech provider is unavailable.') from None
