"""The call language only changes on a substantial utterance in a language Riya speaks."""

from app.lang.languages import LanguageTracker, has_foreign_script


def test_garbled_mixed_script_transcript_does_not_switch_to_gujarati():
    t = LanguageTracker("hi")
    assert t.observe("અ ಆ ಸತ್ಯ", "gu-IN") == "hi"
    assert t.observe("17 से ਅੱਸੀ ਲੱਖ ਮੈਂ", "pa-IN") == "hi"


def test_a_single_word_does_not_switch():
    t = LanguageTracker("hi")
    assert t.observe("હા", "gu-IN") == "hi"


def test_a_real_marathi_sentence_still_switches():
    t = LanguageTracker("hi")
    assert t.observe("मला दोन बीएचके पाहिजे आहे", "mr-IN") == "mr"


def test_foreign_script_detection():
    assert has_foreign_script("ಸತ್ಯ") and has_foreign_script("ਲੱਖ") and has_foreign_script("ଆପଣ")
    assert not has_foreign_script("मुझे 2 BHK चाहिए") and has_foreign_script("મને જોઈએ")


def test_an_english_word_in_a_hindi_sentence_does_not_switch_to_english():
    t = LanguageTracker("hi")
    assert t.observe("Tuesday छे बजे", "en-IN") == "hi"
    assert t.observe("जी पहन दीजिए", "en-IN") == "hi"


def test_a_real_english_sentence_still_switches():
    t = LanguageTracker("hi")
    assert t.observe("can you show me two bedroom flats please", "en-IN") == "en"


def test_two_english_words_do_not_switch_a_hindi_call():
    t = LanguageTracker("hi")
    assert t.observe("Near by", "en-IN") == "hi"
    assert t.observe("near by area में बताओ", "en-IN") == "hi"


def test_a_marathi_letter_in_a_place_name_does_not_switch_a_hindi_call():
    t = LanguageTracker("hi")
    assert t.observe("वाकळ में दिखा दो", "mr-IN") == "hi"
    assert t.observe("बजट नक्की नहीं है मेरा", "mr-IN") == "hi"


def test_real_marathi_still_switches():
    t = LanguageTracker("hi")
    assert t.observe("मला दोन बीएचके पाहिजे आहे", "mr-IN") == "mr"


def test_asking_for_hindi_locks_it():
    t = LanguageTracker("mr")
    assert t.observe("हिंदी में बात करिए पहले") == "hi"
    assert t.observe("मला दोन बीएचके पाहिजे आहे", "mr-IN") == "hi"  # locked
    assert t.observe("please speak English with me") == "en"   # another explicit request
