"""Bundled fallback/shaping coverage for representative exported scripts."""

from tests.test_walkthrough_portable_document import _native


def test_multilingual_pdf_text_and_font_fallbacks_without_gui(tmp_path):
    _native("""
        class NoFontTools:
            def find_spec(self, fullname, *_args):
                if fullname.split('.')[0] == 'fontTools':
                    raise ModuleNotFoundError('Build-time font tools unavailable')
        sys.meta_path.insert(0, NoFontTools())
        from pathlib import Path
        import pypdfium2 as pdfium
        from pypdf import PdfReader
        from services.walkthrough.document import write_markdown_pdf
        sources = [
            '日本語の説明とスクリーンショット。',
            '中文说明和屏幕截图。',
            'مرحبا بالعالم هذا اختبار اللغة العربية',
            'नमस्ते दुनिया यह हिन्दी भाषा का परीक्षण है',
            'Mixed Office file مرحبا بالعالم and 中文 with हिन्दी text.',
        ]
        path = write_markdown_pdf('# Multilingual export\\n\\n' + '\\n\\n'.join(sources),
            folder / 'multilingual.pdf', folder=folder, title='Multilingual export')
        pdf = pdfium.PdfDocument(path)
        extracted = pdf[0].get_textpage().get_text_range().splitlines()
        assert extracted[0] == 'Multilingual export'
        assert [' '.join(line.split()) for line in extracted[1:]] == sources
        reader = PdfReader(path)
        assert [' '.join(line.split()) for line in reader.pages[0].extract_text().splitlines()[1:]] == sources
        names = [str(font.get_object()['/BaseFont']) for font in reader.pages[0]['/Resources']['/Font'].values()]
        for family in ('Inter', 'NotoSansSC', 'NotoSansArabic', 'NotoSansDevanagari'):
            assert any(family in name for name in names), (family, names)
        rendered = pdf[0].render(scale=2).to_pil()
        rendered.save(folder / 'multilingual.png')
        assert rendered.width > 1000 and rendered.height > 1500
        pixels = rendered.convert('L')
        bands = [pixels.crop((100, top, 1100, top + 35)) for top in (170, 215, 260, 305, 350)]
        assert all(band.getextrema()[0] < 100 for band in bands)
        assert not any(name.startswith('fontTools') for name in sys.modules)
        pdf.close()
    """, tmp_path)


def test_complex_clusters_keep_source_unicode_and_rasterize(tmp_path):
    _native("""
        from pypdf import PdfReader
        import pypdfium2 as pdfium
        from services.walkthrough.document import write_markdown_pdf
        from reportlab.pdfbase.pdfmetrics import getFont
        sources = [
            'السَّلَامُ عَلَيْكُمْ',
            'اللغة العربية مع English 123 هنا.',
            'क्षि त्र ज्ञ श्र हिन्दी कार्यालय में स्वागत है',
            'Office ffi file हिन्दी 中文',
        ]
        for index, source in enumerate(sources):
            path = write_markdown_pdf(source, folder / f'clusters-{index}.pdf',
                folder=folder, title='Complex clusters')
            reader = PdfReader(path)
            if index:
                assert ' '.join(reader.pages[0].extract_text().split()) == source
            else:
                # Readers reorder vowelled RTL clusters differently; the PDF
                # must still map the base glyph to its complete logical source.
                cmaps = '\\n'.join(font.get_object()['/ToUnicode'].get_data().decode()
                    for font in reader.pages[0]['/Resources']['/Font'].values()
                    if '/ToUnicode' in font.get_object())
                assert '0645064F' in cmaps
                assert '0633064E0651' in cmaps
            pdf = pdfium.PdfDocument(path)
            image = pdf[0].render(scale=2).to_pil()
            image.save(folder / f'complex-clusters-{index}.png')
            assert image.convert('L').getextrema()[0] < 100
            pdf.close()
        for name in ('OWNotoArabic', 'OWNotoDevanagari'):
            font = getFont(name)
            assert font.shapable and font._source_text
            assert any(len(source) > 1 for source in font._source_text.values())
    """, tmp_path)


def test_multilingual_sections_paginate_with_every_source_character(tmp_path):
    _native("""
        from datetime import datetime
        from PIL import Image
        from pypdf import PdfReader
        from services.walkthrough.document import Section, Walkthrough, write_pdf
        Image.new('RGB', (800, 450), 'navy').save(folder / 'shot.png')
        japanese = '日本語の説明とスクリーンショット。' * 100
        hindi = 'नमस्ते दुनिया यह हिन्दी भाषा का परीक्षण है। ' * 80
        arabic = 'مرحبا بالعالم هذا اختبار اللغة العربية ' * 80
        walkthrough = Walkthrough(datetime(2026, 10, 20), 600, (
            Section(japanese, folder / 'shot.png', 10), Section(hindi), Section(arabic),
        ))
        reader = PdfReader(write_pdf(walkthrough, folder, page_size='letter'))
        assert len(reader.pages) >= 3
        text = ''.join(page.extract_text() for page in reader.pages)
        compact = ''.join(text.split())
        for source in (japanese, hindi, arabic):
            assert ''.join(source.split()) in compact
        assert sum(len(page.images) for page in reader.pages) == 1
        assert 'Screenshot 1' in text
    """, tmp_path)


def test_fallback_font_assets_are_bounded_licensed_and_match_pinned_sources(tmp_path):
    _native("""
        import hashlib
        import json
        from config import bundle_root
        from services.walkthrough.portable_document import _fonts, _font_for_character
        root = Path(bundle_root()) / 'native' / 'assets' / 'fonts'
        manifest = json.loads((root / 'Noto-SOURCES.json').read_text())
        assert len(manifest['revision']) == 40
        assert sum(item['bytes'] for item in manifest['fonts']) < 12 * 1024 * 1024
        for item in manifest['fonts']:
            path = root / item['file']
            assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
            assert 'SIL OPEN FONT LICENSE' in path.with_name(path.stem.replace('-Regular', '') + '-LICENSE.txt').read_text()
        _fonts()
        assert _font_for_character('A', 'OWInter') == 'OWInter'
        assert _font_for_character('Ж', 'OWInter') == 'OWInter'
        assert _font_for_character('中', 'OWInter') == 'OWNotoSC'
        assert _font_for_character('日', 'OWInter') == 'OWNotoSC'
        assert _font_for_character('م', 'OWInter') == 'OWNotoArabic'
        assert _font_for_character('ह', 'OWInter') == 'OWNotoDevanagari'
    """, tmp_path)
