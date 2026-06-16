"""
Style DNA Extractor — reads an existing .pptx and fingerprints its design.
Extracts: colors, fonts, slide size, logo positions, layout names.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_style_dna(pptx_path: str) -> dict:
    """
    Read an existing .pptx and return its full Style DNA dict.
    Used to match brand style when generating new presentations.
    """
    try:
        from pptx import Presentation
        from pptx.enum.dml import MSO_THEME_COLOR
    except ImportError:
        raise ImportError("python-pptx not installed. Run: pip install python-pptx")

    prs = Presentation(pptx_path)

    dna = {
        "source_file": Path(pptx_path).name,
        "colors": {
            "primary":   "1F4E79",
            "secondary": "2E75B6",
            "palette":   [],
        },
        "fonts": {
            "title":      "Calibri",
            "title_size": 36,
            "body":       "Calibri",
            "body_size":  18,
        },
        "slide_size": {
            "width_inches":  round(prs.slide_width  / 914400, 2),
            "height_inches": round(prs.slide_height / 914400, 2),
        },
        "backgrounds": [],
        "logo_positions": [],
        "layouts": [],
    }

    # ── Layout names from slide master ──
    for master in prs.slide_masters:
        for layout in master.slide_layouts:
            if layout.name not in dna["layouts"]:
                dna["layouts"].append(layout.name)

        # Background from master
        try:
            bg = master.background.fill
            if bg.type is not None:
                try:
                    rgb = bg.fore_color.rgb
                    dna["backgrounds"].append({
                        "type": str(bg.type),
                        "color": str(rgb),
                    })
                except Exception:
                    pass
        except Exception:
            pass

    # ── Sample first 5 slides for colors + fonts + logos ──
    color_counts: dict = {}
    sample_slides = list(prs.slides)[:5]

    for slide in sample_slides:
        for shape in slide.shapes:

            # ── Colors from shape fills ──
            try:
                fill = shape.fill
                if fill.type is not None:
                    rgb = str(fill.fore_color.rgb)
                    if rgb not in ("000000", "FFFFFF", "ffffff", "000000"):
                        color_counts[rgb] = color_counts.get(rgb, 0) + 1
            except Exception:
                pass

            # ── Colors from line ──
            try:
                rgb = str(shape.line.color.rgb)
                if rgb not in ("000000", "FFFFFF"):
                    color_counts[rgb] = color_counts.get(rgb, 0) + 1
            except Exception:
                pass

            # ── Fonts from text frames ──
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        try:
                            fname = run.font.name
                            fsize = run.font.size
                            if fname and fname not in ("+mj-lt", "+mn-lt", None):
                                size_pt = round(fsize / 12700, 1) if fsize else None
                                if size_pt and size_pt > 20:
                                    dna["fonts"]["title"]      = fname
                                    dna["fonts"]["title_size"] = size_pt
                                elif size_pt:
                                    dna["fonts"]["body"]      = fname
                                    dna["fonts"]["body_size"] = size_pt
                        except Exception:
                            pass

            # ── Logo detection (small image in a corner) ──
            try:
                if shape.shape_type == 13:   # MSO_SHAPE_TYPE.PICTURE
                    left_in   = round(shape.left   / 914400, 3)
                    top_in    = round(shape.top    / 914400, 3)
                    width_in  = round(shape.width  / 914400, 3)
                    height_in = round(shape.height / 914400, 3)
                    sw = dna["slide_size"]["width_inches"]
                    sh = dna["slide_size"]["height_inches"]
                    # Small image in corner = likely a logo
                    if width_in < 2.5 and height_in < 1.5:
                        corner = _detect_corner(left_in, top_in, sw, sh)
                        entry  = {
                            "left": left_in, "top": top_in,
                            "width": width_in, "height": height_in,
                            "corner": corner,
                        }
                        if entry not in dna["logo_positions"]:
                            dna["logo_positions"].append(entry)
            except Exception:
                pass

    # ── Build color palette from frequency ──
    top_colors = sorted(color_counts.items(), key=lambda x: x[1], reverse=True)[:6]
    palette = [c[0] for c in top_colors]
    dna["colors"]["palette"] = palette
    if len(palette) >= 1:
        dna["colors"]["primary"]   = palette[0]
    if len(palette) >= 2:
        dna["colors"]["secondary"] = palette[1]

    logger.info(
        "Style DNA extracted from %s | colors=%s | fonts=%s/%s",
        Path(pptx_path).name,
        palette[:3],
        dna["fonts"]["title"],
        dna["fonts"]["body"],
    )
    return dna


def _detect_corner(left: float, top: float, slide_w: float, slide_h: float) -> str:
    mid_x = slide_w / 2
    mid_y = slide_h / 2
    if left < mid_x and top < mid_y:  return "top-left"
    if left > mid_x and top < mid_y:  return "top-right"
    if left < mid_x and top > mid_y:  return "bottom-left"
    return "bottom-right"
