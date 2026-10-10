"""Areas, like prices, may only be spoken when a tool returned them in this call."""

from app.domain.real_estate.guardrails import PriceGuard

INVENTED = ("Skyline Crest में 3 BHK का carpet area 1650 sq ft से 1850 sq ft और built-up area "
            "1880 sq ft से 2100 sq ft है।")


def test_invented_areas_are_caught():
    guard = PriceGuard()
    guard.add_result({"chunks": [{"content": "| 3 BHK | 950 | 2 | 4 |"}]})
    assert guard.unsupported(INVENTED) == [1650, 1850, 1880, 2100]


def test_areas_from_a_tool_result_may_be_spoken():
    guard = PriceGuard()
    guard.add_result({"unitTypes": [{"bhk": 3, "carpetAreaSqft": 950}]})
    assert guard.unsupported("Skyline Crest में 3 BHK का carpet area 950 sq ft है।") == []
    assert guard.unsupported("3 BHK carpet area ९५० वर्ग फुट है") == []


def test_sentences_without_an_area_are_not_affected():
    guard = PriceGuard()
    assert guard.unsupported("Visit Sunday 10 AM, Tower A, floor 12.") == []


def test_prices_are_still_checked():
    guard = PriceGuard()
    guard.add_result({"priceMinInr": 7650000})
    assert guard.unsupported("2 BHK 76.5 lakh से शुरू है") == []
    assert guard.unsupported("2 BHK 60 lakh में है") == [6000000]


def test_a_brochure_never_supplies_a_price():
    guard = PriceGuard()
    guard.add_result({"chunks": [{"text": "3 BHK Type A starting ₹92 lakh, carpet 1180 sq ft"}]}, prices=False)
    assert guard.unsupported("Aurum Heights 3 BHK 92 lakh से शुरू है") == [9200000]
    assert guard.unsupported("3 BHK का carpet area 1180 sq ft है") == []  # areas still allowed


def test_the_callers_spoken_budget_may_be_read_back():
    guard = PriceGuard()
    guard.add_caller_text("मुझे two BHK में साठ से अठ्ठे अस्सी लाख")
    assert guard.unsupported("ठीक है, 60 से 80 lakh में 2 BHK") == []
    assert guard.unsupported("60 lakh से 80 lakh") == []


def test_numbers_written_as_words_are_checked_like_digits():
    guard = PriceGuard()
    guard.add_result({"priceMinInr": 7650000, "priceMaxInr": 8200000, "unitTypes": [{"carpetAreaSqft": 720}]})
    assert guard.unsupported("Skyline Crest में साढ़े छिहत्तर से बयासी लाख में मिलेगा।") == []
    assert guard.unsupported("दो BHK का carpet area सात सौ बीस स्क्वायर फ़ीट है।") == []
    assert guard.unsupported("दो BHK साठ लाख में मिलेगा।") == [6000000]
    assert guard.unsupported("carpet area सोलह सौ पचास sq ft है।") == [1650]



def test_a_budget_with_the_single_character_nukta_may_be_read_back():
    # Sarvam writes "करोड़" with U+095C; the patterns use ड + nukta. Both must count.
    guard = PriceGuard()
    guard.add_caller_text("1 करोड़ के आस पास")
    assert guard.unsupported("समझ गई, 1 crore के आसपास।") == []
    assert guard.unsupported("आपके 1 करोड़ के बजट में 3 ऑप्शन्स हैं।") == []
