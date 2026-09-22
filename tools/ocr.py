"""OCR an image with Tesseract. Requires pytesseract + Tesseract engine.
Usage:
  python tools/ocr.py <image.png or .jpg>
Output text is saved to out/ocr/<name>.txt.
"""
import sys
from pathlib import Path
from PIL import Image
import pytesseract

if len(sys.argv) < 2:
    sys.exit("usage: ocr.py <image.png>")

img_path = Path(sys.argv[1])
OUT = Path(__file__).parent.parent / "out" / "ocr"
OUT.mkdir(parents=True, exist_ok=True)

text = pytesseract.image_to_string(Image.open(img_path))
out = OUT / (img_path.stem + ".txt")
out.write_text(text, encoding="utf-8")
print(out)
