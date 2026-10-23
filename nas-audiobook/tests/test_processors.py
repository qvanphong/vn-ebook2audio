import pytest

from app import books
from app.books import PROCESSORS, ParamError, default_selection, parse
from app.ingest import IngestError, UnsupportedFormat, build_book, load_book
from app.model import Plan
from app.text import TextOptions
from conftest import LONG


def titles(path, processor, **params):
    return [s.title for s in parse(path, processor, params).sections]


def test_registry_lists_every_processor_with_label_and_description():
    assert list(PROCESSORS) == ["auto", "epub-toc", "epub-spine", "epub-headings", "text-pattern", "fixed-size"]
    assert all(p.label and p.description for p in PROCESSORS.values())


# --- EPUB: table of contents -------------------------------------------------

def test_toc_reads_text_in_divs_and_br_and_titles_from_toc(toc_epub):
    body = f"<p><strong>Chương I</strong></p><div>{LONG}<br/>Dòng thứ hai của đoạn.<br/>Dòng thứ ba nữa.</div>"
    path = toc_epub({"text/c1.html": body}, [("Chương I", "text/c1.html")])
    section = parse(path, "epub-toc").sections[0]
    assert section.title == "Chương I"
    assert "Dòng thứ hai của đoạn." in section.text.split("\n") and section.text.count("Chương I") == 0


def test_toc_titles_win_over_headings(toc_epub):
    files = {"a.html": f"<h1>Tiêu đề trong file</h1><p>{LONG}</p>", "b.html": f"<p>{LONG}</p>"}
    path = toc_epub(files, [("Chương Một", "a.html"), ("Chương Hai", "b.html")])
    assert titles(path, "epub-toc") == ["Chương Một", "Chương Hai"]


def test_toc_cuts_one_file_at_anchors(toc_epub):
    body = (f'<h2 id="c1">Một</h2><p>{LONG}</p><h2 id="c2">Hai</h2><p>Nội dung hai. {LONG}</p>'
            f'<h2 id="c3">Ba</h2><p>Nội dung ba. {LONG}</p>')
    path = toc_epub({"all.html": body}, [("Một", "all.html#c1"), ("Hai", "all.html#c2"), ("Ba", "all.html#c3")])
    sections = parse(path, "epub-toc").sections
    assert [s.title for s in sections] == ["Một", "Hai", "Ba"]
    assert "Nội dung hai" in sections[1].text and "Nội dung ba" not in sections[1].text


def test_toc_unlisted_file_continues_the_previous_chapter(toc_epub):
    files = {"c1.html": f"<p>{LONG}</p>", "c1b.html": "<p>Phần tiếp theo của chương một. " + LONG + "</p>"}
    sections = parse(toc_epub(files, [("Chương 1", "c1.html")]), "epub-toc").sections
    assert len(sections) == 1 and "Phần tiếp theo" in sections[0].text


def test_toc_drops_running_header_above_the_title_echo(toc_epub):
    body = f"<p>Tác giả</p><p>TÊN SÁCH</p><p>Chương I</p><p>Lời mở đầu</p><p>{LONG}</p>"
    text = parse(toc_epub({"c.html": body}, [("Chương I", "c.html")]), "epub-toc").sections[0].text
    assert text.split("\n")[0] == "Lời mở đầu"


def test_toc_processor_reports_when_there_is_no_toc(toc_epub):
    parsed = parse(toc_epub({"a.html": f"<p>{LONG}</p>"}, []), "epub-toc")
    assert parsed.sections == [] and "no table of contents" in parsed.note


# --- EPUB: spine and headings ---------------------------------------------------

def test_spine_one_chapter_per_file_named_from_heading_then_toc_then_number(toc_epub):
    files = {"a.html": f"<h1>Phần A</h1><p>{LONG}</p>", "b.html": f"<p>{LONG}</p>", "c.html": f"<p>{LONG}</p>"}
    path = toc_epub(files, [("Từ mục lục", "b.html")])
    assert titles(path, "epub-spine") == ["Phần A", "Từ mục lục", "Section 3"]


def test_headings_levels_choose_what_starts_a_chapter(toc_epub):
    body = f"<h1>Phần một</h1><h2>Chương 1</h2><p>{LONG}</p><h2>Chương 2</h2><p>{LONG}</p>"
    path = toc_epub({"a.html": body}, [])
    assert titles(path, "epub-headings", levels="h1") == ["Phần một"]
    assert titles(path, "epub-headings", levels="h1-h2") == ["Phần một", "Chương 1", "Chương 2"]


def test_headings_keeps_text_before_the_first_heading_as_its_own_section(toc_epub):
    path = toc_epub({"a.html": f"<p>Lời tựa. {LONG}</p><h1>Một</h1><p>{LONG}</p>"}, [])
    assert titles(path, "epub-headings") == ["(before the first heading)", "Một"]


# --- Text pattern and fixed size ---------------------------------------------------

def test_pattern_finds_plain_paragraph_titles_in_an_epub(toc_epub):
    body = f"<p>Chương 1</p><p>{LONG}</p><p>Chương 2</p><p>{LONG}</p><p>Epilogue</p><p>{LONG}</p>"
    path = toc_epub({"a.html": body}, [])
    got = titles(path, "text-pattern")
    assert got == ["Chương 1", "Chương 2"]
    assert titles(path, "text-pattern", pattern=r"^(chương \d|epilogue)") == ["Chương 1", "Chương 2", "Epilogue"]


def test_pattern_rejects_invalid_regex(toc_epub):
    path = toc_epub({"a.html": f"<p>{LONG}</p>"}, [])
    with pytest.raises(ParamError, match="regular expression"):
        parse(path, "text-pattern", {"pattern": "("})


