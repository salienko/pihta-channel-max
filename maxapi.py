"""Async HTTP client for the MAX Bot API (platform-api2.max.ru).

Trusts the Russian Trusted Root CA (bundled at ca/russian_trusted_root_ca.pem)
in addition to the system store; the API host is signed by it.
"""
import asyncio
import logging
import os
import ssl
from pathlib import Path

import aiohttp

BASE = os.getenv('MAX_API_BASE', 'https://platform-api2.max.ru')
ROOT_CA = Path(__file__).with_name('ca') / 'russian_trusted_root_ca.pem'
UPLOAD_TYPES = {'image': 'image', 'video': 'video', 'audio': 'audio', 'file': 'file'}


def ssl_context():
    ctx = ssl.create_default_context()
    if ROOT_CA.exists():
        ctx.load_verify_locations(str(ROOT_CA))
    return ctx


class MaxApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f'{status} {code}: {message}')
        self.status = status
        self.code = code
        self.message = message


class MaxApi:
    """Thin async wrapper. Single ClientSession, retries on 429/5xx."""

    def __init__(self, token, base=BASE):
        self.token = token.strip()
        self.base = base.rstrip('/')
        self._session = None

    async def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=ssl_context(), limit=20),
                timeout=aiohttp.ClientTimeout(total=60),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method, path, *, params=None, json=None, data=None,
                       headers=None, retries=5):
        url = f'{self.base}{path}'
        hdrs = {'Authorization': self.token}
        if headers:
            hdrs.update(headers)
        delay = 1.0
        for attempt in range(retries):
            s = await self.session()
            try:
                async with s.request(method, url, params=params, json=json,
                                     data=data, headers=hdrs) as resp:
                    body = await resp.json(content_type=None) if resp.content_type and 'json' in (resp.content_type or '') else None
                    if resp.status == 200:
                        return body if body is not None else {}
                    code = (body or {}).get('code', '')
                    message = (body or {}).get('message', '')
                    if resp.status == 429 or resp.status >= 500 or code == 'attachment.not.ready':
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 15)
                        continue
                    raise MaxApiError(resp.status, code, message)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15)
        raise MaxApiError(503, 'retries.exhausted', 'too many retries')

    # ---- identity / commands ----

    async def get_me(self):
        return await self._request('GET', '/me')

    async def patch_commands(self, commands):
        """commands: [{'name': 'start', 'description': 'Главное меню'}, ...]"""
        return await self._request('PATCH', '/me/commands',
                                   json={'commands': commands})

    # ---- messaging ----

    async def send(self, *, user_id=None, chat_id=None, text=None,
                   attachments=None, notify=True, disable_link_preview=False):
        params = {}
        if user_id is not None:
            params['user_id'] = str(user_id)
        if chat_id is not None:
            params['chat_id'] = str(chat_id)
        if disable_link_preview:
            params['disable_link_preview'] = 'true'
        body = {'text': text, 'notify': notify}
        if attachments is not None:
            body['attachments'] = attachments
        return await self._request('POST', '/messages', params=params, json=body)

    async def edit(self, message_id, *, text=None, attachments=None, notify=True):
        body = {'text': text, 'notify': notify}
        if attachments is not None:
            body['attachments'] = attachments
        return await self._request('PUT', '/messages',
                                   params={'message_id': str(message_id)},
                                   json=body)

    async def answer(self, callback_id, *, notification=None, message=None):
        body = {}
        if notification is not None:
            body['notification'] = notification
        if message is not None:
            body['message'] = message
        return await self._request('POST', '/answers',
                                   params={'callback_id': callback_id},
                                   json=body)

    # ---- uploads ----

    async def upload(self, media_type, payload: bytes, filename='upload'):
        """media_type: image|video|audio|file. Returns attachment payload dict."""
        if media_type not in UPLOAD_TYPES:
            raise ValueError('bad upload type')
        endpoint = await self._request('POST', '/uploads',
                                       params={'type': media_type})
        token = endpoint.get('token')
        if not token:
            upload_url = endpoint['url']
            s = await self.session()
            form = aiohttp.FormData()
            form.add_field('data', payload, filename=filename)
            async with s.post(upload_url, data=form) as resp:
                await resp.read()
                if resp.status != 200:
                    raise MaxApiError(resp.status, 'upload.failed', f'status {resp.status}')
                try:
                    token = (await resp.json(content_type=None)).get('token')
                except Exception:
                    token = None
        if not token:
            raise MaxApiError(502, 'upload.token.missing', 'no token after upload')
        return {'type': media_type, 'payload': {'token': token}}

    # ---- subscriptions / updates ----

    async def subscribe(self, url, secret=None, update_types=None):
        body = {'url': url}
        if secret:
            body['secret'] = secret
        if update_types:
            body['update_types'] = update_types
        return await self._request('POST', '/subscriptions', json=body)

    async def get_updates(self, marker=None, timeout=25, types=None):
        params = {'timeout': str(timeout)}
        if marker is not None:
            params['marker'] = str(marker)
        if types:
            params['types'] = ','.join(types)
        return await self._request('GET', '/updates', params=params,
                                   retries=1)

    # ---- logging helpers ----

    @staticmethod
    def quiet_loggers():
        logging.getLogger('aiohttp').setLevel(logging.CRITICAL)
        logging.getLogger('asyncio').setLevel(logging.CRITICAL)
