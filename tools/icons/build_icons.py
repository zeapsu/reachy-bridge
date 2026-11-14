# /// script
# requires-python = ">=3.11"
# dependencies = ["opencv-python-headless>=4.10", "numpy>=2", "pillow>=11"]
# ///
"""Build the dashboard's icons from one master image.

`source-tile.png` is the generated tile artwork as delivered. This script
fixes it into the masters, then exports every icon size from them:

- master-tile.png: the tile with both eyes the same size (the right eye is a
  copy of the left, placed symmetrically about the head) and the bridge camera
  re-centred between them.
- master-head.png: the head alone (antennas, shell, eyes) on transparency.

Exports, into web/public/:

- favicon.ico (16, 32, 48): the head with a thin dark outline, so the white
  shell reads on light and dark browser tabs.
- apple-touch-icon.png (180) and icon-192/512.png: the tile, full bleed.
  iOS rounds the corners itself and fills transparency with black.
- icon-maskable-512.png: the head inside Android's safe zone (the central
  80% circle) on the tile's background colours.

Run from the repository root:  uv run tools/icons/build_icons.py
"""

from pathlib import Path

import cv2
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
PUBLIC = HERE.parents[1] / "web" / "public"

# Geometry of source-tile.png (1254 x 1254), measured from the artwork.
SHELL_CX = 620.0  # midpoint of the shell's left and right edges
LEFT_EYE = (443.65, 770.3, 133.2)  # centre x, centre y, outer rim radius
BRIDGE_CAMERA_X = 643  # the small camera between the eyes, before the fix
BRIDGE_ROWS = slice(735, 806)
NECK_TOP = 930  # below here the shell's outline is rebuilt (the neck joins it)
FULL_BLEED = (72, 60, 1110)  # x, y, side of the square cut from the tile
OUTLINE = (11, 15, 20)  # dashboard dark background (#0b0f14), as RGB


def eyes_fixed(src: np.ndarray) -> np.ndarray:
    """Return the tile with matching eyes and a centred bridge camera."""
    im = src.astype(np.float32)
    h, w = im.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)

    def disk(cx: float, cy: float, r: float, feather: float) -> np.ndarray:
        return np.clip((r - np.hypot(xx - cx, yy - cy)) / feather + 0.5, 0, 1)[..., None]

    def sample(mx: np.ndarray, my: np.ndarray) -> np.ndarray:
        return cv2.remap(im, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    out = im.copy()
    # Bridge: repaint the band with a plain column, then put the camera back
    # at the shell's centre line (the side dots no longer fit between the eyes).
    plain = im[BRIDGE_ROWS, 616:620].mean(1, keepdims=True)
    out[BRIDGE_ROWS, 574:668] = np.repeat(plain, 94, 1)
    a = disk(SHELL_CX, 770, 20, 2)
    out = out * (1 - a) + sample(xx + (BRIDGE_CAMERA_X - SHELL_CX), yy) * a

    # Right eye: the left eye, shifted to the mirror position about the shell.
    cx, cy, r = LEFT_EYE
    c2 = 2 * SHELL_CX - cx
    a = disk(c2, cy, r + 1.5, 1.5)
    out = out * (1 - a) + sample(xx - (c2 - cx), yy) * a
    # The left eye's right rim is where the bridge band meets it. On the copy,
    # take that rim sector from the left eye's clean left rim, mirrored.
    u, v = xx - c2, yy - cy
    rr = np.hypot(u, v)
    theta = np.degrees(np.arctan2(v, u))
    wgt = (
        np.clip((30 - np.abs(theta)) / 8, 0, 1) * np.clip((rr - (r - 20)) / 6, 0, 1) * np.clip((r + 2 - rr) / 1.5, 0, 1)
    )[..., None]
    out = out * (1 - wgt) + sample(cx - u, yy) * wgt
    return out.clip(0, 255).astype(np.uint8)


def head_mask(im: np.ndarray) -> np.ndarray:
    """Return the head's alpha (0-255): shell, eyes and antennas, no neck."""
    cv2.setRNGSeed(0)
    h, w = im.shape[:2]
    lum = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(int)
    local = cv2.cvtColor(cv2.medianBlur(im, 61), cv2.COLOR_BGR2GRAY).astype(int)

    # The shell: the largest bright region, with the eyes filled in.
    n, lab, stats, _ = cv2.connectedComponentsWithStats((lum > 120).astype(np.uint8))
    shell = (lab == 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])).astype(np.uint8)
    shell = fill_holes(shell)

    # GrabCut seeded with the shell and the antennas (darker than the tile).
    mask = np.full((h, w), cv2.GC_BGD, np.uint8)
    mask[60:1095, 150:1100] = cv2.GC_PR_BGD
    dark = lum < local - 18
    dark[:, :150] = dark[:, 1100:] = dark[1000:] = False
    mask[dark] = cv2.GC_PR_FGD
    mask[cv2.erode(shell, np.ones((9, 9), np.uint8)) > 0] = cv2.GC_FGD
    core = (lum < 35) & (lum < local - 25)
    core[:60] = core[540:] = core[:, :150] = core[:, 1100:] = False
    mask[core] = cv2.GC_FGD
    below = np.zeros((h, w), bool)
    for x in range(w):
        rows = np.flatnonzero(shell[:, x])
        if len(rows):
            below[rows.max() + 3 :, x] = True
    below[:900] = False
    mask[below] = cv2.GC_BGD
    bgd, fgd = np.zeros((1, 65)), np.zeros((1, 65))
    cv2.grabCut(im, mask, None, bgd, fgd, 6, cv2.GC_INIT_WITH_MASK)
    fg = ((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD)).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(fg)
    fg = (lab == 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])).astype(np.uint8)

    # The shell's shaded underside meets the neck, so rebuild its lower edge:
    # follow the grey shell down each column and bridge the neck's span.
    grey = lum > 80
    bottom = np.zeros(w)
    cols = [x for x in range(186, 1055) if grey[960, x]]
    for x in cols:
        y = 960
        while y < h - 1 and grey[y + 1, x]:
            y += 1
        bottom[x] = y if y < 1086 else 1082
    smooth = np.convolve(np.pad(bottom[cols], 5, mode="edge"), np.ones(11) / 11, "valid")
    fg[NECK_TOP:] = 0
    for x, y in zip(cols, smooth, strict=True):
        fg[NECK_TOP : int(round(y)) + 1, x] = 1
    fg = fill_holes(fg)
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    # Soften the cut edge by about a pixel.
    return cv2.GaussianBlur(fg.astype(np.float32) * 255, (0, 0), 0.8).astype(np.uint8)


