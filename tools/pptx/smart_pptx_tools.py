"""
Smart PowerPoint Tool Mixin for BEACON.
Tools: generate_pptx | extract_pptx_style
"""
import copy
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DNA = {
    "source_file": "default",
    "colors": {
        "primary":   "1F4E79",
        "secondary": "2E75B6",
        "palette":   ["1F4E79", "2E75B6", "ED7D31", "70AD47", "FFC000"],
    },
    "fonts": {
        "title":      "Calibri",
        "title_size": 36,
        "body":       "Calibri",
        "body_size":  18,
    },
    "slide_size": {"width_inches": 13.33, "height_inches": 7.5},
    "backgrounds": [],
    "logo_positions": [],
    "layouts": [],
}


class SmartPptxToolsMixin:
    """BEACON Tool Mixin: generate beautiful PowerPoints with optional brand style matching."""

    async def _generate_pptx(self, topic, outline="", template_pptx="", output_path="", slide_count=7):
        """
        Generate a beautiful PowerPoint presentation.
        topic         - main subject
        outline       - optional extra context
        template_pptx - path to existing .pptx to match its style
        output_path   - where to save (default: output/<topic>.pptx)
        slide_count   - number of slides (default 7)
        """
        try:
            from openai import AsyncOpenAI
            from tools.pptx.style_extractor import extract_style_dna
            from tools.pptx.pptx_builder import build_beautiful_pptx
        except ImportError as exc:
            return "Missing dependency: {}  Run: pip install python-pptx".format(exc)

        # Resolve output path
        if not output_path:
            safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in str(topic))
            safe = safe.lower().replace(" ", "_")[:40]
            output_path = "output/{}.pptx".format(safe)
        path = Path(output_path)
        if str(path.parent) == ".":
            path = Path("output") / path.name

        # Extract Style DNA
        style_dna = copy.deepcopy(DEFAULT_DNA)
        template_note = "Modern corporate default style"

        if template_pptx:
            tp = Path(str(template_pptx))
            if not tp.exists():
                return "Template file not found: {}".format(template_pptx)
            try:
                style_dna = extract_style_dna(str(tp))
                template_note = "Matched from {} | Colors: {} | Fonts: {}/{}".format(
                    tp.name,
                    style_dna["colors"]["palette"][:3],
                    style_dna["fonts"]["title"],
                    style_dna["fonts"]["body"],
                )
            except Exception as exc:
                logger.warning("Style extraction failed: %s", exc)
                template_note = "Style extraction failed ({}) - using default".format(exc)

        # Build prompt
        try:
            slide_count_int = int(slide_count)
        except (TypeError, ValueError):
            slide_count_int = 7

        style_ctx = "Primary: #{}, Secondary: #{}, Title font: {}, Body font: {}".format(
            style_dna["colors"]["primary"],
            style_dna["colors"]["secondary"],
            style_dna["fonts"]["title"],
            style_dna["fonts"]["body"],
        )

        prompt_lines = [
            "You are a world-class presentation designer.",
            "Create a compelling {}-slide presentation on: \"{}\"".format(slide_count_int, topic),
        ]
        if outline:
            prompt_lines.append("Additional context: {}".format(outline))
        prompt_lines += [
            "Style context: {}".format(style_ctx),
            "",
            "Return ONLY a valid JSON array (no markdown, no explanation):",
            '[',
            '  {"layout": "title", "title": "Main Title", "subtitle": "Tagline"},',
            '  {"layout": "section", "title": "Section Name", "section_num": "01"},',
            '  {"layout": "content", "title": "Slide Title", "bullets": [',
            '    "Key point",',
            '    "  sub-point (2 leading spaces)",',
            '    "Another point"',
            '  ]}',
            ']',
            "",
            "Rules:",
            "- Slide 1 MUST be layout=title",
            "- Include 1-2 section divider slides",
            "- Content slides: 3-5 bullets, max 15 words each",
            "- Sub-bullets start with 2 spaces",
            "- Total slides MUST be exactly {}".format(slide_count_int),
            "- Make it professional and insightful",
        ]
        prompt = "\n".join(prompt_lines)

        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL"))
        response = await client.chat.completions.create(
            model=os.getenv("AI_MODEL", "global/anthropic.claude-sonnet-4-6"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )

        raw = response.choices[0].message.content.strip()
        if "```" in raw:
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        try:
            slides_content = json.loads(raw)
        except json.JSONDecodeError as exc:
            return "OpenAI returned invalid JSON: {}\nPreview: {}".format(exc, raw[:300])

        if not isinstance(slides_content, list):
            return "Expected JSON array from OpenAI, got: {}".format(type(slides_content))

        try:
            build_beautiful_pptx(
                slides_content=slides_content,
                style_dna=style_dna,
                output_path=str(path),
            )
        except Exception as exc:
            return "PPTX build error: {}".format(exc)

        layouts_used = sorted({s.get("layout", "content") for s in slides_content})
        return (
            "PowerPoint created successfully!\n"
            "File      : {}\n"
            "Slides    : {} ({})\n"
            "Fonts     : {} (title) / {} (body)\n"
            "Colors    : #{} / #{}\n"
            "Style     : {}\n"
            "Open with : Microsoft PowerPoint, Google Slides, or LibreOffice"
        ).format(
            path,
            len(slides_content),
            ", ".join(layouts_used),
            style_dna["fonts"]["title"],
            style_dna["fonts"]["body"],
            style_dna["colors"]["primary"],
            style_dna["colors"]["secondary"],
            template_note,
        )

    async def _extract_pptx_style(self, pptx_path):
        """
        Read an existing .pptx and show its Style DNA.
        pptx_path - full path to the .pptx file to inspect
        """
        try:
            from tools.pptx.style_extractor import extract_style_dna
        except ImportError:
            return "python-pptx not installed. Run: pip install python-pptx"

        p = Path(str(pptx_path))
        if not p.exists():
            return "File not found: {}".format(pptx_path)
        if p.suffix.lower() != ".pptx":
            return "Expected a .pptx file, got: {}".format(p.suffix)

        try:
            dna = extract_style_dna(str(p))
        except Exception as exc:
            return "Style extraction error: {}".format(exc)

        logos = dna.get("logo_positions", [])
        logo_str = (
            ", ".join(
                "{} ({}\" x {}\")".format(lo["corner"], lo["width"], lo["height"])
                for lo in logos
            )
            if logos else "None detected"
        )

        return (
            "Style DNA: {}\n"
            "{}\n"
            "Colors\n"
            "  Primary   : #{}\n"
            "  Secondary : #{}\n"
            "  Palette   : {}\n\n"
            "Fonts\n"
            "  Title : {} @ {}pt\n"
            "  Body  : {} @ {}pt\n\n"
            "Slide Size : {} x {} inches\n"
            "Layouts    : {}\n"
            "Logos      : {}\n"
            "{}\n"
            "Tip: generate_pptx(topic=\"...\", template_pptx=\"{}\")"
        ).format(
            p.name,
            "-" * 48,
            dna["colors"].get("primary", "N/A"),
            dna["colors"].get("secondary", "N/A"),
            dna["colors"].get("palette", []),
            dna["fonts"].get("title", "N/A"),
            dna["fonts"].get("title_size", "?"),
            dna["fonts"].get("body", "N/A"),
            dna["fonts"].get("body_size", "?"),
            dna["slide_size"]["width_inches"],
            dna["slide_size"]["height_inches"],
            ", ".join(dna.get("layouts", [])[:6]),
            logo_str,
            "-" * 48,
            p,
        )
