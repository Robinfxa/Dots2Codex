"""Exact, bounded image payload validation and native MCP image projection."""
import base64
import binascii
import io
from .wire_support import require, ProtocolError

MAX_IMAGE_BYTES = 512 * 1024
MAX_PIXELS = 16_000_000
MAX_IMAGES = 8


def image_payload(part):
    require(isinstance(part, dict) and part.get('type') == 'input_image'
            and isinstance(part.get('image_url'), str), 'invalid_image_content')
    require(part.get('detail') in {None, 'auto', 'low', 'high', 'original'}, 'invalid_image_content')
    url = part['image_url']
    require(url.startswith('data:image/'), 'unsupported_image_source')
    header, separator, encoded = url.partition(',')
    mime = header[5:-7] if header.endswith(';base64') else ''
    require(separator and mime in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}, 'invalid_image_content')
    require(len(encoded) <= (MAX_IMAGE_BYTES + 2) // 3 * 4, 'image_content_too_large')
    try:
        raw = base64.b64decode(encoded, validate=True)
        require(len(raw) <= MAX_IMAGE_BYTES, 'image_content_too_large')
        # Pillow decodes headers without loading pixels; dimension limits are
        # checked before verify to avoid decompression bombs.
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as parsed:
            require(parsed.width > 0 and parsed.height > 0 and parsed.width * parsed.height <= MAX_PIXELS,
                    'image_dimensions_too_large')
            require(Image.MIME.get(parsed.format) == mime, 'invalid_image_content')
            require(getattr(parsed, 'n_frames', 1) == 1, 'invalid_image_content')
            parsed.verify()
    except ProtocolError:
        raise
    except Exception:
        require(False, 'invalid_image_content')
    return {'type': 'image', 'data': encoded, 'mimeType': mime,
            **({'_meta': {'codex/imageDetail': part['detail']}} if part.get('detail') else {})}


def content_images(items):
    """Extract only supported content positions, never arbitrary nested data."""
    found = []
    for item in items:
        kind = item.get('type', 'message' if 'role' in item else None)
        content = (item.get('content') if kind == 'message' else item.get('output')
                   if kind in {'function_call_output', 'custom_tool_call_output'} else None)
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'input_image':
                    found.append(image_payload(part))
                    require(len(found) <= MAX_IMAGES, 'image_count_exceeded')
    return found


def delivery_images(result):
    context = result.get('context')
    if not isinstance(context, dict): return []
    return content_images(context.get('history', context.get('append', [])))
