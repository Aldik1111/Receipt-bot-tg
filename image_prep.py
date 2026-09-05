"""Проверка и сжатие фото чека до запроса в Gemini.

Тяжёлая работа (Pillow) должна вызываться из рабочего потока, не из event loop.
"""

from __future__ import annotations

import hashlib
import os

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_RECEIPT_BYTES = 8 * 1024 * 1024
MAX_SIDE_PX = 1600
MIN_BYTES = 32


class ReceiptImageError(Exception):
    """code: too_large | unsupported | corrupt"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def sniff_image_mime(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
        return None
    return None


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_receipt_image(src_path: str) -> str:
    """Проверяет magic bytes, снимает EXIF, уменьшает длинную сторону.

    Возвращает путь к JPEG без EXIF (может совпасть с исходным путём).
    """
    try:
        size = os.path.getsize(src_path)
    except OSError as exc:
        raise ReceiptImageError("corrupt", "Не удалось прочитать файл") from exc
    if size > MAX_RECEIPT_BYTES:
        raise ReceiptImageError("too_large", "Файл больше 8 МБ")
    if size < MIN_BYTES:
        raise ReceiptImageError("corrupt", "Файл слишком маленький")

    with open(src_path, "rb") as fh:
        header = fh.read(16)
    if sniff_image_mime(header) is None:
        raise ReceiptImageError("unsupported", "Нужны JPEG, PNG или WebP")

    dest_path = os.path.splitext(src_path)[0] + ".prep.jpg"
    try:
        with Image.open(src_path) as image:
            image = ImageOps.exif_transpose(image)
            image.load()
            if image.mode != "RGB":
                image = image.convert("RGB")
            width, height = image.size
            longest = max(width, height)
            if longest > MAX_SIDE_PX:
                scale = MAX_SIDE_PX / longest
                image = image.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.Resampling.LANCZOS,
                )
            image.save(dest_path, format="JPEG", quality=85, optimize=True)
        if os.path.getsize(dest_path) > MAX_RECEIPT_BYTES:
            raise ReceiptImageError("too_large", "Файл больше 8 МБ")
        return dest_path
    except ReceiptImageError:
        _unlink_quietly(dest_path)
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        _unlink_quietly(dest_path)
        raise ReceiptImageError("corrupt", "Повреждённое изображение") from exc


def _unlink_quietly(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
