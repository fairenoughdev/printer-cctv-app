"""Isolated upload renderer. Invoked as a subprocess, never in the web worker."""

import json
import resource
import sys
from pathlib import Path

from PIL import Image, ImageOps


def render(source: Path, destination: Path):
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024**2, 1536 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (90, 95))
    Image.MAX_IMAGE_PIXELS = 40_000_000
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    pages = []
    if source.read_bytes()[:5] == b"%PDF-":
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(source)
        if not 1 <= len(document) <= 200:
            raise ValueError("PDF must contain between 1 and 200 pages")
        for index in range(len(document)):
            page = document[index]
            width, height = page.get_size()
            if min(width, height) <= 0:
                raise ValueError("Invalid PDF page dimensions")
            bitmap = page.render(scale=min(2, 2560 / max(width, height)))
            image = bitmap.to_pil().convert("RGB")
            image.save(destination / f"{index}.jpg", quality=88)
            pages.append({"index": index, "width": image.width, "height": image.height})
            image.close()
            bitmap.close()
            page.close()
        document.close()
    else:
        with Image.open(source) as original:
            if original.format not in ("JPEG", "PNG", "WEBP"):
                raise ValueError("Only PDF, JPEG, PNG, and WebP files are supported")
            original.load()
            image = ImageOps.exif_transpose(original).convert("RGBA")
            image.thumbnail((2560, 2560))
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            background.save(destination / "0.jpg", quality=90)
            pages.append({"index": 0, "width": background.width, "height": background.height})
    return pages


if __name__ == "__main__":
    try:
        print(json.dumps({"pages": render(Path(sys.argv[1]), Path(sys.argv[2]))}))
    except Exception:
        print(json.dumps({"error": "Cannot render this file. Check its format, PDF password, "
                          "page count (200 max), and image dimensions (40 megapixels max)."}))
        sys.exit(1)
