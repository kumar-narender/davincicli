"""Animated text overlays as Fusion compositions (text only, transparent background).

Why this shape (found on Resolve 21.1, free edition):
- Timeline.InsertFusionTitleIntoTimeline inserts at the playhead on V1 and ripples the clips there, so it
  cannot put a title on top of existing shots.
- A composition that merges text over its own MediaIn renders the photo shrunk inside a larger canvas.
- A composition whose output is only the text maps onto the frame correctly.
So an overlay is a carrier clip (any still) on a track above the shots, whose composition ignores its
MediaIn and outputs Follower -> Text+ -> Blur. The MediaIn/MediaOut blocks are taken from the carrier's
own default composition (exported at run time), so the file always matches the clip.

The animation is keyframed per letter with a StyledTextFollower (staggered fade), plus letter spacing,
size and blur curves.
"""

import re

PRESETS = {
    # Letters fade in one by one while the spacing collapses; then fade out one by one while expanding.
    "morph": {"center": (0.5, 0.79), "delay": 1.5, "style": "Heavy",
              "opacity": [(0, 0), (6, 1), (40, 1), (46, 0)],
              "size": [(0, 0.1), (22, 0.122), (46, 0.122), (68, 0.15)],
              "spacing": [(0, 1.7), (22, 1.03), (44, 1.03), (68, 1.6)],
              "blur": [(0, 14), (18, 0), (44, 0), (68, 24)]},
    # Short lower caption for one ~1 s shot.
    "caption": {"center": (0.5, 0.15), "delay": 0.5, "style": "Heavy",
                "opacity": [(1, 0), (5, 1), (18, 1), (22, 0)],
                "size": [(1, 0.085), (12, 0.1)],
                "spacing": [(1, 1.45), (12, 1.08), (24, 1.3)],
                "blur": [(1, 6), (7, 0), (18, 0), (24, 10)]},
    # Ending word that stays on screen.
    "outro": {"center": (0.5, 0.8), "delay": 2.0, "style": "Heavy",
              "opacity": [(4, 0), (10, 1)],
              "size": [(4, 0.09), (30, 0.11)],
              "spacing": [(4, 2.0), (28, 1.12)],
              "blur": [(4, 16), (20, 0)]},
}


def _spline(name, keys):
    frames = []
    for i, (t, v) in enumerate(keys):
        parts = [f"{v}"]
        if i > 0:
            pt = keys[i - 1][0]
            parts.append(f"LH = {{ {t - (t - pt) / 3:.4f}, {v} }}")
        if i < len(keys) - 1:
            nt = keys[i + 1][0]
            parts.append(f"RH = {{ {t + (nt - t) / 3:.4f}, {v} }}")
        frames.append(f"\t\t\t\t[{t}] = {{ {', '.join(parts)} }}")
    return (f"\t\t{name} = BezierSpline {{\n\t\t\tSplineColor = {{ Red = 204, Green = 0, Blue = 0 }},\n"
            f"\t\t\tNameSet = true,\n\t\t\tKeyFrames = {{\n" + ",\n".join(frames) + "\n\t\t\t}\n\t\t},\n")


def lua_string(text):
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def text_tools(text, preset="morph", font="Avenir Next", center=None, scale=1.0):
    """The Follower1/Text1/Blur1 tool definitions for a composition's Tools block."""
    p = PRESETS[preset]
    cx, cy = center or p["center"]
    size = [(t, round(v * scale, 5)) for t, v in p["size"]]
    return (
        _spline("Follower1Opacity", p["opacity"])
        + f'''		Follower1 = StyledTextFollower {{
			Inputs = {{
				Delay = Input {{ Value = {p["delay"]}, }},
				Text = Input {{
					Value = StyledText {{
						Array = {{
						}},
						Value = "{lua_string(text)}"
					}},
				}},
				Opacity1 = Input {{
					SourceOp = "Follower1Opacity",
					Source = "Value",
				}},
			}},
		}},
'''
        + _spline("Text1Size", size)
        + _spline("Text1CharacterSpacing", p["spacing"])
        + f'''		Text1 = TextPlus {{
			Inputs = {{
				UseFrameFormatSettings = Input {{ Value = 1, }},
				Center = Input {{ Value = {{ {cx}, {cy} }}, }},
				StyledText = Input {{
					SourceOp = "Follower1",
					Source = "StyledText",
				}},
				Font = Input {{ Value = "{lua_string(font)}", }},
				Style = Input {{ Value = "{p["style"]}", }},
				Size = Input {{
					SourceOp = "Text1Size",
					Source = "Value",
				}},
				CharacterSpacing = Input {{
					SourceOp = "Text1CharacterSpacing",
					Source = "Value",
				}},
				LineSpacing = Input {{ Value = 0.92, }},
			}},
		}},
'''
        + _spline("Blur1XBlurSize", p["blur"])
        + '''		Blur1 = Blur {
			Inputs = {
				XBlurSize = Input {
					SourceOp = "Blur1XBlurSize",
					Source = "Value",
				},
				Input = Input {
					SourceOp = "Text1",
					Source = "Output",
				},
			},
		},
'''
    )


def overlay_comp(default_comp, text, preset="morph", font="Avenir Next", center=None, scale=1.0):
    """Turn an exported default composition (MediaIn1 -> MediaOut1) into a text-only overlay."""
    comp, n = re.subn(r"(\tTools = \{\n)", lambda m: m.group(1) + text_tools(text, preset, font, center, scale),
                      default_comp, count=1)
    if n != 1:
        raise ValueError("The default composition has no Tools block.")
    comp, n = re.subn(r'(MediaOut1 = Saver \{.*?Input = Input \{\s*SourceOp = )"MediaIn1"', r'\1"Blur1"', comp,
                      count=1, flags=re.S)
    if n != 1:
        raise ValueError("The default composition does not route MediaIn1 into MediaOut1.")
    return comp
