"""Puzzle generator — hole + matching piece share same angle & size."""
import os, io, base64, random, math
from PIL import Image, ImageDraw, ImageFilter
import numpy as np

from ..config import IMAGE_DIR, TRACK_WIDTH, PIECE_MIN_X, TOLERANCE, CANVAS_HEIGHT

SHAPES = ['circle', 'triangle', 'square', 'diamond', 'ellipse']
DECOY_SHAPES = ['triangle', 'square', 'circle', 'diamond', 'ellipse', 'rectangle']

def _list_images():
    if not os.path.isdir(IMAGE_DIR): return []
    return sorted([f for f in os.listdir(IMAGE_DIR) if f.endswith(('.jpg','.jpeg','.png','.webp'))])

def _load_random_bg():
    imgs = _list_images()
    if not imgs: return _fallback(), "synthetic"
    fn = random.choice(imgs)
    try: return Image.open(os.path.join(IMAGE_DIR, fn)).convert("RGB"), fn
    except: return _fallback(), "synthetic"

def _fallback():
    w, h = TRACK_WIDTH, CANVAS_HEIGHT
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            arr[y,x] = [min(255,max(0,60+int(80*math.sin(x*0.03+y*0.02)))),
                        min(255,max(0,80+int(100*math.cos(x*0.02+y*0.04)))),
                        min(255,max(0,30+int(60*math.sin(x*0.04+y*0.03))))]
    return Image.fromarray(arr).filter(ImageFilter.GaussianBlur(2))