def test_text_files_use_pattern_and_fixed_size(tmp_path):
    body = "Nội dung rất dài để vượt ngưỡng tối thiểu của một chương. " * 6
    p = tmp_path / "t.txt"
    p.write_text(f"Chương 1\n{body}\nChương 2\n{body}\n", encoding="utf8")
    assert titles(p, "text-pattern") == ["Chương 1", "Chương 2"]
    assert titles(p, "fixed-size", chars="100") == ["Part 1", "Part 2"]
    assert titles(p, "fixed-size", chars="100000") == ["Part 1"]


def test_numeric_and_choice_params_are_validated(tmp_path, toc_epub):
    p = tmp_path / "t.txt"; p.write_text("x")
    with pytest.raises(ParamError):
        parse(p, "fixed-size", {"chars": "0"})
    with pytest.raises(ParamError):
        parse(p, "fixed-size", {"chars": "many"})
    with pytest.raises(ParamError):
        parse(toc_epub({"a.html": f"<p>{LONG}</p>"}, []), "epub-headings", {"levels": "h9"})


def test_epub_only_processors_refuse_text_files(tmp_path):
    p = tmp_path / "t.txt"; p.write_text("x")
    with pytest.raises(ParamError, match="cannot read"):
        parse(p, "epub-toc")
    with pytest.raises(ParamError, match="unknown processor"):
        parse(p, "nope")


def test_applicable_processors_depend_on_the_source(tmp_path, toc_epub):
    txt = tmp_path / "t.txt"; txt.write_text("x")
    assert [p.id for p in books.applicable(books.load_source(txt))] == ["auto", "text-pattern", "fixed-size"]
    epub_ids = [p.id for p in books.applicable(books.load_source(toc_epub({"a.html": f"<p>{LONG}</p>"}, [])))]
    assert epub_ids == list(PROCESSORS)


# --- Auto -------------------------------------------------------------------------

def test_auto_prefers_toc_then_headings_then_pattern_then_fixed(toc_epub, tmp_path):
    two = {"a.html": f"<h1>Một</h1><p>{LONG}</p>", "b.html": f"<h1>Hai</h1><p>{LONG}</p>"}
    with_toc = parse(toc_epub(two, [("Chương A", "a.html"), ("Chương B", "b.html")], name="1.epub"), "auto")
    assert [s.title for s in with_toc.sections] == ["Chương A", "Chương B"] and "table of contents" in with_toc.note
    no_toc = parse(toc_epub(two, [], name="2.epub"), "auto")
    assert [s.title for s in no_toc.sections][-2:] == ["Một", "Hai"] and "headings" in no_toc.note
    plain = {"a.html": f"<p>Chương 1</p><p>{LONG}</p><p>Chương 2</p><p>{LONG}</p>"}
    assert "title lines" in parse(toc_epub(plain, [], name="3.epub"), "auto").note
    flat = tmp_path / "flat.txt"; flat.write_text("\n".join([LONG] * 60))
    assert "Equal-sized" in parse(flat, "auto").note


# --- Selection, plan and build -----------------------------------------------------

def test_tiny_and_contents_sections_are_listed_but_not_selected_by_default(toc_epub):
    files = {"front.html": "<p>Chào mừng</p>", "toc.html": f"<h1>Mục lục</h1><p>{LONG}</p>", "c1.html": f"<p>{LONG}</p>"}
    path = toc_epub(files, [("Chào", "front.html"), ("Mục lục", "toc.html"), ("Chương 1", "c1.html")])
    sections = parse(path, "epub-toc").sections
    assert [s.title for s in sections] == ["Chào", "Mục lục", "Chương 1"]
    assert default_selection(sections) == [2]


def test_plan_selection_restricts_chapters_and_overrides_metadata(toc_epub):
    files = {f"c{i}.html": f"<p>{LONG}</p>" for i in range(3)}
    path = toc_epub(files, [("Một", "c0.html"), ("Hai", "c1.html"), ("Ba", "c2.html")])
    book = load_book(path, TextOptions(True), Plan("epub-toc", selection=(0, 2), title="Tên mới", author="Người khác"))
    assert [c.title for c in book.chapters] == ["Một", "Ba"]
    assert (book.title, book.author) == ("Tên mới", "Người khác")


def test_build_with_nothing_selected_is_an_error(toc_epub):
    path = toc_epub({"c.html": f"<p>{LONG}</p>"}, [("Một", "c.html")])
    with pytest.raises(IngestError):
        build_book(parse(path, "epub-toc"), Plan(selection=()), TextOptions(False))


def test_plan_survives_a_json_round_trip():
    plan = Plan("fixed-size", (("chars", "500"),), (1, 3), "T", None)
    assert Plan.from_json(plan.to_json()) == plan
    assert Plan.from_json(None) == Plan()


def test_vietnamese_text_overrides_a_wrong_english_language_tag(toc_epub):
    path = toc_epub({"c.html": f"<p>{LONG} và những điều đẹp đẽ của buổi sáng yên tĩnh.</p>"}, [("Một", "c.html")])
    assert parse(path, "epub-toc").language == "vi"


def test_unsupported_and_missing_tools(tmp_path, monkeypatch):
    with pytest.raises(UnsupportedFormat):
        parse(tmp_path / "x.xyz")
    monkeypatch.setattr(books.source.shutil, "which", lambda _: None)
    f = tmp_path / "x.mobi"; f.write_bytes(b"x")
    with pytest.raises(IngestError, match="Calibre"):
        parse(f)
    g = tmp_path / "x.pdf"; g.write_bytes(b"x")
    with pytest.raises(IngestError, match="pdftotext"):
        parse(g)
    bad = tmp_path / "bad.epub"; bad.write_bytes(b"not a zip")
    with pytest.raises(IngestError, match="could not open"):
        parse(bad)
