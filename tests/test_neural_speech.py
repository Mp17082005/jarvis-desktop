"""Tests use a mock HTTP transport, not synthesized provider output."""
import asyncio
import json

import httpx
import pytest


class AudioStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv('JARVIS_LLM_BASE_URL', 'https://tts.invalid/v1/')
    monkeypatch.setenv('JARVIS_LLM_API_KEY', 'private-test-key')
    monkeypatch.delenv('JARVIS_TTS_MODEL', raising=False)
    monkeypatch.delenv('JARVIS_TTS_VOICE', raising=False)
    monkeypatch.delenv('JARVIS_TTS_BASE_URL', raising=False)
    monkeypatch.delenv('JARVIS_TTS_API_KEY', raising=False)


def test_streams_audio_incrementally_with_server_auth(configured):
    from jarvis.neural_speech import NeuralSpeech

    stream = AudioStream([b'first-mock-audio', b'second-mock-audio'])
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(200, headers={'Content-Type': 'audio/mpeg'}, stream=stream)

    async def run():
        speech = NeuralSpeech(transport=httpx.MockTransport(provider))
        output = speech.synthesize('Hello.')
        assert await anext(output) == b'first-mock-audio'
        assert stream.reads == 1
        assert await anext(output) == b'second-mock-audio'
        with pytest.raises(StopAsyncIteration):
            await anext(output)

    asyncio.run(run())
    assert stream.closed
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == 'https://tts.invalid/v1/audio/speech'
    assert request.headers['Authorization'] == 'Bearer private-test-key'
    assert json.loads(request.content) == {
        'model': 'tts-mai-voice-2', 'voice': 'alloy', 'input': 'Hello.', 'response_format': 'mp3',
    }


@pytest.mark.parametrize('text', ['', ' \n\t', 'x' * 4001, None, b'hello'])
def test_invalid_input_never_contacts_provider(configured, text):
    from jarvis.neural_speech import NeuralSpeech

    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(200, headers={'Content-Type': 'audio/mpeg'}, content=b'audio')

    async def run():
        speech = NeuralSpeech(transport=httpx.MockTransport(provider))
        with pytest.raises(ValueError, match='1 to 4000'):
            async for _ in speech.synthesize(text):
                pass

    asyncio.run(run())
    assert not requests


@pytest.mark.parametrize('status,headers', [
    (401, {'Content-Type': 'audio/mpeg'}),
    (503, {'Content-Type': 'application/json'}),
    (302, {'Location': 'https://other.invalid/', 'Content-Type': 'audio/mpeg'}),
    (200, {'Content-Type': 'application/json'}),
    (200, {'Content-Type': 'text/html'}),
    (200, {}),
    (200, {'Content-Type': 'audio/wav'}),
])
def test_rejects_non_mp3_success_without_reading_error_body(configured, status, headers):
    from jarvis.neural_speech import NeuralSpeech, NeuralSpeechError

    stream = AudioStream([b'private-test-key provider secret details'])
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(status, headers=headers, stream=stream)

    async def run():
        with pytest.raises(NeuralSpeechError) as error:
            async for _ in NeuralSpeech(transport=httpx.MockTransport(provider)).synthesize('Hi'):
                pytest.fail('Non-audio response was emitted')
        assert 'private-test-key' not in str(error.value)
        assert 'provider secret' not in str(error.value)

    asyncio.run(run())
    assert stream.reads == 0
    assert stream.closed
    assert len(requests) == 1


@pytest.mark.parametrize('headers,chunks,expected_reads', [
    ({'Content-Length': str(5 * 1024 * 1024 + 1)}, [b'never read'], 0),
    ({}, [b'x' * (5 * 1024 * 1024), b'over bound'], 2),
    ({'Content-Length': '1'}, [b'x' * (5 * 1024 * 1024 + 1)], 1),
    ({}, [], 0),
    ({'Content-Encoding': 'gzip'}, [b'compressed-bomb'], 0),
])
def test_bounds_audio_without_trusting_length_header(configured, headers, chunks, expected_reads):
    from jarvis.neural_speech import NeuralSpeech, NeuralSpeechError

    stream = AudioStream(chunks)
    headers = {'Content-Type': 'audio/mpeg', **headers}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, stream=stream))
    emitted = []

    async def run():
        with pytest.raises(NeuralSpeechError):
            async for chunk in NeuralSpeech(transport=transport).synthesize('Hello'):
                emitted.append(chunk)

    asyncio.run(run())
    assert sum(map(len, emitted)) <= 5 * 1024 * 1024
    assert stream.closed
    assert stream.reads == expected_reads


