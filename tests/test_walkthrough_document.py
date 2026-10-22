"""A walkthrough written out as Markdown, a PDF and a JSON manifest.

Screenshots here are small PNGs filled with one colour; the PDF is read back
with QtPdf, whose text runs follow the printed lines.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QLocale, QSize, QSizeF
from PyQt6.QtGui import QColor, QImage, QPageSize
from PyQt6.QtPdf import QPdfDocument

from services.walkthrough import document
from services.walkthrough.document import (
    Section,
    Walkthrough,
    markdown,
    write_manifest,
    write_markdown,
    write_pdf,
)

#: A Tuesday afternoon.
STARTED = datetime(2026, 10, 20, 14, 32, 5)
ABOUT_SHOTLESS = "Spoken notes with screenshots, transcribed by OpenWhisper."
ABOUT = (
    f"{ABOUT_SHOTLESS} Each screenshot comes right after the words that describe it."
)
LETTER = QPageSize(QPageSize.PageSizeId.Letter)
US_UNITS = QLocale.MeasurementSystem.ImperialUSSystem


@pytest.fixture(autouse=True)
def qt_app(_session_qt_application):
    return _session_qt_application


def _png(path: Path, width: int, height: int, color: str = "#3060c0") -> Path:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path))
    return path


def _pdf(path: Path) -> QPdfDocument:
    pdf = QPdfDocument(None)
    assert pdf.load(str(path)) == QPdfDocument.Error.None_
    return pdf


def _pages(path: Path) -> list[str]:
    """Each page's text, with the line breaks of wrapped lines as spaces."""
    pdf = _pdf(path)
    try:
        return [_flat(pdf.getAllText(page).text()) for page in range(pdf.pageCount())]
    finally:
        pdf.close()


def _flat(text: str) -> str:
    return " ".join(text.split())


# --- Markdown ---------------------------------------------------------------


def test_each_screenshot_follows_its_words(tmp_path):
    walkthrough = Walkthrough(STARTED, 136.4, (
        Section("Okay, the update card is grayed out.", tmp_path / "shot-1.png", 41.3),
        Section("Next, this label is stale.", tmp_path / "shot-2.png", 72.6),
        Section("That's it.", None, None),
    ))

    assert markdown(walkthrough, folder=tmp_path) == (
        "# Walkthrough\n"
        "\n"
        "_Tue, Oct 20, 2026, 2:32 PM · 2:16 of speech · 2 screenshots. "
        f"{ABOUT}_\n"
        "\n"
        "Okay, the update card is grayed out.\n"
        "\n"
        "![Screenshot 1 (0:41)](shot-1.png)\n"
        "\n"
        "Next, this label is stale.\n"
        "\n"
        "![Screenshot 2 (1:12)](shot-2.png)\n"
        "\n"
        "That's it.\n"
    )


def test_absolute_links_keep_a_folder_with_spaces_in_one_piece(tmp_path):
    folder = tmp_path / "Big D" / "captures" / "2026-10-20_14-32-05"
    shot = folder / "shot-1.png"
    walkthrough = Walkthrough(STARTED, 20, (Section("Look here.", shot, 12.0),))

    assert markdown(walkthrough, folder=folder, absolute_images=True) == (
        "# Walkthrough\n"
        "\n"
        "_Tue, Oct 20, 2026, 2:32 PM · 0:20 of speech · 1 screenshot. "
        f"{ABOUT}_\n"
        "\n"
        "Look here.\n"
        "\n"
        f"![Screenshot 1 (0:12)](<{shot}>)\n"
    )
    # The platform's own separators, as a terminal agent would type it.
    assert f"{os.sep}Big D{os.sep}captures{os.sep}" in str(shot)


def test_a_relative_link_with_a_space_is_bracketed_too(tmp_path):
    shot = tmp_path / "shots" / "shot 1.png"
    walkthrough = Walkthrough(STARTED, 20, (Section("", shot, 3.0),))

    text = markdown(walkthrough, folder=tmp_path)

    assert text.endswith("\n![Screenshot 1 (0:03)](<shots/shot 1.png>)\n")


def test_a_screenshot_outside_the_folder_keeps_its_full_path(tmp_path):
    shot = tmp_path / "elsewhere" / "shot-1.png"
    walkthrough = Walkthrough(STARTED, 20, (Section("", shot, 3.0),))

    text = markdown(walkthrough, folder=tmp_path / "capture")

    assert text.endswith(f"\n![Screenshot 1 (0:03)](<{shot}>)\n")


