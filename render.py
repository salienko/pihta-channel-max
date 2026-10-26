"""Render a core screen with MAX attachments. No business rules here."""
from pathlib import Path

ASSETS = Path(__file__).with_name('assets')
MAX_TEXT = 3500


def button(item):
    if item.get('url'):
        return {'type': 'link', 'text': str(item['text'])[:128], 'url': item['url'][:2048]}
    if item.get('contact'):
        return {'type': 'request_contact', 'text': str(item['text'])[:128]}
    return {'type': 'callback', 'text': str(item['text'])[:128],
            'payload': str(item['action'])[:1024]}


def markup(screen):
    """Inline keyboard attachment, or None when the screen has no buttons."""
    rows = [[button(item) for item in row] for row in (screen.get('rows') or [])]
    if not rows:
        return None
    return {'type': 'inline_keyboard', 'payload': {'buttons': rows}}


def attachments(screen, banner=None):
    built = []
    if banner:
        built.append(banner)
    keyboard = markup(screen)
    if keyboard:
        built.append(keyboard)
    return built or None


def chunks(text, size=MAX_TEXT):
    text = text or ''
    return [text[i:i + size] for i in range(0, len(text), size)] or ['']


def banner_path(name):
    if not name:
        return None
    path = ASSETS / f'{name}.jpg'
    return path if path.exists() else None
