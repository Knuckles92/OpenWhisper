"""Read native PDF/Word exports back while forbidding GUI imports."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap


def _native(source, folder):
    environment = os.environ.copy()
    environment.update(OPENWHISPER_DESKTOP_RUNTIME="native", CAPTURE_FOLDER=str(folder))
    guard = """
import os
from pathlib import Path
import sys
class NoGui:
    def find_spec(self, fullname, *_args):
        if fullname.split('.')[0] in ('PyQt6', 'ui_qt'):
            raise AssertionError('GUI import: ' + fullname)
sys.meta_path.insert(0, NoGui())
folder = Path(os.environ['CAPTURE_FOLDER'])
"""
    result = subprocess.run(
        [sys.executable, "-c", guard + textwrap.dedent(source)],
        cwd=Path(__file__).resolve().parents[1], env=environment,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_walkthrough_exports_preserve_unicode_images_captions_and_settings(tmp_path):
    _native("""
        from datetime import datetime
        from PIL import Image
        from docx import Document
        from pypdf import PdfReader
        from services.walkthrough.document import DocumentFormat, Section, Walkthrough, write_pdf
        from services.walkthrough.word import write_docx
        Image.new('RGB', (800, 450), '#3060c0').save(folder / 'shot.png')
        walkthrough = Walkthrough(datetime(2026, 10, 20, 14, 32), 80, (
            Section('Café Привет — first paragraph.\\n\\nSecond paragraph.', folder / 'shot.png', 4),
            Section('Done.'),
        ))
        pdf = PdfReader(write_pdf(walkthrough, folder, page_size='letter'))
        text = ' '.join(' '.join(page.extract_text().split()) for page in pdf.pages)
        assert 'Café Привет — first paragraph.' in text
        assert 'Screenshot 1 · 0:04' in text and 'Second paragraph.' in text and 'Done.' in text
        assert sum(len(page.images) for page in pdf.pages) == 1
        assert tuple(float(n) for n in pdf.pages[0].mediabox[2:]) == (612, 792)
        assert pdf.metadata.creator == 'OpenWhisper'
        assert pdf.metadata.title == 'Walkthrough · Tue, Oct 20, 2026, 2:32 PM'
        word = Document(write_docx(walkthrough, folder, page_size='letter'))
        paragraphs = {p.text: p for p in word.paragraphs}
        assert len(word.inline_shapes) == 1
        assert paragraphs['Second paragraph.'].paragraph_format.keep_with_next
        assert paragraphs['Screenshot 1 · 0:04'].paragraph_format.keep_with_next is False
        assert word.sections[0].page_width.pt == 612
        assert word.core_properties.author == ''
        bare = DocumentFormat(title='Bug report', details=False, about=False, captions=False)
        pdf = PdfReader(write_pdf(walkthrough, folder, document_format=bare, page_size='a4'))
        text = ' '.join(page.extract_text() for page in pdf.pages)
        assert 'Bug report' in text and 'Screenshot 1' not in text and 'of speech' not in text
        word = Document(write_docx(walkthrough, folder, document_format=bare))
        assert [p.text for p in word.paragraphs if p.text] == [
            'Bug report', 'Café Привет — first paragraph.', 'Second paragraph.', 'Done.'
        ]
    """, tmp_path)


def test_markdown_preserves_structure_links_look_and_only_local_images(tmp_path):
    _native('''
        from PIL import Image
        from docx import Document
        from docx.oxml.ns import qn
        from pypdf import PdfReader
        from services.walkthrough.document import Look, write_markdown_pdf
        from services.walkthrough.word import write_markdown_docx
        Image.new('RGB', (400, 240), 'blue').save(folder / 'shot.png')
        outside = folder.parent / 'outside.png'
        Image.new('RGB', (20, 20), 'red').save(outside)
        try:
            (folder / 'escape.png').symlink_to(outside)
        except OSError:
            pass
        markdown = """# Changing the backend
        
        Open **Settings**, *then* follow [the guide](https://example.com/guide).
        
        ![The menu](shot.png)
        
        ## Steps
        
        - Pick one
          - Nested step
        - Save it
        
        3. Third step
        
        > Restart after.
        
        ```python
        backend = "remote"
          keep_indentation = True
        ```
        
        | Name | Value |
        |---|---|
        | speed | fast |
        
        ![Remote](https://example.com/secret.png)
        ![Outside](../outside.png)
        ![Symlink](escape.png)
        ![Missing](missing.png)
        
        <script>alert("literal HTML")</script>
        """
        look = Look(text_color='#203040', heading_color='#5b2a86', font='Not an installed font')
        path = write_markdown_docx(markdown, folder / 'write-up.docx', folder=folder, title='Backend', look=look)
        word = Document(path)
        paragraphs = {p.text: p for p in word.paragraphs if p.text}
        assert paragraphs['Changing the backend'].style.name == 'Heading 1'
        assert paragraphs['Changing the backend'].runs[0].font.color.rgb == (0x5b, 0x2a, 0x86)
        assert paragraphs['Changing the backend'].runs[0].bold
        body = paragraphs['Open Settings, then follow the guide.']
        assert [link.address for link in body.hyperlinks] == ['https://example.com/guide']
        assert any(run.bold for run in body.runs if run.text == 'Settings')
        assert any(run.italic for run in body.runs if run.text == 'then')
        assert paragraphs['•\\tPick one'].paragraph_format.first_line_indent < 0
        assert paragraphs['•\\tNested step'].paragraph_format.left_indent > paragraphs['•\\tPick one'].paragraph_format.left_indent
        assert '3.\\tThird step' in paragraphs
        assert paragraphs['Restart after.']._p.pPr.find(qn('w:pBdr')) is not None
        code = paragraphs['backend = "remote"\\n  keep_indentation = True']
        assert code._p.pPr.find(qn('w:shd')).get(qn('w:fill')) == 'F5F1EB'
        assert code.runs[0].font.name == 'IBM Plex Mono'
        assert [[cell.text for cell in row.cells] for row in word.tables[0].rows] == [['Name', 'Value'], ['speed', 'fast']]
        assert len(word.inline_shapes) == 1
        assert '<script>alert("literal HTML")</script>' in paragraphs
        pdf = PdfReader(write_markdown_pdf(markdown, folder / 'write-up.pdf', folder=folder, title='Backend', look=look))
        text = ' '.join(page.extract_text() for page in pdf.pages)
        assert 'Changing the backend' in text and 'literal HTML' in text and 'backend = "remote"' in text
        assert sum(len(page.images) for page in pdf.pages) == 1
        links = [annotation.get_object().get('/A', {}).get('/URI') for page in pdf.pages for annotation in page.get('/Annots', [])]
        assert 'https://example.com/guide' in links
    ''', tmp_path)


def test_long_walkthrough_paginates_without_losing_words_or_screenshot(tmp_path):
    _native("""
        from datetime import datetime
        from PIL import Image
        from pypdf import PdfReader
        from services.walkthrough.document import Section, Walkthrough, write_pdf
        Image.new('RGB', (900, 1200), 'green').save(folder / 'shot.png')
        walkthrough = Walkthrough(datetime(2026, 10, 20), 600, (
            Section('An explanation that keeps going. ' * 160, folder / 'shot.png', 300),
            Section('Finished.'),
        ))
        pdf = PdfReader(write_pdf(walkthrough, folder, page_size='letter'))
        text = ' '.join(' '.join(page.extract_text().split()) for page in pdf.pages)
        assert len(pdf.pages) > 1
        assert text.count('An explanation that keeps going.') == 160
        assert text.index('Screenshot 1 · 5:00') < text.index('Finished.')
        assert sum(len(page.images) for page in pdf.pages) == 1
    """, tmp_path)


def test_saved_automatic_and_explicit_paper_settings_work_without_qt(tmp_path):
    _native("""
        from unittest.mock import patch
        from pypdf import PdfReader
        from services.walkthrough.finish import document_settings
        from services.walkthrough.document import write_markdown_pdf
        from services.walkthrough.portable_document import _page_size
        _format, paper = document_settings({})
        assert paper == 'auto'
        with patch('locale.getlocale', return_value=('en_US', 'UTF-8')):
            assert _page_size(paper) == (612, 792)
            path = write_markdown_pdf('Words.', folder / 'automatic.pdf', folder=folder, title='Automatic', page_size=paper)
            assert float(PdfReader(path).pages[0].mediabox.width) == 612
        with patch('locale.getlocale', return_value=('de_DE', 'UTF-8')):
            assert round(_page_size('system')[0]) == 595
        assert _page_size('letter') == (612, 792)
        assert round(_page_size('a4')[0]) == 595
    """, tmp_path)


def test_failed_writers_preserve_previous_exports_and_remove_partial_files(tmp_path):
    _native("""
        from unittest.mock import patch
        from services.walkthrough.document import write_markdown_pdf
        from services.walkthrough.word import write_markdown_docx
        previous_pdf, previous_docx = folder / 'previous.pdf', folder / 'previous.docx'
        previous_pdf.write_bytes(b'previous PDF')
        previous_docx.write_bytes(b'previous Word')
        def fail_pdf(self, _story):
            Path(self.filename).write_bytes(b'partial PDF')
            raise OSError('disk write failed')
        def fail_word(self, path):
            Path(path).write_bytes(b'partial Word')
            raise OSError('disk write failed')
        with patch('reportlab.platypus.BaseDocTemplate.build', fail_pdf):
            try:
                write_markdown_pdf('Words.', previous_pdf, folder=folder, title='Title')
            except OSError:
                pass
            else:
                raise AssertionError('failure was swallowed')
        with patch('docx.document.Document.save', fail_word):
            try:
                write_markdown_docx('Words.', previous_docx, folder=folder, title='Title')
            except OSError:
                pass
            else:
                raise AssertionError('failure was swallowed')
        assert previous_pdf.read_bytes() == b'previous PDF'
        assert previous_docx.read_bytes() == b'previous Word'
        assert sorted(p.name for p in folder.iterdir()) == ['previous.docx', 'previous.pdf']
    """, tmp_path)


def test_native_exports_work_on_concurrent_worker_threads(tmp_path):
    _native("""
        from concurrent.futures import ThreadPoolExecutor
        from docx import Document
        from pypdf import PdfReader
        from services.walkthrough.document import write_markdown_pdf
        from services.walkthrough.word import write_markdown_docx
        def render(index):
            title = f'Worker {index}'
            pdf = write_markdown_pdf('# ' + title, folder / f'{index}.pdf', folder=folder, title=title)
            word = write_markdown_docx('# ' + title, folder / f'{index}.docx', folder=folder, title=title)
            assert title in PdfReader(pdf).pages[0].extract_text()
            assert Document(word).paragraphs[0].text == title
        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(render, range(3)))
        assert not any(name.startswith(('PyQt6', 'ui_qt')) for name in sys.modules)
    """, tmp_path)