@pytest.mark.parametrize(
    "spoken, written",
    [
        ("# one thing", "\\# one thing"),
        ("#hashtag", "\\#hashtag"),
        ("> quoted", "\\> quoted"),
        ("- dash", "\\- dash"),
        ("+ plus", "\\+ plus"),
        ("* star", "\\* star"),
        ("1. First", "1\\. First"),
        ("12) Twelfth", "12\\) Twelfth"),
        ("1.5 seconds in", "1.5 seconds in"),
        ("Okay # not a heading - not a list", "Okay # not a heading - not a list"),
        (
            "Line one\n- line two\n  3. line three",
            "Line one\n\\- line two\n  3\\. line three",
        ),
    ],
)
def test_speech_never_turns_into_markdown_structure(tmp_path, spoken, written):
    walkthrough = Walkthrough(STARTED, 10, (Section(spoken),))

    assert markdown(walkthrough, folder=tmp_path).endswith(f"_\n\n{written}\n")


@pytest.mark.parametrize(
    "shots, count, about",
    [
        (0, "no screenshots", ABOUT_SHOTLESS),
        (1, "1 screenshot", ABOUT),
        (3, "3 screenshots", ABOUT),
    ],
)
def test_the_meta_line_counts_screenshots(tmp_path, shots, count, about):
    sections = tuple(
        Section(f"Words {number}.", tmp_path / f"shot-{number}.png", float(number))
        for number in range(1, shots + 1)
    ) + (Section("The end."),)

    lines = markdown(Walkthrough(STARTED, 75, sections), folder=tmp_path).split("\n")

    meta = f"Tue, Oct 20, 2026, 2:32 PM · 1:15 of speech · {count}"
    assert lines[2] == f"_{meta}. {about}_"


@pytest.mark.parametrize(
    "seconds, clock",
    [
        (0, "0:00"),
        (9.99, "0:09"),
        (136.4, "2:16"),
        (3599.9, "59:59"),
        (3600, "1:00:00"),
        (3725, "1:02:05"),
        (-1, "0:00"),
    ],
)
def test_times_read_as_minutes_and_seconds_or_hours(tmp_path, seconds, clock):
    shot = tmp_path / "shot-1.png"
    walkthrough = Walkthrough(STARTED, seconds, (Section("Look.", shot, seconds),))

    text = markdown(walkthrough, folder=tmp_path)

    assert f" · {clock} of speech · " in text
    assert f"![Screenshot 1 ({clock})](shot-1.png)" in text


def test_empty_sections_are_dropped_and_screenshots_numbered_in_order(tmp_path):
    walkthrough = Walkthrough(STARTED, 30, (
        Section("   "),
        Section("", tmp_path / "shot-a.png", 5.0),
        Section("\n"),
        Section("Then this.", tmp_path / "shot-b.png", None),
        Section(""),
    ))

    body = markdown(walkthrough, folder=tmp_path).split("_\n\n", 1)[1]

    assert body == (
        "![Screenshot 1 (0:05)](shot-a.png)\n"
        "\n"
        "Then this.\n"
        "\n"
        "![Screenshot 2](shot-b.png)\n"
    )


def test_markdown_file_is_utf8_with_lf_line_endings(tmp_path):
    spoken = "Café, line one\r\nline two\r\rafter a blank line"
    shot = tmp_path / "shot-1.png"
    walkthrough = Walkthrough(STARTED, 30, (Section(spoken, shot, 3.0),))

    path = write_markdown(walkthrough, tmp_path)

    data = path.read_bytes()
    assert path == tmp_path / "walkthrough.md"
    assert b"\r" not in data
    assert data.decode("utf-8") == markdown(walkthrough, folder=tmp_path)
    assert "Café, line one\nline two\n\nafter a blank line\n" in data.decode("utf-8")


# --- Manifest ---------------------------------------------------------------


def test_manifest_keeps_the_sections_for_a_later_write_up(tmp_path):
    walkthrough = Walkthrough(STARTED, 136.4, (
        Section(" Okay, look here. ", tmp_path / "shot-1.png", 41.3),
        Section(""),
        Section("That's it.", None, None),
    ))

    path = write_manifest(walkthrough, tmp_path)

    manifest = json.loads(path.read_text(encoding="utf-8"))
    started = datetime.fromisoformat(manifest["started_at"])
    assert path == tmp_path / "walkthrough.json"
    assert b"\r" not in path.read_bytes()
    assert manifest["version"] == 1
    assert started.tzinfo is not None and started.replace(tzinfo=None) == STARTED
    assert manifest["duration_s"] == 136.4
    assert manifest["sections"] == [
        {"text": "Okay, look here.", "shot": "shot-1.png", "kept_at_s": 41.3},
        {"text": "That's it.", "shot": None, "kept_at_s": None},
    ]


