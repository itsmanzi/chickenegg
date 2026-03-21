"""
Run this once to generate the PWA icons.
Requires: pip install Pillow
"""
from PIL import Image, ImageDraw
import math

def draw_icon(size):
    img = Image.new('RGBA', (size, size), (10, 10, 10, 255))
    d   = ImageDraw.Draw(img)
    s   = size

    # Background circle
    d.ellipse([s*0.04, s*0.04, s*0.96, s*0.96], fill=(26, 26, 16, 255))

    # Egg shape (oval)
    ex1, ey1, ex2, ey2 = s*0.28, s*0.12, s*0.72, s*0.78
    # gradient simulation — draw concentric ellipses
    steps = 18
    for i in range(steps, -1, -1):
        t   = i / steps
        pad = t * s * 0.04
        r   = int(180 + t * 50)
        g_  = int(160 + t * 48)
        b   = int(100 + t * 30)
        d.ellipse([ex1+pad, ey1+pad, ex2-pad, ey2-pad], fill=(r, g_, b, 255))

    # Hazard stripe
    stripe_y = s * 0.46
    stripe_h = s * 0.08
    for xi in range(0, size, int(s*0.12)):
        d.polygon([
            (xi,          stripe_y),
            (xi+s*0.06,   stripe_y),
            (xi+s*0.06-s*0.04, stripe_y+stripe_h),
            (xi-s*0.04,   stripe_y+stripe_h),
        ], fill=(245, 200, 66, 60))

    # Rivets
    rv = int(s * 0.055)
    for rx, ry in [(0.37, 0.32), (0.63, 0.32), (0.33, 0.58), (0.67, 0.58)]:
        cx, cy = int(s*rx), int(s*ry)
        d.ellipse([cx-rv, cy-rv, cx+rv, cy+rv], fill=(80, 76, 58, 255), outline=(30,28,20,255))
        lw = max(1, int(rv*0.35))
        d.line([cx-lw*2, cy, cx+lw*2, cy], fill=(30,28,20,255), width=lw)
        d.line([cx, cy-lw*2, cx, cy+lw*2], fill=(30,28,20,255), width=lw)

    # Wrench diagonal
    ww = max(2, int(s*0.025))
    d.line([int(s*0.32), int(s*0.72), int(s*0.68), int(s*0.18)],
           fill=(245, 200, 66, 200), width=ww)
    wr = int(s*0.065)
    d.ellipse([int(s*0.32)-wr, int(s*0.72)-wr, int(s*0.32)+wr, int(s*0.72)+wr],
              fill=(10,10,10,0), outline=(245,200,66,200), width=ww)
    d.ellipse([int(s*0.68)-wr, int(s*0.18)-wr, int(s*0.68)+wr, int(s*0.18)+wr],
              fill=(10,10,10,0), outline=(245,200,66,200), width=ww)

    # "ce" text hint at bottom
    font_size = max(12, int(s * 0.14))
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("arial.ttf", font_size)
    except Exception:
        font = None

    text = "ce"
    if font:
        bbox = d.textbbox((0,0), text, font=font)
        tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
        d.text(((s-tw)//2, int(s*0.82)), text, fill=(245,200,66,255), font=font)
    else:
        d.text((int(s*0.38), int(s*0.82)), text, fill=(245,200,66,255))

    return img

try:
    img192 = draw_icon(192)
    img192.save('static/icon-192.png')

    img512 = draw_icon(512)
    img512.save('static/icon-512.png')

    print("Icons created: static/icon-192.png and static/icon-512.png")
except ImportError:
    print("Pillow not installed. Run: pip install Pillow")
    print("Then run: python make_icons.py")