def _shape_mask(shape, size):
    w, h = size
    mask = Image.new('L', (w, h), 0)
    d = ImageDraw.Draw(mask)
    pad = 2
    if shape == 'circle':
        r = min(w,h)//2 - pad; d.ellipse([w//2-r, h//2-r, w//2+r, h//2+r], fill=255)
    elif shape == 'triangle':
        d.polygon([(w//2,pad),(w-pad,h-pad),(pad,h-pad)], fill=255)
    elif shape == 'square':
        s = min(w,h)-pad*2; ox,oy = (w-s)//2,(h-s)//2
        d.rounded_rectangle([ox,oy,ox+s,oy+s], radius=4, fill=255)
    elif shape == 'ellipse':
        d.ellipse([pad,pad,w-pad,h-pad], fill=255)
    elif shape == 'diamond':
        d.polygon([(w//2,pad),(w-pad,h//2),(w//2,h-pad),(pad,h//2)], fill=255)
    elif shape == 'rectangle':
        d.rounded_rectangle([pad,pad,w-pad,h-pad], radius=4, fill=255)
    return mask

def _cut_with_mask(bg_img, mask, sx, sy, mw, mh):
    """Cut a region from bg_img using mask at (sx, sy), return RGBA image."""
    result = Image.new('RGBA', (mw, mh), (0,0,0,0))
    for y in range(mh):
        for x in range(mw):
            if mask.getpixel((x,y)) > 128:
                bx, by = sx+x, sy+y
                if 0<=bx<bg_img.width and 0<=by<bg_img.height:
                    px = bg_img.getpixel((bx,by))
                    result.putpixel((x,y), (*px,255))
    return result

def _cut_hole(bg_rgba, mask, sx, sy, mw, mh):
    """Cut a transparent hole in bg_rgba using mask at (sx, sy)."""
    for y in range(mh):
        for x in range(mw):
            if mask.getpixel((x,y)) > 128:
                bx, by = sx+x, sy+y
                if 0<=bx<bg_rgba.width and 0<=by<bg_rgba.height:
                    bg_rgba.putpixel((bx,by), (0,0,0,0))

def generate_puzzle():
    bg_img, bg_name = _load_random_bg()
    bg_w, bg_h = bg_img.size

    if bg_w != TRACK_WIDTH:
        bg_img = bg_img.resize((TRACK_WIDTH, int(bg_h*(TRACK_WIDTH/bg_w))), Image.LANCZOS)
        bg_w, bg_h = bg_img.size
    if bg_h > CANVAS_HEIGHT:
        cy = (bg_h-CANVAS_HEIGHT)//2; bg_img = bg_img.crop((0, cy, TRACK_WIDTH, cy+CANVAS_HEIGHT))
    elif bg_h < CANVAS_HEIGHT:
        pad = Image.new('RGB', (TRACK_WIDTH, CANVAS_HEIGHT), (11,17,32))
        py = (CANVAS_HEIGHT-bg_h)//2; pad.paste(bg_img, (0, py)); bg_img = pad
    bg_w, bg_h = bg_img.size

    # Shape size
    pw = random.randint(32, 44)
    ph = random.randint(32, 44)
    # Same angle for hole + matching piece
    target_angle = random.randint(0, 359)

    # ── Hole + matching piece ──
    hole_shape = random.choice(SHAPES)
    # Create mask at (pw+4, ph+4) then rotate both mask and use
    mask_size = pw+6
    base_mask = _shape_mask(hole_shape, (mask_size, mask_size))
    # Rotate mask
    rotated_mask = base_mask.rotate(target_angle, expand=True, fillcolor=0)
    rmw, rmh = rotated_mask.size

    hx = random.randint(20, bg_w-rmw-20)
    hy = random.randint(20, bg_h-rmh-40)

    hole = {"shape": hole_shape, "x": hx, "y": hy, "w": rmw, "h": rmh, "angle": target_angle}

    # Cut hole
    bg_rgba = bg_img.convert('RGBA')
    _cut_hole(bg_rgba, rotated_mask, hx, hy, rmw, rmh)

    # Correct piece
    c_img = _cut_with_mask(bg_img, rotated_mask, hx, hy, rmw, rmh)
    cbuf = io.BytesIO(); c_img.save(cbuf, format='PNG')
    correct_piece = {"shape": hole_shape, "imgData": "data:image/png;base64,"+base64.b64encode(cbuf.getvalue()).decode(),
                     "w": rmw, "h": rmh, "isTarget": True, "angle": target_angle}

    # ── Decoys ──
    pieces = [correct_piece]
    decoy_shapes = random.sample([s for s in DECOY_SHAPES if s != hole_shape], 2)
    used = [(hx, hy, rmw, rmh)]

    for ds in decoy_shapes:
        da = random.randint(0, 359)
        dmask = _shape_mask(ds, (mask_size, mask_size))
        drotated = dmask.rotate(da, expand=True, fillcolor=0)
        dw, dh = drotated.size

        # Find non-overlapping spot
        dx = dy = 0
        for _ in range(50):
            dx = random.randint(10, bg_w-dw-10)
            dy = random.randint(10, bg_h-dh-10)
            ok = True
            for ux, uy, uw, uh in used:
                if abs(dx-ux) < dw+10 and abs(dy-uy) < dh+10:
                    ok = False; break
            if ok: break
        used.append((dx, dy, dw, dh))

        dimg = _cut_with_mask(bg_img, drotated, dx, dy, dw, dh)
        dbuf = io.BytesIO(); dimg.save(dbuf, format='PNG')
        pieces.append({"shape": ds, "imgData": "data:image/png;base64,"+base64.b64encode(dbuf.getvalue()).decode(),
                       "w": dw, "h": dh, "isTarget": False, "angle": da})

    random.shuffle(pieces)

    # Positions — random on canvas, avoid hole area
    positions = []
    for p in pieces:
        pw = p["w"]; ph = p["h"]
        for _ in range(60):
            px = random.randint(5, bg_w-pw-5)
            py = random.randint(5, bg_h-ph-5)
            if abs(px-hx) < pw+15 and abs(py-hy) < ph+15: continue
            ok = True
            for ox, oy in positions:
                if abs(px-ox) < pw+8 and abs(py-oy) < ph+8:
                    ok = False; break
            if ok: positions.append((px, py)); break
        else: positions.append((10+len(positions)*(pw+10), 10))

    for i, p in enumerate(pieces):
        p["x"], p["y"] = positions[i]

    bg_buf = io.BytesIO(); bg_rgba.save(bg_buf, format='PNG')
    return {
        "background_b64": base64.b64encode(bg_buf.getvalue()).decode(),
        "hole": hole,
        "pieces": pieces,
        "image_id": bg_name,
    }
