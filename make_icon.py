"""
make_icon.py — generate icon.ico for the Flurry .exe.

Renders an "F" formed from two stacked axes:
- The two handles stack vertically to make the F's stem.
- Each axe's blade extends to the right at the top of its handle —
  the upper axe's blade is the F's top crossbar, the lower axe's blade
  (sitting at the middle of the icon, where the handles meet) is the
  middle crossbar. The lower crossbar is shorter than the upper one,
  matching the letterform of a capital F.

Output: icon.ico (multi-resolution: 16/32/48/64/128/256) and icon.png
(single 256px raster, useful for previewing).

Run once after design changes; the .ico is committed and consumed by
build_exe.py via PyInstaller's --icon flag. Pillow is the only dependency.
"""

import os
from PIL import Image, ImageDraw

CANVAS = 256

# Palette — picks colors that match the app's dark UI (panel + slate +
# wood handle + faintly burnished blade).
BG = (24, 33, 54, 255)        # rounded-square background
BLADE = (213, 222, 234, 255)   # silver
BLADE_EDGE = (71, 85, 105, 255)
HANDLE = (146, 95, 50, 255)    # wood
HANDLE_EDGE = (84, 50, 24, 255)


def draw_horizontal_blade(d, neck_x, axis_y, extent_right,
                          height_above, height_below):
    """Draw an axe blade extending to the right.

    The blade attaches to the handle at (neck_x, axis_y) and tapers out
    to the right with a slight outward bulge — a 5-point polygon that
    reads as a fan-shaped axe head. `axis_y` is where the handle's top
    meets the blade's neck; the blade extends `height_above` above that
    line and `height_below` below it.
    """
    top_y = axis_y - height_above
    bot_y = axis_y + height_below
    tip_x = neck_x + extent_right
    h_total = bot_y - top_y
    pts = [
        (neck_x, top_y + h_total * 0.18),                    # neck top
        (tip_x - extent_right * 0.05, top_y),                # outer top corner
        (tip_x, axis_y),                                     # tip (rightmost)
        (tip_x - extent_right * 0.05, bot_y),                # outer bottom corner
        (neck_x, bot_y - h_total * 0.18),                    # neck bottom
    ]
    d.polygon(pts, fill=BLADE, outline=BLADE_EDGE)


def render(size=CANVAS):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded-square background. Radius is ~17% of size — enough to feel
    # like an app icon but not aggressive.
    d.rounded_rectangle((0, 0, size, size), radius=int(size * 0.17), fill=BG)

    # Layout (in 256px coords; we render at full size and Pillow downscales
    # for the smaller icon entries).
    handle_x = 75
    handle_w = 30
    stack_top = 44
    stack_bot = 212
    mid = (stack_top + stack_bot) // 2  # joint between the two axes

    # Top axe handle — upper half of F's vertical stem.
    d.rectangle(
        (handle_x, stack_top, handle_x + handle_w, mid),
        fill=HANDLE, outline=HANDLE_EDGE,
    )
    # Bottom axe handle — lower half. Drawn separately (rather than as
    # one rectangle) so we can outline each axe's haft individually if we
    # later want to add a leather grip wrap or similar detail at the joint.
    d.rectangle(
        (handle_x, mid, handle_x + handle_w, stack_bot),
        fill=HANDLE, outline=HANDLE_EDGE,
    )

    blade_neck_x = handle_x + handle_w  # right side of handle

    # Top crossbar — blade at the top of the upper handle.
    draw_horizontal_blade(
        d, blade_neck_x, stack_top,
        extent_right=110, height_above=20, height_below=36,
    )
    # Middle crossbar — blade at the joint between the two handles.
    # Shorter than the top crossbar so the icon reads as an F, not an E.
    draw_horizontal_blade(
        d, blade_neck_x, mid,
        extent_right=80, height_above=20, height_below=36,
    )

    return img


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    img = render(CANVAS)

    png_path = os.path.join(here, 'icon.png')
    ico_path = os.path.join(here, 'icon.ico')

    img.save(png_path)
    img.save(
        ico_path,
        format='ICO',
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f'Wrote {png_path}')
    print(f'Wrote {ico_path}')


if __name__ == '__main__':
    main()
