from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def build_icon(output: Path) -> Path:
    size = 1024
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Match the dashboard's rounded arch A: one quiet silhouette at taskbar size.
    draw.rounded_rectangle(
        (48, 48, size - 48, size - 48),
        radius=224,
        fill=(32, 35, 41, 255),
    )
    mask = image.getchannel("A")
    for y in range(48, 977):
        t = (y - 48) / 928
        color = tuple(round(a + (b - a) * t) for a, b in zip((72, 83, 99), (32, 35, 41)))
        draw.line((48, y, 976, y), fill=(*color, 255))
    image.putalpha(mask)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((49, 49, 975, 975), radius=224, outline=(141, 151, 164, 255), width=3)
    ink = (250, 251, 253, 255)
    draw.arc((248, 222, 776, 750), 180, 360, fill=ink, width=68)
    draw.line((282, 486, 282, 742), fill=ink, width=68)
    draw.line((742, 486, 742, 742), fill=ink, width=68)
    draw.line((282, 582, 742, 582), fill=ink, width=68)
    for x in (282, 742):
        draw.ellipse((x - 34, 708, x + 34, 776), fill=ink)

    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(
        output,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    image.resize((256, 256), Image.Resampling.LANCZOS).save(output.with_suffix(".png"))
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(build_icon(args.output))


if __name__ == "__main__":
    main()
