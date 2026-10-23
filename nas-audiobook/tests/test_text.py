from app import text
from app.text import TextOptions, build_chapter, split_sentences, dialogue_segments


def test_abbreviation_and_number_do_not_end_a_sentence():
    assert split_sentences("Anh Nam sống ở TP. Hồ Chí Minh từ năm 2010. Anh làm việc rất chăm chỉ!") == [
        "Anh Nam sống ở TP. Hồ Chí Minh từ năm 2010.",
        "Anh làm việc rất chăm chỉ!",
    ]
    assert split_sentences("Xem mục 3. Nội dung chính nằm ở đây.")[0].startswith("Xem mục 3.")


def test_pause_depends_on_ending_and_never_below_floor():
    assert text.pause_after("Xin chào.") == 480 - 120
    assert text.pause_after("Thật sao?") == 540 - 120
    assert text.pause_after("Rồi thì…") == 600 - 120
    assert text.pause_after("Như sau:") == 380 - 120
    assert text.pause_after("Một, hai,") == 300 - 120
    assert text.pause_after("Không dấu") == 360 - 120


def test_long_sentence_splits_at_commas_within_limits():
    long = ", ".join(["một mệnh đề khá dài cho câu này" for _ in range(14)]) + "."
    parts = split_sentences(long)
    assert len(parts) > 1
    assert all(len(p) <= 400 for p in parts)
    assert " ".join(parts).replace(" ", "") == long.replace(" ", "")


def test_short_fragment_merges_into_next():
    assert split_sentences("Cô hỏi. Anh gật đầu và mỉm cười thật tươi.") == ["Cô hỏi. Anh gật đầu và mỉm cười thật tươi."]


def test_explicit_pause_tag_overrides_previous_clip():
    ch = build_chapter("T", "Một câu thôi. [nghỉ 2s] Câu sau cùng nhé.", TextOptions(False))
    assert [c.pause_ms for c in ch.clips][0] == 2000


def test_pronunciation_whole_word_case_insensitive_longest_first():
    pairs = [("iPhone 15", "ai phôn mười lăm"), ("AI", "ây ai")]
    assert text.apply_pronunciations("Dùng iphone 15 và AI, không phải MAIL.", sorted(pairs, key=lambda p: -len(p[0]))) == \
        "Dùng ai phôn mười lăm và ây ai, không phải MAIL."


def test_clean_strips_urls_footnotes_bullets():
    assert text.clean("Xem https://x.com/a tại đây¹ [12] • hết") == "Xem tại đây, hết"


def test_chapter_title_clip_and_final_pause():
    ch = build_chapter("Chương 1", "Hôm nay trời đẹp lắm. Chúng ta đi chơi nhé.\nĐoạn hai có câu này.", TextOptions(True))
    assert ch.clips[0].text == "Chương 1" and ch.clips[0].pause_ms == 1300
    assert ch.clips[-1].pause_ms == 1000
    para_end = [c for c in ch.clips if c.text.startswith("Chúng ta")][0]
    assert para_end.pause_ms == 720 - 120


def test_title_not_read_when_disabled():
    ch = build_chapter("Chương 1", "Một câu thôi nhé.", TextOptions(False))
    assert [c.text for c in ch.clips] == ["Một câu thôi nhé."]


def test_dialogue_segments_quotes_dash_and_short_terms():
    assert dialogue_segments("“Em có khỏe không?” cô hỏi.") == [("Em có khỏe không?", True), (" cô hỏi.", False)]
    assert dialogue_segments("- Anh đi đâu về đấy, cô hỏi.") == [("Anh đi đâu về đấy, cô hỏi.", True)]
    assert dialogue_segments("Anh gọi nó là “sách” thôi.") == [("Anh gọi nó là “sách” thôi.", False)]


def test_dialogue_flags_only_when_enabled():
    para = "Anh nói: “Hôm nay tôi rất vui vẻ.” Rồi anh cười."
    off = build_chapter("T", para, TextOptions(False, dialogue=False))
    on = build_chapter("T", para, TextOptions(False, dialogue=True))
    assert not any(c.dialog for c in off.clips)
    assert [(c.text, c.dialog) for c in on.clips] == [("Anh nói:", False), ("Hôm nay tôi rất vui vẻ.", True), ("Rồi anh cười.", False)]


def test_single_capital_initial_does_not_end_a_sentence():
    assert split_sentences("Dr. Blair T. Spalding viết cuốn sách này.") == ["Dr. Blair T. Spalding viết cuốn sách này."]
