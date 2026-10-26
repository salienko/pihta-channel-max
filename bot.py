"""Thin MAX channel: webhook in, core screen out, drawn with MAX attachments."""
import asyncio
import hmac
import logging
import os
import re
from pathlib import Path

from aiohttp import web

from core_client import Core, CoreError
from maxapi import MaxApi, MaxApiError
from render import attachments, banner_path, chunks

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger('channel.max')

TEL_RE = re.compile(r'TEL[^:\n]*:([+\d()\s\-]+)')
COMMANDS = [
    {'name': 'start', 'description': 'Главное меню'},
    {'name': 'book', 'description': 'Забронировать баню'},
    {'name': 'bookings', 'description': 'Мои бронирования'},
    {'name': 'help', 'description': 'Помощь'},
]


class Channel:
    def __init__(self, api, core, banners=None):
        self.api, self.core = api, core
        self.banners = banners if banners is not None else {}
        self.locks = {}

    def lock(self, user_id):
        return self.locks.setdefault(user_id, asyncio.Lock())

    async def banner(self, name):
        """Upload a banner once per process and reuse its token."""
        if not name:
            return None
        if name in self.banners:
            return self.banners[name]
        path = banner_path(name)
        if not path:
            return None
        try:
            attachment = await self.api.upload('image', path.read_bytes(), path.name)
        except MaxApiError as error:
            log.warning('banner upload failed for %s: %s', name, error)
            return None
        self.banners[name] = attachment
        return attachment

    async def send_screen(self, user_id, screen, message_id=None):
        banner = await self.banner(screen.get('photo'))
        markup = attachments(screen, banner)
        text = screen.get('text') or ''
        if message_id:
            try:
                await self.api.edit(message_id, text=text, attachments=markup)
                return
            except MaxApiError as error:
                if error.status not in (400, 404):
                    raise
        parts = chunks(text)
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            await self.api.send(user_id=user_id, text=part,
                                attachments=markup if last else None)


def parse_update(update):
    """Return (kind, user_id, payload, message_id, callback_id) or None."""
    kind = update.get('update_type')
    if kind == 'message_created':
        message = update.get('message') or {}
        sender = (message.get('sender') or {}).get('user_id')
        body = message.get('body') or {}
        text = body.get('text') or ''
        phone = None
        for attachment in body.get('attachments') or []:
            if attachment.get('type') == 'contact':
                info = (attachment.get('payload') or {}).get('vcf_info') or ''
                match = TEL_RE.search(info)
                if match:
                    phone = match.group(1).strip()
        if not sender or (not text and not phone):
            return None
        return ('contact' if phone else 'text', int(sender), phone or text, None, None)
    if kind == 'message_callback':
        callback = update.get('callback') or {}
        user_id = (callback.get('user') or {}).get('user_id')
        payload = callback.get('payload') or 'home'
        message = callback.get('message') or {}
        message_id = (message.get('body') or {}).get('mid')
        if not user_id:
            return None
        return ('callback', int(user_id), payload, message_id, callback.get('callback_id'))
    if kind == 'bot_started':
        user_id = (update.get('user') or {}).get('user_id')
        if not user_id:
            return None
        return ('start', int(user_id), 'home', None, None)
    return None


