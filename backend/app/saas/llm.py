"""Lazy HTTP-only OpenAI-compatible adapter; no SDK or local model imports."""
import asyncio
import json
import os
import httpx


class UpstreamProtocolError(Exception):
    pass


class LLMNotConfigured(Exception):
    pass


class OutputLimitError(Exception):
    pass


class OpenAICompatibleLLM:
    def __init__(self, settings, *, client=None):
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._client_lock = asyncio.Lock()

    async def _http(self, profile=None):
        if profile is None and not self.settings.llm_api_key:
            raise LLMNotConfigured()
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    # Windows SSL initialization can block the event loop for seconds.
                    self._client = await asyncio.to_thread(httpx.AsyncClient,
                        timeout=self.settings.chat_timeout_seconds, follow_redirects=False)
        return self._client

    def _request(self, messages, temperature, max_tokens, top_p=1, profile=None):
        return {'model': profile['model'] if profile else self.settings.llm_model, 'messages': messages,
                'temperature': temperature, 'top_p': top_p, 'max_tokens': max_tokens}

    def _binding(self, profile):
        if profile is None:
            return self.settings.llm_api_url, self.settings.llm_api_key
        from .model_profiles import validate_profile
        profile = validate_profile(profile, self.settings, resolve=True)
        url = profile['base_url']
        if not url.endswith('/chat/completions'):
            url += '/chat/completions'
        return url, os.environ.get(profile['api_key_env'], '') if profile['api_key_env'] else ''

    def _headers(self, key):
        return {'Accept': 'application/json', **({'Authorization': 'Bearer ' + key} if key else {})}

    async def complete(self, *, messages, temperature, max_tokens, top_p=1, timeout_seconds=None, profile=None, profile_id=None):
        url, key = self._binding(profile)
        client = await self._http(profile)
        payload = self._request(messages, temperature, max_tokens, top_p, profile)
        payload['stream'] = False
        # Bound the response body before JSON parsing, including provider error bodies.
        async with client.stream('POST', url, headers=self._headers(key), json=payload,
                                 timeout=timeout_seconds or self.settings.chat_timeout_seconds, follow_redirects=False) as response:
            response.raise_for_status()
            body = bytearray()
            async for block in response.aiter_bytes():
                body.extend(block)
                if len(body) > 1048576:
                    raise OutputLimitError()
        try:
            data = json.loads(body)
            content = data['choices'][0]['message']['content']
            if not isinstance(content, str):
                raise ValueError()
            return {'content': content, 'usage': data.get('usage', {})}
        except (ValueError, KeyError, IndexError, TypeError):
            raise UpstreamProtocolError() from None

    async def stream(self, *, messages, temperature, max_tokens, top_p=1, timeout_seconds=None, profile=None, profile_id=None):
        url, key = self._binding(profile)
        client = await self._http(profile)
        payload = self._request(messages, temperature, max_tokens, top_p, profile)
        payload['stream'] = True
        async with client.stream('POST', url, headers=self._headers(key), json=payload,
                                 timeout=timeout_seconds or self.settings.chat_timeout_seconds, follow_redirects=False) as response:
            response.raise_for_status()
            # Parse bounded bytes instead of aiter_lines, which can buffer an unbounded line.
            pending, event = b'', []
            total = 0
            async for block in response.aiter_bytes():
                total += len(block)
                if total > 2097152:
                    raise OutputLimitError()
                pending += block
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    if len(line) > 65536:
                        raise OutputLimitError()
                    line = line.rstrip(b'\r')
                    if line.startswith(b'data:'):
                        event.append(line[5:].lstrip())
                        if sum(map(len, event)) > 65536:
                            raise OutputLimitError()
                    elif not line and event:
                        data = b'\n'.join(event)
                        event = []
                        if data == b'[DONE]':
                            return
                        try:
                            frame = json.loads(data)
                            if 'error' in frame:
                                raise ValueError()
                            choices = frame.get('choices', [])
                            if not choices:
                                continue  # optional usage frame
                            content = choices[0].get('delta', {}).get('content')
                            if content is not None:
                                if not isinstance(content, str):
                                    raise ValueError()
                                yield content
                        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                            raise UpstreamProtocolError() from None
                if len(pending) > 65536:
                    raise OutputLimitError()
            raise UpstreamProtocolError()  # EOF is never an implicit success.

    async def close(self):
        if self._owns_client and self._client is not None:
            await self._client.aclose()
