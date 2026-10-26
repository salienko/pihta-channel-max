"""HTTP client for the booking core. The channel knows nothing about bookings."""
import aiohttp


class CoreError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f'{status} {code}: {message}')
        self.status, self.code, self.message = status, code, message


class Core:
    def __init__(self, base_url, token, timeout=8.0):
        self.base = base_url.rstrip('/')
        self.token = token
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = None

    async def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, path, payload):
        session = await self.session()
        async with session.post(self.base + path, json=payload,
                                headers={'X-Core-Token': self.token}) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                error = (body or {}).get('error') or {}
                raise CoreError(response.status, error.get('code', 'http_error'),
                                error.get('message') or 'Сервис недоступен')
            return body

    async def screen(self, user_id, action='home'):
        return await self._post('/api/v1/screen',
                                {'platform': 'max', 'user_id': user_id, 'action': action})

    async def text(self, user_id, text):
        return await self._post('/api/v1/text',
                                {'platform': 'max', 'user_id': user_id, 'text': text})

    async def contact(self, user_id, phone):
        return await self._post('/api/v1/contact',
                                {'platform': 'max', 'user_id': user_id, 'phone': phone})