def build_app(channel, core_token, webhook_secret, webhook_url='', api=None):
    app = web.Application(client_max_size=65536)
    app['channel'] = channel

    async def health(request):
        return web.json_response({'status': 'ok', 'service': 'pihta-channel-max'})

    async def webhook(request):
        supplied = request.headers.get('X-Max-Bot-Api-Secret', '')
        if webhook_secret and not hmac.compare_digest(supplied, webhook_secret):
            return web.json_response({'error': 'forbidden'}, status=403)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({'ok': True})
        updates = payload if isinstance(payload, list) else payload.get('updates') or [payload]
        for update in updates:
            try:
                await handle(channel, update)
            except Exception:
                log.exception('update failed: %s', str(update)[:200])
        return web.json_response({'ok': True})

    async def deliver(request):
        supplied = request.headers.get('X-Core-Token', '')
        if not core_token or not hmac.compare_digest(supplied, core_token):
            return web.json_response({'error': 'unauthorized'}, status=401)
        body = await request.json()
        try:
            user_id = int(body['external_id'])
            text = str(body['text'])
        except (KeyError, TypeError, ValueError):
            return web.json_response({'error': 'external_id and text required'}, status=400)
        rows = body.get('rows') or []
        markup = attachments({'rows': rows}) if rows else None
        try:
            parts = chunks(text)
            for index, part in enumerate(parts):
                await channel.api.send(user_id=user_id, text=part,
                                       attachments=markup if index == len(parts) - 1 else None)
        except MaxApiError as error:
            if error.status == 429:
                return web.json_response({'error': 'rate_limited', 'retry_after': 30}, status=429)
            # 400/403/404 mean the recipient is unknown to MAX or has blocked the bot:
            # the core must stop retrying instead of hammering the queue forever.
            permanent = error.status in (400, 403, 404)
            log.error('delivery failed: %s permanent=%s', error, permanent)
            return web.json_response({'error': 'send_failed', 'permanent': permanent},
                                     status=410 if permanent else 502)
        return web.json_response({'ok': True})

    app.add_routes([web.get('/health', health), web.post('/webhook', webhook), web.post('/deliver', deliver)])
    return app


async def handle(channel, update):
    parsed = parse_update(update)
    if not parsed:
        return
    kind, user_id, payload, message_id, callback_id = parsed
    async with channel.lock(user_id):
        try:
            if kind == 'contact':
                screen = await channel.core.contact(user_id, payload)
            elif kind == 'text':
                screen = await channel.core.text(user_id, payload)
            else:
                screen = await channel.core.screen(user_id, payload)
        except CoreError as error:
            if callback_id:
                await channel.api.answer(callback_id, notification=error.message)
            else:
                await channel.api.send(user_id=user_id, text=error.message
                                       if error.status == 400 else 'Сервис обновляется. Попробуйте через минуту.')
            return
        if callback_id:
            try:
                await channel.api.answer(callback_id)
            except MaxApiError as error:
                log.warning('callback answer failed: %s', error)
        # Notice goes to the chat as its own message, the same as for plain text input.
        if screen.get('notice'):
            await channel.api.send(user_id=user_id, text=screen['notice'])
        if screen.get('nothing'):
            return
        await channel.send_screen(user_id, screen, message_id if kind == 'callback' else None)


async def startup(api, webhook_url, webhook_secret):
    try:
        await api.patch_commands(COMMANDS)
        log.info('commands registered')
    except MaxApiError as error:
        log.warning('commands registration failed: %s', error)
    if webhook_url:
        try:
            await api.subscribe(webhook_url, secret=webhook_secret,
                                update_types=['message_created', 'message_callback', 'bot_started'])
            log.info('webhook subscribed: %s', webhook_url)
        except MaxApiError as error:
            log.error('webhook subscription failed: %s', error)


async def main():
    token = os.getenv('MAX_BOT_TOKEN') or os.getenv('BOT_TOKEN', '')
    core_url = os.getenv('CORE_URL', '').strip()
    core_token = os.getenv('CORE_API_TOKEN', '').strip()
    secret = os.getenv('WEBHOOK_SECRET', '').strip()
    webhook_url = os.getenv('WEBHOOK_URL', '').strip()
    if not token or not core_url or not core_token:
        raise SystemExit('MAX_BOT_TOKEN, CORE_URL and CORE_API_TOKEN are required')
    api = MaxApi(token)
    core = Core(core_url, core_token)
    channel = Channel(api, core)
    app = build_app(channel, core_token, secret)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv('PORT', '3000'))
    await web.TCPSite(runner, '0.0.0.0', port).start()
    log.info('release=%s core=%s listening on 0.0.0.0:%s',
             os.getenv('RELEASE', 'dev'), core_url, port)
    await startup(api, webhook_url, secret)
    try:
        await asyncio.Event().wait()
    finally:
        await core.close()
        await api.close()
        await runner.cleanup()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as error:
        log.error('startup failed: %s: %s', type(error).__name__, str(error)[:300])
        raise
