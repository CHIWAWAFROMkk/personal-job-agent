"""Embed a locally installed Chinese TrueType font; never redistribute it."""
import os
from pathlib import Path
from threading import Lock

_lock = Lock()


def resume_pdf_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    name = 'JobAgentEmbeddedCJK'
    with _lock:
        if name in pdfmetrics.getRegisteredFontNames():
            return name
        configured = os.getenv('JOB_AGENT_PDF_FONT', '').strip()
        candidates = [Path(configured)] if configured else [
            Path(os.getenv('WINDIR', 'C:/Windows')) / 'Fonts' / 'simsun.ttc',
            Path('/System/Library/Fonts/STHeiti Light.ttc'),
            Path('/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'),
        ]
        for path in candidates:
            if not path.is_file():
                continue
            try:
                pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=0))
            except Exception:
                continue
            pdfmetrics.registerFontFamily(name, normal=name, bold=name, italic=name, boldItalic=name)
            return name
    raise ValueError('未找到可嵌入的中文字体。请安装宋体或通过 JOB_AGENT_PDF_FONT 指定中文 TrueType 字体后重试。')
