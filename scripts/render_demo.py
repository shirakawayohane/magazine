#!/usr/bin/env python3
"""Render the offline CLI demo. Developer dependency: Pillow (not needed by mag)."""
import argparse
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from demo import capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", default="/System/Library/Fonts/Menlo.ttc", help="Path to a monospace font")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[1] / "docs" / "assets"
    directory.mkdir(parents=True, exist_ok=True)
    scenes = capture()
    font = ImageFont.truetype(args.font, 23)
    small = ImageFont.truetype(args.font, 17)
    title = ImageFont.truetype(args.font, 36)
    frames = []
    palette = Image.new("RGB", (1, 1))
    colors = [(15, 19, 26), (23, 29, 38), (230, 237, 243), (147, 163, 180),
              (255, 142, 114), (111, 214, 178), (46, 56, 69)]
    palette = palette.convert("P")
    palette.putpalette([v for color in colors for v in color] + [0] * (768 - len(colors) * 3))
    for i, scene in enumerate(scenes):
        frame = Image.new("RGB", (1200, 760), colors[0])
        draw = ImageDraw.Draw(frame)
        draw.rounded_rectangle((42, 38, 76, 72), radius=7, fill=colors[4])
        draw.text((89, 36), "magazine", font=title, fill=colors[2])
        draw.text((43, 101), "Your Claude Code and Codex accounts, in one CLI.", font=small, fill=colors[3])
        draw.text((43, 148), scene["title"], font=font, fill=colors[4])
        draw.rounded_rectangle((38, 202, 1162, 662), radius=12, fill=colors[1], outline=colors[6])
        draw.text((63, 224), "$ " + scene["command"], font=font, fill=colors[5])
        output = scene["output"].replace("🔵 ", "").replace("🟢 ", "").replace("🔁 ", "")
        for row, line in enumerate(output.splitlines()):
            if row >= 12 or draw.textlength(line, font=font) > 1060:
                raise ValueError(f"Demo text would clip: {line}")
            color = colors[5] if "▶" in line else colors[2]
            draw.text((63, 268 + row * 29), line, font=font, fill=color)
        if draw.textlength(scene["note"], font=small) > 1120:
            raise ValueError("Demo caption would clip")
        draw.text((43, 683), scene["note"], font=small, fill=colors[3])
        draw.text((43, 717), "OFFLINE DEMO / SYNTHETIC DATA", font=small, fill=colors[4])
        for dot in range(len(scenes)):
            x = 1040 + dot * 25
            draw.ellipse((x, 724, x + 8, 732), fill=colors[4] if dot == i else colors[6])
        frames.append(frame.quantize(palette=palette, dither=Image.Dither.NONE))
    frames[0].save(directory / "demo.png")
    frames[0].save(directory / "demo.gif", save_all=True, append_images=frames[1:],
                   duration=[scene["seconds"] * 1000 for scene in scenes], loop=0, disposal=2, optimize=False)
    (directory / "demo-transcript.json").write_text(json.dumps(scenes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Rendered {len(frames)} scenes / {sum(s['seconds'] for s in scenes)} seconds to {directory}")


if __name__ == "__main__":
    main()
