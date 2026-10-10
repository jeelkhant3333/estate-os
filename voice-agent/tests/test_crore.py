"""Hundreds of lakh are spoken as crore."""

from app.lang.spoken import crore_not_lakh


def test_hundred_lakh_is_one_crore():
    assert crore_not_lakh("The 3 BHK is 100 lakh.", "en") == "The 3 BHK is 1 crore."
    assert crore_not_lakh("from 85 lakh to 120 lakhs", "en") == "from 85 lakh to 1 crore 20 lakh"
    assert crore_not_lakh("कीमत 150 लाख है", "hi") == "कीमत 1 करोड़ 50 लाख है"
    assert crore_not_lakh("किंमत १०० लाख आहे", "mr") == "किंमत 1 कोटी आहे"
    assert crore_not_lakh("about 105.5 lakh", "en") == "about 1 crore 5.5 lakh"


def test_ordinary_figures_are_left_alone():
    for text in ("85 lakh", "1 crore 20 lakh", "99.5 lakh", "₹1,00,00,000", "1100 sq ft"):
        assert crore_not_lakh(text, "en") == text
