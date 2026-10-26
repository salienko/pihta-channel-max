"""MAX channel tests: rendering, update parsing, webhook and delivery contracts."""
import unittest

from aiohttp.test_utils import TestClient, TestServer

import bot as channel_bot
from render import attachments, button, chunks, markup


class FakeApi:
    def __init__(self):
        self.sent, self.edits, self.answers, self.commands, self.subscribed = [], [], [], None, None

    error = None

    async def send(self, *, user_id=None, chat_id=None, text=None, attachments=None, notify=True):
        if self.error:
            raise self.error
        self.sent.append({'user_id': user_id, 'text': text, 'attachments': attachments})
        return {'ok': True}

    async def edit(self, message_id, *, text=None, attachments=None, notify=True):
        self.edits.append({'message_id': message_id, 'text': text, 'attachments': attachments})
        return {'ok': True}

    async def answer(self, callback_id, *, notification=None, message=None):
        self.answers.append({'id': callback_id, 'notification': notification})
        return {'ok': True}

    async def upload(self, media_type, payload, filename='upload'):
        return {'type': 'image', 'payload': {'token': 'banner-token'}}

    async def patch_commands(self, commands):
        self.commands = commands

    async def subscribe(self, url, secret=None, update_types=None):
        self.subscribed = (url, secret, update_types)


class FakeCore:
    def __init__(self, screen=None):
        self.calls = []
        self.screen_result = screen or {'text': 'Экран', 'rows': [], 'notice': 'Готово'}

    async def screen(self, user_id, action='home'):
        self.calls.append(('screen', user_id, action))
        return self.screen_result

    async def text(self, user_id, text):
        self.calls.append(('text', user_id, text))
        return self.screen_result

    async def contact(self, user_id, phone):
        self.calls.append(('contact', user_id, phone))
        return self.screen_result


class Rendering(unittest.TestCase):
    def test_callback_button(self):
        self.assertEqual(button({'text': 'Да', 'action': 'home'}),
                         {'type': 'callback', 'text': 'Да', 'payload': 'home'})

    def test_link_button(self):
        item = button({'text': 'Сайт', 'url': 'https://example.test'})
        self.assertEqual(item['type'], 'link')
        self.assertEqual(item['url'], 'https://example.test')

    def test_contact_button(self):
        self.assertEqual(button({'text': 'Номер', 'contact': True})['type'], 'request_contact')

    def test_empty_rows_no_markup(self):
        self.assertIsNone(markup({'rows': []}))

    def test_banner_prepended(self):
        built = attachments({'rows': [[{'text': 'Да', 'action': 'home'}]]},
                            {'type': 'image', 'payload': {'token': 't'}})
        self.assertEqual(built[0]['type'], 'image')
        self.assertEqual(built[1]['type'], 'inline_keyboard')

    def test_long_text_split(self):
        self.assertEqual(len(chunks('a' * 9000)), 3)


class Updates(unittest.TestCase):
    def test_text_message(self):
        update = {'update_type': 'message_created',
                  'message': {'body': {'mid': 'm1', 'text': 'Иван'},
                              'sender': {'user_id': 530433212}}}
        self.assertEqual(channel_bot.parse_update(update), ('text', 530433212, 'Иван', None, None))

    def test_contact_attachment(self):
        update = {'update_type': 'message_created',
                  'message': {'body': {'mid': 'm2', 'text': '', 'attachments': [
                      {'type': 'contact', 'payload': {'vcf_info': 'BEGIN:VCARD\nTEL;CELL:+79812440050\nEND:VCARD'}}]},
                      'sender': {'user_id': 7}}}
        self.assertEqual(channel_bot.parse_update(update), ('contact', 7, '+79812440050', None, None))

    def test_callback(self):
        update = {'update_type': 'message_callback',
                  'callback': {'callback_id': 'cb1', 'payload': 'begin',
                               'user': {'user_id': 9}, 'message': {'body': {'mid': 'm3'}}}}
        self.assertEqual(channel_bot.parse_update(update), ('callback', 9, 'begin', 'm3', 'cb1'))

    def test_bot_started(self):
        update = {'update_type': 'bot_started', 'user': {'user_id': 11}}
        self.assertEqual(channel_bot.parse_update(update), ('start', 11, 'home', None, None))

    def test_unknown_update_ignored(self):
        self.assertIsNone(channel_bot.parse_update({'update_type': 'message_edited'}))


class Endpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = FakeApi()
        self.core = FakeCore()
        self.channel = channel_bot.Channel(self.api, self.core)
        app = channel_bot.build_app(self.channel, 'core-secret', 'wh-secret')
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_health(self):
        response = await self.client.get('/health')
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['service'], 'pihta-channel-max')

    async def test_webhook_rejects_wrong_secret(self):
        response = await self.client.post('/webhook', json={'update_type': 'bot_started',
                                                           'user': {'user_id': 1}},
                                          headers={'X-Max-Bot-Api-Secret': 'wrong'})
        self.assertEqual(response.status, 403)

    async def test_bot_started_sends_menu(self):
        response = await self.client.post('/webhook', json={'update_type': 'bot_started',
                                                           'user': {'user_id': 530433212}},
                                          headers={'X-Max-Bot-Api-Secret': 'wh-secret'})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.core.calls[-1], ('screen', 530433212, 'home'))
        self.assertEqual(self.api.sent[-1]['user_id'], 530433212)
        self.assertEqual(self.api.sent[-1]['text'], 'Экран')

    async def test_callback_edits_and_answers(self):
        response = await self.client.post('/webhook', json={
            'update_type': 'message_callback',
            'callback': {'callback_id': 'cb1', 'payload': 'begin', 'user': {'user_id': 5},
                         'message': {'body': {'mid': 'm9'}}}},
            headers={'X-Max-Bot-Api-Secret': 'wh-secret'})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.core.calls[-1], ('screen', 5, 'begin'))
        # спиннер гасим пустым ответом, а плашка приходит сообщением в чат
        self.assertEqual(self.api.answers[-1], {'id': 'cb1', 'notification': None})
        self.assertEqual(self.api.sent[-1]['text'], 'Готово')
        self.assertEqual(self.api.edits[-1]['message_id'], 'm9')

    async def test_deliver_requires_core_token(self):
        response = await self.client.post('/deliver', json={'external_id': 5, 'text': 'привет'})
        self.assertEqual(response.status, 401)

    async def test_deliver_marks_unknown_recipient_permanent(self):
        self.api.error = channel_bot.MaxApiError(400, 'proto.payload', 'user not found')
        response = await self.client.post('/deliver', json={'external_id': 5, 'text': 'x'},
                                          headers={'X-Core-Token': 'core-secret'})
        self.assertEqual(response.status, 410)

    async def test_deliver_keeps_server_errors_retryable(self):
        self.api.error = channel_bot.MaxApiError(500, 'internal', 'boom')
        response = await self.client.post('/deliver', json={'external_id': 5, 'text': 'x'},
                                          headers={'X-Core-Token': 'core-secret'})
        self.assertEqual(response.status, 502)

    async def test_deliver_sends_text(self):
        response = await self.client.post('/deliver', json={'external_id': 5, 'text': 'привет'},
                                          headers={'X-Core-Token': 'core-secret'})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.api.sent[-1], {'user_id': 5, 'text': 'привет', 'attachments': None})

class CallbackWithoutNotice(unittest.IsolatedAsyncioTestCase):
    """Regression: an empty callback answer must not break the screen."""

    async def asyncSetUp(self):
        self.api = FakeApi()
        self.core = FakeCore(screen={'text': 'Экран', 'rows': [], 'notice': None})
        self.channel = channel_bot.Channel(self.api, self.core)

    async def test_no_answer_request_when_notice_empty(self):
        update = {'update_type': 'message_callback',
                  'callback': {'callback_id': 'cb1', 'payload': 'begin', 'user': {'user_id': 5},
                               'message': {'body': {'mid': 'm1'}}}}
        await channel_bot.handle(self.channel, update)
        # ответ нужен только чтобы снять спиннер, текста в нём нет
        self.assertEqual(self.api.answers, [{'id': 'cb1', 'notification': None}])
        self.assertFalse([m for m in self.api.sent if m['text']])
        self.assertEqual(self.api.edits[-1]['message_id'], 'm1')

    async def test_failed_answer_still_draws_screen(self):
        class FailingAnswer(FakeApi):
            async def answer(self, callback_id, *, notification=None, message=None):
                raise channel_bot.MaxApiError(400, 'proto.payload', 'Invalid request')

        api = FailingAnswer()
        channel = channel_bot.Channel(api, FakeCore(screen={'text': 'Экран', 'rows': [],
                                                            'notice': 'Готово'}))
        update = {'update_type': 'message_callback',
                  'callback': {'callback_id': 'cb2', 'payload': 'home', 'user': {'user_id': 5},
                               'message': {'body': {'mid': 'm2'}}}}
        await channel_bot.handle(channel, update)
        self.assertEqual(api.edits[-1]['message_id'], 'm2')