@pytest.mark.parametrize('failure', [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_transport_failures_are_redacted(configured, failure):
    from jarvis.neural_speech import NeuralSpeech, NeuralSpeechError
    import traceback

    def provider(request):
        raise failure('private-test-key https://secret.invalid/raw-provider-error')

    async def run():
        with pytest.raises(NeuralSpeechError) as error:
            async for _ in NeuralSpeech(transport=httpx.MockTransport(provider)).synthesize('Hello'):
                pass
        rendered = ''.join(traceback.format_exception(error.value))
        assert 'private-test-key' not in rendered
        assert 'raw-provider-error' not in rendered

    asyncio.run(run())


def test_timeout_settings_are_bounded(configured):
    from jarvis.neural_speech import NeuralSpeech

    def provider(request):
        assert request.extensions['timeout'] == {'connect': 10.0, 'read': 60.0, 'write': 60.0, 'pool': 60.0}
        assert request.headers['Accept-Encoding'] == 'identity'
        return httpx.Response(200, headers={'Content-Type': 'audio/mpeg'}, content=b'audio')

    async def run():
        async for _ in NeuralSpeech(transport=httpx.MockTransport(provider)).synthesize('Hello'):
            pass

    asyncio.run(run())


def test_total_deadline_closes_slow_stream(configured, monkeypatch):
    import jarvis.neural_speech as module
    monkeypatch.setattr(module, 'TOTAL_TIMEOUT_SECONDS', 0.03, raising=False)

    class SlowStream(AudioStream):
        async def __aiter__(self):
            yield b'first'
            await asyncio.sleep(0.06)
            yield b'too late'

    stream = SlowStream([])
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={'Content-Type': 'audio/mpeg'}, stream=stream))

    async def run():
        with pytest.raises(module.NeuralSpeechError, match='timed out'):
            async for _ in module.NeuralSpeech(transport=transport).synthesize('Hello'):
                pass

    asyncio.run(run())
    assert stream.closed


def test_deadline_does_not_cancel_consumer_but_rejects_late_resume(configured, monkeypatch):
    import jarvis.neural_speech as module
    monkeypatch.setattr(module, 'TOTAL_TIMEOUT_SECONDS', 0.01)
    stream = AudioStream([b'first', b'late'])
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={'Content-Type': 'audio/mpeg'}, stream=stream))

    async def run():
        output = module.NeuralSpeech(transport=transport).synthesize('Hello')
        assert await anext(output) == b'first'
        await asyncio.sleep(0.03)
        with pytest.raises(module.NeuralSpeechError, match='timed out'):
            await anext(output)
        assert stream.closed

    asyncio.run(run())


@pytest.mark.parametrize('setting,value', [
    ('JARVIS_LLM_BASE_URL', ''), ('JARVIS_LLM_BASE_URL', 'http://tts.invalid/v1'),
    ('JARVIS_LLM_BASE_URL', 'https://user:password@tts.invalid/v1'),
    ('JARVIS_LLM_BASE_URL', 'https://tts.invalid/v1?secret=value'),
    ('JARVIS_LLM_API_KEY', ''), ('JARVIS_LLM_API_KEY', 'key\nInjected: yes'),
])
def test_rejects_unsafe_server_configuration(configured, monkeypatch, setting, value):
    from jarvis.neural_speech import NeuralSpeech, NeuralSpeechError
    monkeypatch.setenv(setting, value)
    with pytest.raises(NeuralSpeechError, match='configuration'):
        NeuralSpeech()


def test_custom_tts_configuration_and_buffered_helper(configured, monkeypatch):
    from jarvis.neural_speech import NeuralSpeech
    monkeypatch.setenv('JARVIS_TTS_BASE_URL', 'https://dedicated.invalid/v1')
    monkeypatch.setenv('JARVIS_TTS_API_KEY', 'dedicated-private-key')
    monkeypatch.setenv('JARVIS_TTS_MODEL', 'custom-model')
    monkeypatch.setenv('JARVIS_TTS_VOICE', 'custom-voice')
    text = 'அ' * 4000

    def provider(request):
        assert str(request.url) == 'https://dedicated.invalid/v1/audio/speech'
        assert request.headers['Authorization'] == 'Bearer dedicated-private-key'
        payload = json.loads(request.content)
        assert payload['input'] == text
        assert payload['model'] == 'custom-model'
        assert payload['voice'] == 'custom-voice'
        return httpx.Response(200, headers={'Content-Type': 'Audio/Mpeg; charset=binary'}, stream=AudioStream([b'a', b'b']))

    speech = NeuralSpeech(transport=httpx.MockTransport(provider))
    assert asyncio.run(speech.synthesize_bytes(text)) == b'ab'
    assert 'private-key' not in repr(speech)


@pytest.mark.parametrize('cancel', [False, True])
def test_cancellation_or_early_close_releases_stream(configured, cancel):
    from jarvis.neural_speech import NeuralSpeech
    entered = asyncio.Event()

    class BlockingStream(AudioStream):
        async def __aiter__(self):
            yield b'first'
            entered.set()
            await asyncio.Event().wait()

    stream = BlockingStream([])
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={'Content-Type': 'audio/mpeg'}, stream=stream))

    async def run():
        output = NeuralSpeech(transport=transport).synthesize('Hello')
        assert await anext(output) == b'first'
        if cancel:
            task = asyncio.create_task(anext(output))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await output.aclose()
        assert stream.closed

    asyncio.run(run())
