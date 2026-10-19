import sys, json, tempfile, os
sys.path.insert(0, '.')
from server import ocr, clipboard
from PIL import Image, ImageDraw

img = Image.new('RGB', (400, 100), 'white')
d = ImageDraw.Draw(img)
d.text((20, 30), 'Hello OCR World 123', fill='black')
p = os.path.join(tempfile.gettempdir(), 'ocr_test.png')
img.save(p)

print('=== ocr_file ===')
res = ocr.ocr_file(p)
print('lines:', len(res['lines']))
for w in res['lines'][:6]:
    print(' ', w)

print('=== find_text_on_screen ===')
res = ocr.find_text_on_screen('Fire')
print('matches:', res['count'], 'scanned:', res['scanned_words'])
for m in res['matches'][:5]:
    print(' ', m)

print('=== clipboard ===')
print(clipboard.read_clipboard())