# --- PDF --------------------------------------------------------------------


def test_pdf_holds_the_words_and_captions_as_text(tmp_path):
    shot = _png(tmp_path / "shot-1.png", 800, 450)
    walkthrough = Walkthrough(STARTED, 136.4, (
        Section("The update card is grayed out.", shot, 41.3),
        Section("That's it.", None, None),
    ))

    path = write_pdf(walkthrough, tmp_path, page_size=LETTER)

    assert path == tmp_path / "walkthrough.pdf"
    (page,) = _pages(path)
    expected = [
        "Walkthrough",
        "Tue, Oct 20, 2026, 2:32 PM · 2:16 of speech · 1 screenshot",
        ABOUT,
        "The update card is grayed out.",
        "Screenshot 1 · 0:41",
        "That's it.",
    ]
    positions = [page.find(text) for text in expected]
    assert -1 not in positions, page
    assert positions == sorted(positions)


def test_pdf_metadata_names_the_walkthrough(tmp_path):
    walkthrough = Walkthrough(STARTED, 5, (Section("Hi."),))

    path = write_pdf(walkthrough, tmp_path, page_size=LETTER)

    pdf = _pdf(path)
    try:
        title = pdf.metaData(QPdfDocument.MetaDataField.Title)
        creator = pdf.metaData(QPdfDocument.MetaDataField.Creator)
    finally:
        pdf.close()
    assert title == "Walkthrough · Tue, Oct 20, 2026, 2:32 PM"
    assert creator == "OpenWhisper"


def test_a_section_that_would_split_starts_the_next_page(tmp_path):
    # Each portrait screenshot takes most of a page, so the second section
    # cannot fit under the first: its words move along with its caption.
    walkthrough = Walkthrough(STARTED, 90, (
        Section("First words.", _png(tmp_path / "shot-1.png", 900, 1200), 10.0),
        Section("Second words.", _png(tmp_path / "shot-2.png", 900, 1200), 50.0),
        Section("Last words.", None, None),
    ))

    pages = _pages(write_pdf(walkthrough, tmp_path, page_size=LETTER))

    assert len(pages) == 2
    assert "First words." in pages[0] and "Screenshot 1 · 0:10" in pages[0]
    assert "Second words." not in pages[0] and "Screenshot 2" not in pages[0]
    assert "Second words." in pages[1] and "Screenshot 2 · 0:50" in pages[1]
    assert "Last words." in pages[1]


def test_a_screenshot_without_words_moves_whole(tmp_path):
    # Too tall for the room left under the first section, the second
    # screenshot starts the next page as wide as the text, not shrunk.
    walkthrough = Walkthrough(STARTED, 90, (
        Section("First words.", _png(tmp_path / "shot-1.png", 1344, 900), 10.0),
        Section("", _png(tmp_path / "shot-2.png", 1600, 900, "#30a060"), 20.0),
    ))

    path = write_pdf(walkthrough, tmp_path, page_size=LETTER)

    pages = _pages(path)
    assert len(pages) == 2 and "Screenshot 2 · 0:20" in pages[1]
    # Letter less 0.75 inch margins leaves 7 inches of text width.
    assert _widest(path, 1, "#30a060") == pytest.approx(7 * 72, abs=3)


def test_a_screenshot_without_words_is_never_shrunk(tmp_path):
    # Wherever the first section ends, the screenshot after it starts its
    # own section: it moves on whole, or stays whole, but never shrinks.
    lone = _png(tmp_path / "lone.png", 1600, 900)
    for height in range(300, 460, 6):
        first = _png(tmp_path / f"first-{height}.png", 1344, 2 * height)
        sections = (Section("First words.", first, 1.0), Section("", lone, 2.0))
        composer = document._Composer(QSizeF(672, 921), 96)

        _doc, frames = composer.compose(Walkthrough(STARTED, 9, sections))

        assert frames[1].size() == QSizeF(672, 378), height


def _widest(path: Path, page: int, color: str) -> int:
    """The widest row of ``color`` on ``page``, in points."""
    pdf = _pdf(path)
    try:
        size = pdf.pagePointSize(page)
        image = pdf.render(page, QSize(round(size.width()), round(size.height())))
    finally:
        pdf.close()
    target = QColor(color)
    return max(
        sum(1 for x in range(image.width()) if image.pixelColor(x, y) == target)
        for y in range(0, image.height(), 4)
    )


