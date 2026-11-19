"""Generate the Telegram contact QR for Bloodlust Optimizer.

Produces a dark rounded card with the t.me/Alpha0NemesiSS QR, styled in the
same spirit as the reference (black canvas, white rounded card, Telegram
glyph in the center).
"""

import math

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_H
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers import RoundedModuleDrawer
from qrcode.image.styles.colormasks import SolidFillColorMask

LINK = "https://t.me/Alpha0NemesiSS"
HANDLE = "@Alpha0NemesiSS"

# --- geometry ---------------------------------------------------------------
W = 900                      # canvas width
CARD_R = 72                  # card corner radius
PAD = 64                     # card padding
QR_BOX = 560                 # qr area in px
GLYPH_BOX = 116
OUT = "src/assets/telegram-qr.png"

INK = (16, 15, 14, 255)
CARD = (255, 255, 255, 255)
QRMOD = (24, 24, 24, 255)

# --- build the QR itself ----------------------------------------------------
qr = qrcode.QRCode(
    error_correction=ERROR_CORRECT_H,
    box_size=12,
    border=1,
)
qr.add_data(LINK)
qr.make(fit=True)

qr_img = qr.make_image(
    image_factory=StyledPilImage,
    module_drawer=RoundedModuleDrawer(radius_ratio=0.5),
    color_mask=SolidFillColorMask(back_color=CARD[:3], front_color=QRMOD[:3]),
).convert("RGB")
qr_img = qr_img.resize((QR_BOX, QR_BOX), Image.LANCZOS)

# --- canvas -----------------------------------------------------------------
canvas = Image.new("RGBA", (W, W), INK)
d = ImageDraw.Draw(canvas)
card = (PAD, PAD, W - PAD, W - PAD)
d.rounded_rectangle(card, radius=CARD_R, fill=CARD)

qx = card[0] + (card[2] - card[0] - QR_BOX) // 2
qy = card[1] + 44
canvas.paste(qr_img, (qx, qy))

# --- Telegram glyph in the finder-safe center --------------------------------
cx, cy = W // 2, qy + QR_BOX // 2
r = GLYPH_BOX // 2
d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(24, 24, 24, 255))
# paper plane: a rotated triangle-ish dart
plane = [(cx + r * 0.66, cy - r * 0.56), (cx - r * 0.62, cy + r * 0.02), (cx - r * 0.10, cy + r * 0.14)]
d.polygon(plane, fill=(255, 255, 255, 255))
keel = [(cx - r * 0.10, cy + r * 0.14), (cx - r * 0.62, cy + r * 0.02), (cx + r * 0.06, cy + r * 0.56)]
d.polygon(keel, fill=(255, 255, 255, 255))
fold = [(cx + r * 0.66, cy - r * 0.56), (cx - r * 0.10, cy + r * 0.14), (cx + r * 0.06, cy + r * 0.56)]
d.polygon(fold, fill=(214, 214, 214, 255))

# --- handle text --------------------------------------------------------------
def load_font(size):
    for name in ("segoeuib.ttf", "segoeui.ttf", "arialbd.ttf", "arial.ttf"):
        for base in (r"C:\Windows\Fonts",):
            try:
                return ImageFont.truetype(f"{base}\\{name}", size)
            except OSError:
                continue
    return ImageFont.load_default()

text = HANDLE
font = load_font(44)
text_box = d.textbbox((0, 0), text, font=font)
tw = text_box[2] - text_box[0]
d.text(((W - tw) / 2, card[3] - 74), text, font=font, fill=(24, 24, 24, 255))

import os
os.makedirs(os.path.dirname(OUT), exist_ok=True)
canvas.convert("RGB").save(OUT, quality=95)
print("written", OUT)
