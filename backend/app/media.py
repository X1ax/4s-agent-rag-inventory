"""Local catalog images: bounded decoding, opaque keys, no client filenames."""
import asyncio
import io
import re
import warnings
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError

from backend.app.security import administrator

router = APIRouter(prefix='/api/media', tags=['资料图片'])
UPLOAD_KEY = re.compile(r'^upload-[a-f0-9]{32}$')
MAX_BYTES = 5*1024*1024


def image_path(root, key):
    if not UPLOAD_KEY.fullmatch(key):
        raise HTTPException(404, '图片不存在')
    return root/'data/uploads/images'/f'{key}.png'


def store_image(root, content):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=['PNG', 'JPEG', 'WEBP']) as picture:
                if picture.width*picture.height > 16_000_000 or getattr(picture, 'n_frames', 1) != 1:
                    raise HTTPException(422, '请选择不超过1600万像素的静态图片')
                picture.load()
                clean = ImageOps.exif_transpose(picture).convert('RGBA' if 'A' in picture.getbands() or 'transparency' in picture.info else 'RGB')
                key = 'upload-'+uuid4().hex
                path = image_path(root, key)
                path.parent.mkdir(parents=True, exist_ok=True)
                # Re-encode pixels only; original filenames and metadata are not retained.
                clean.save(path, format='PNG')
                return {'image_key': key, 'url': f'/api/media/images/{key}', 'source': 'local_upload'}
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, '文件不是有效的PNG、JPEG或WebP静态图片') from None


@router.post('/images', status_code=201)
async def upload(request: Request, user=Depends(administrator)):
    if request.headers.get('content-type', '').split(';')[0] not in {'image/png', 'image/jpeg', 'image/webp'}:
        raise HTTPException(415, '仅支持PNG、JPEG和WebP图片')
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BYTES:
            raise HTTPException(413, '图片大小不能超过5MB')
    return await asyncio.to_thread(store_image, request.app.state.knowledge.root, bytes(content))


@router.get('/images/{key}')
def image(key: str, request: Request):
    # Public preview matches existing public catalog imagery; key reveals no filesystem path.
    path = image_path(request.app.state.knowledge.root, key)
    if not path.is_file():
        raise HTTPException(404, '图片不存在')
    return FileResponse(path, media_type='image/png', headers={'X-Content-Type-Options': 'nosniff'})