def test_the_first_section_stays_on_the_first_page_with_the_title(tmp_path):
    # Its words and a portrait screenshot overflow the page under the
    # header. Moving it would leave the title alone, so the screenshot
    # shrinks to fit.
    words = "Some context before the first screenshot. " * 20
    shot = _png(tmp_path / "shot-1.png", 900, 1200)
    sections = (Section(words, shot, 61.0), Section("Done."))
    walkthrough = Walkthrough(STARTED, 90, sections)

    pages = _pages(write_pdf(walkthrough, tmp_path, page_size=LETTER))

    assert "Walkthrough" in pages[0]
    assert pages[0].count("Some context before the first screenshot.") == 20
    assert "Screenshot 1 · 1:01" in pages[0]


def test_a_section_longer_than_a_page_flows_on(tmp_path):
    words = "An explanation that keeps going. " * 150
    walkthrough = Walkthrough(STARTED, 600, (
        Section("Short start.", _png(tmp_path / "shot-1.png", 900, 500), 5.0),
        Section(words, _png(tmp_path / "shot-2.png", 900, 1200), 300.0),
        Section("Done."),
    ))

    pages = _pages(write_pdf(walkthrough, tmp_path, page_size=LETTER))

    text = " ".join(pages)
    assert len(pages) >= 2
    assert text.count("An explanation that keeps going.") == 150
    assert text.index("Screenshot 2 · 5:00") < text.index("Done.")


def test_write_pdf_works_on_a_worker_thread(tmp_path):
    shot = _png(tmp_path / "shot-1.png", 640, 360)
    sections = (Section("Written by a worker.", shot, 5.0),)
    walkthrough = Walkthrough(STARTED, 20, sections)
    outcome = {}

    def write():
        try:
            outcome["path"] = write_pdf(walkthrough, tmp_path, page_size=LETTER)
        except Exception as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=write)
    worker.start()
    worker.join(timeout=60)

    assert not worker.is_alive()
    assert "error" not in outcome, outcome.get("error")
    (page,) = _pages(outcome["path"])
    assert "Written by a worker." in page and "Screenshot 1 · 0:05" in page


def test_a_walkthrough_without_screenshots_is_text_only(tmp_path):
    sections = (Section("First paragraph.\n\nSecond one."),)
    walkthrough = Walkthrough(STARTED, 52, sections)

    (page,) = _pages(write_pdf(walkthrough, tmp_path, page_size=LETTER))

    assert "0:52 of speech · no screenshots" in page
    assert "First paragraph." in page and "Second one." in page
    assert "Each screenshot comes right after" not in page


def test_an_unreadable_screenshot_keeps_its_words_and_caption(tmp_path, caplog):
    broken = tmp_path / "shot-1.png"
    broken.write_bytes(b"not a png")
    walkthrough = Walkthrough(STARTED, 20, (Section("These words stay.", broken, 7.0),))

    (page,) = _pages(write_pdf(walkthrough, tmp_path, page_size=LETTER))

    assert "These words stay." in page and "Screenshot 1 · 0:07" in page
    assert "Could not read screenshot" in caplog.text


def test_an_unwritable_pdf_raises(tmp_path):
    walkthrough = Walkthrough(STARTED, 5, (Section("Hi."),))

    with pytest.raises(OSError):
        write_pdf(walkthrough, tmp_path / "missing", page_size=LETTER)


def _locale_measuring(measurement):
    """A QLocale stand-in whose system locale measures in ``measurement``."""

    class Locale:
        MeasurementSystem = QLocale.MeasurementSystem

        @staticmethod
        def system():
            return SimpleNamespace(measurementSystem=lambda: measurement)

    return Locale


@pytest.mark.parametrize(
    "measurement, page_size, points",
    [
        (US_UNITS, None, (612, 792)),
        (QLocale.MeasurementSystem.MetricSystem, None, (595, 842)),
        (QLocale.MeasurementSystem.ImperialUKSystem, None, (595, 842)),
        (US_UNITS, QPageSize.PageSizeId.A4, (595, 842)),
    ],
)
def test_paper_follows_the_locale_unless_given(
    tmp_path, monkeypatch, measurement, page_size, points
):
    monkeypatch.setattr(document, "QLocale", _locale_measuring(measurement))
    size = QPageSize(page_size) if page_size is not None else None
    walkthrough = Walkthrough(STARTED, 5, (Section("Hi."),))

    path = write_pdf(walkthrough, tmp_path, page_size=size)

    pdf = _pdf(path)
    try:
        page = pdf.pagePointSize(0)
    finally:
        pdf.close()
    assert (round(page.width()), round(page.height())) == points