def fill_holes(mask: np.ndarray) -> np.ndarray:
    h, w = mask.shape
    flood = mask.copy()
    cv2.floodFill(flood, np.zeros((h + 2, w + 2), np.uint8), (0, 0), 1)
    return mask | (1 - flood)


def rgba(im: np.ndarray, alpha: np.ndarray) -> Image.Image:
    rgb = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
    return Image.fromarray(np.dstack([rgb, alpha]), "RGBA")


def square(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """Crop to `box` and centre it on a transparent square."""
    img = img.crop(box)
    side = max(img.size)
    out = Image.new(img.mode, (side, side))
    out.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return out


def outlined(head: Image.Image, shell: Image.Image, size: int) -> Image.Image:
    """The head at `size` px with a dark outline about size/32 px wide
    around the shell (outlining the thin antennas would make them fat)."""
    ss = 8  # draw at 8x, then downsample
    width = max(size / 32, 0.75) * ss
    pad = int(np.ceil(width))
    inner = size * ss - 2 * pad
    canvas = Image.new("RGBA", (size * ss, size * ss))
    canvas.paste(head.resize((inner, inner), Image.LANCZOS), (pad, pad))
    body = np.zeros((size * ss, size * ss), np.uint8)
    body[pad : pad + inner, pad : pad + inner] = np.asarray(shell.resize((inner, inner), Image.LANCZOS))
    k = 2 * pad + 1
    ring = cv2.dilate(body, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    under = Image.new("RGBA", canvas.size, (*OUTLINE, 0))
    under.putalpha(Image.fromarray(ring))
    under.alpha_composite(canvas)
    return under.resize((size, size), Image.LANCZOS)


def maskable(head: Image.Image, tile: np.ndarray, size: int) -> Image.Image:
    """The head inside the central 80% circle, on the tile's colours."""
    rgb = cv2.cvtColor(tile, cv2.COLOR_BGR2RGB).astype(np.float32)
    top = rgb[140:200, 140:300].reshape(-1, 3).mean(0)
    bottom = rgb[1100:1160, 140:300].reshape(-1, 3).mean(0)
    t = np.linspace(0, 1, size, dtype=np.float32)[:, None, None]
    bg = np.broadcast_to(top * (1 - t) + bottom * t, (size, size, 3))
    out = Image.fromarray(bg.clip(0, 255).astype(np.uint8), "RGB").convert("RGBA")
    # Scale the head so its farthest opaque pixel touches the 80% circle.
    alpha = np.asarray(head)[..., 3]
    ys, xs = np.nonzero(alpha > 8)
    c = (head.width - 1) / 2
    reach = np.hypot(xs - c, ys - c).max() / head.width  # in head widths
    inner = int(size * 0.4 / reach)
    big = head.resize((inner, inner), Image.LANCZOS)
    out.alpha_composite(big, ((size - inner) // 2, (size - inner) // 2))
    return out


def main() -> None:
    src = cv2.imread(str(HERE / "source-tile.png"))
    tile = eyes_fixed(src)
    alpha = head_mask(tile)
    cv2.imwrite(str(HERE / "master-tile.png"), tile)
    cut = rgba(tile, alpha)
    box = cut.getbbox()
    head = square(cut, box)
    head.save(HERE / "master-head.png", optimize=True)
    # The shell alone: an opening wider than the antennas removes them.
    blob = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (81, 81))
    shell = square(Image.fromarray(cv2.morphologyEx(alpha, cv2.MORPH_OPEN, blob)), box)

    PUBLIC.mkdir(exist_ok=True)
    x, y, side = FULL_BLEED
    bleed = Image.fromarray(cv2.cvtColor(tile[y : y + side, x : x + side], cv2.COLOR_BGR2RGB))
    for name, size in [("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)]:
        bleed.resize((size, size), Image.LANCZOS).save(PUBLIC / name, optimize=True)
    maskable(head, tile, 512).convert("RGB").save(PUBLIC / "icon-maskable-512.png", optimize=True)
    sizes = [16, 32, 48]
    frames = [outlined(head, shell, s) for s in sizes]
    frames[-1].save(PUBLIC / "favicon.ico", sizes=[(s, s) for s in sizes], append_images=frames[:-1])


if __name__ == "__main__":
    main()
