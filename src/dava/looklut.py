"""Hue-selective 'vibrant travel' look LUTs (.cube, 33^3) for a Rec.709 display-referred timeline.

Resolve's scripting API has no curves or qualifiers, so hue-specific grading (bluer sky, richer gold, without
cooling the gold or tinting the clouds) is done with a generated 3D LUT on a node, after the CDL balance.

Sky band (cyan..lavender): hue pulled toward clean sky blue, more saturation, slightly deeper.
Gold band (orange..yellow): hue pulled toward warm gold, more saturation, a touch brighter.
Everything else: gentle vibrance (low-saturation colors gain more than already-strong ones).
Neutrals (whites, greys, clouds, white dome) and near-blacks are protected by a saturation/value mask.
"""

import colorsys
import math


def smoothstep(e0, e1, x):
    t = min(max((x - e0) / (e1 - e0), 0.0), 1.0)
    return t * t * (3 - 2 * t)


def hue_band(h, center, inner, outer):
    """1 within `inner` degrees of center, cosine falloff to 0 at `outer` degrees."""
    d = abs((h - center + 180) % 360 - 180)
    if d <= inner:
        return 1.0
    if d >= outer:
        return 0.0
    return 0.5 * (1 + math.cos(math.pi * (d - inner) / (outer - inner)))


def pull_hue(h, target, amount):
    d = (target - h + 180) % 360 - 180
    return (h + d * amount) % 360


def look(r, g, b, p):
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    h *= 360
    protect = smoothstep(0.04, 0.18, s) * smoothstep(0.03, 0.12, v)
    # Skin: orange hues at moderate saturation and mid brightness. Kept out of the gold boost.
    skin = (hue_band(h, 20, 10, 22) * smoothstep(0.1, 0.2, s) * (1 - smoothstep(0.55, 0.7, s))
            * smoothstep(0.2, 0.35, v) * p.get("skin_protect", 0.0))
    wb = hue_band(h, 218, 22, 50) * protect      # sky: ~168..268 degrees
    wg = hue_band(h, 42, 12, 28) * protect * (1 - skin)   # gold: ~14..70 degrees
    wf = hue_band(h, 112, 22, 45) * protect      # foliage: ~67..157 degrees (yellow-green water stays out)
    h = pull_hue(h, 214, p["sky_shift"] * wb)
    h = pull_hue(h, 40, p["gold_shift"] * wg)
    h = pull_hue(h, 112, p.get("green_shift", 0.0) * wf)
    h = pull_hue(h, 22, p.get("skin_shift", 0.0) * skin)
    s = (s * (1 + (p["sky_sat"] - 1) * wb) * (1 + (p["gold_sat"] - 1) * wg)
         * (1 + (p.get("green_sat", 1.0) - 1) * wf) * (1 + (p.get("skin_sat", 1.0) - 1) * skin))
    # Colors handled by a band are excluded from the general vibrance; foliage only when its band is in use,
    # so presets without green settings behave exactly as before.
    green_active = p.get("green_sat", 1.0) != 1.0 or p.get("green_shift", 0.0) or p.get("green_deepen", 0.0)
    s = s * (1 + p["vibrance"] * (1 - s) * protect * (1 - max(wb, wg, wf if green_active else 0.0, skin)))
    v = (v * (1 - p["sky_deepen"] * wb) * (1 + p["gold_bright"] * wg) * (1 - p.get("green_deepen", 0.0) * wf)
         * (1 + p.get("skin_bright", 0.0) * skin))
    s, v = min(s, 1.0), min(v, 1.0)
    return colorsys.hsv_to_rgb(h / 360, s, v)


PRESETS = {
    "subtle": {"sky_shift": 0.25, "sky_sat": 1.25, "sky_deepen": 0.04, "gold_shift": 0.2, "gold_sat": 1.2,
               "gold_bright": 0.02, "vibrance": 0.08},
    "vibrant": {"sky_shift": 0.45, "sky_sat": 1.55, "sky_deepen": 0.055, "gold_shift": 0.32, "gold_sat": 1.45,
                "gold_bright": 0.03, "vibrance": 0.14},
    "bold": {"sky_shift": 0.55, "sky_sat": 1.7, "sky_deepen": 0.08, "gold_shift": 0.4, "gold_sat": 1.6,
             "gold_bright": 0.05, "vibrance": 0.18},
    # Travel photos with people: blues, greens and gold, skin protected and gently warmed.
    "travel": {"sky_shift": 0.45, "sky_sat": 1.5, "sky_deepen": 0.06, "gold_shift": 0.3, "gold_sat": 1.4,
               "gold_bright": 0.03, "vibrance": 0.12, "green_shift": 0.18, "green_sat": 1.18, "green_deepen": 0.04,
               "skin_protect": 1.0, "skin_shift": 0.15, "skin_sat": 1.06, "skin_bright": 0.02},
}


def write_cube(path, params, size=33, title="dava vibrant travel"):
    """Write the LUT; .cube order has red changing fastest."""
    with open(path, "w", encoding="ascii") as out:
        out.write(f'TITLE "{title}"\nLUT_3D_SIZE {size}\nDOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n')
        n = size - 1
        for bi in range(size):
            for gi in range(size):
                for ri in range(size):
                    r, g, b = look(ri / n, gi / n, bi / n, params)
                    out.write(f"{r:.6f} {g:.6f} {b:.6f}\n")
