"""Backend-rendered spoken forms (spec section 22).

Read-back text is rendered here from the exact value that will be written, never by the model.
Marathi and Hindi number words must be reviewed by native speakers (TTS gate C7).
"""

from __future__ import annotations

import re
from datetime import date, time

from app.lang.languages import Lang

MR_0_99 = (
    "शून्य एक दोन तीन चार पाच सहा सात आठ नऊ दहा अकरा बारा तेरा चौदा पंधरा सोळा सतरा अठरा एकोणीस "
    "वीस एकवीस बावीस तेवीस चोवीस पंचवीस सव्वीस सत्तावीस अठ्ठावीस एकोणतीस तीस एकतीस बत्तीस तेहतीस "
    "चौतीस पस्तीस छत्तीस सदतीस अडतीस एकोणचाळीस चाळीस एक्केचाळीस बेचाळीस त्रेचाळीस चव्वेचाळीस "
    "पंचेचाळीस सेहेचाळीस सत्तेचाळीस अठ्ठेचाळीस एकोणपन्नास पन्नास एक्कावन्न बावन्न त्रेपन्न चोपन्न "
    "पंचावन्न छप्पन्न सत्तावन्न अठ्ठावन्न एकोणसाठ साठ एकसष्ट बासष्ट त्रेसष्ट चौसष्ट पासष्ट सहासष्ट "
    "सदुसष्ट अडुसष्ट एकोणसत्तर सत्तर एकाहत्तर बाहत्तर त्र्याहत्तर चौऱ्याहत्तर पंच्याहत्तर शहात्तर "
    "सत्त्याहत्तर अठ्ठ्याहत्तर एकोणऐंशी ऐंशी एक्क्याऐंशी ब्याऐंशी त्र्याऐंशी चौऱ्याऐंशी पंच्याऐंशी "
    "शहाऐंशी सत्त्याऐंशी अठ्ठ्याऐंशी एकोणनव्वद नव्वद एक्क्याण्णव ब्याण्णव त्र्याण्णव चौऱ्याण्णव "
    "पंच्याण्णव शहाण्णव सत्त्याण्णव अठ्ठ्याण्णव नव्व्याण्णव"
).split()

HI_0_99 = (
    "शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस "
    "बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस "
    "पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चवालीस पैंतालीस छियालीस "
    "सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ "
    "इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर "
    "पचहत्तर छिहत्तर सतहत्तर अठहत्तर उनासी अस्सी इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी "
    "अट्ठासी नवासी नब्बे इक्यानवे बानवे तिरानवे चौरानवे पंचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे"
).split()

_EN_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen"
).split()
_EN_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()

assert len(MR_0_99) == 100 and len(HI_0_99) == 100

SCALE = {
    "mr": {"thousand": "हजार", "lakh": "लाख", "crore": "कोटी", "rupees": "रुपये"},
    "hi": {"thousand": "हज़ार", "lakh": "लाख", "crore": "करोड़", "rupees": "रुपये"},
    "en": {"thousand": "thousand", "lakh": "lakh", "crore": "crore", "rupees": "rupees"},
}

MONTHS = {
    "mr": "जानेवारी फेब्रुवारी मार्च एप्रिल मे जून जुलै ऑगस्ट सप्टेंबर ऑक्टोबर नोव्हेंबर डिसेंबर".split(),
    "hi": "जनवरी फ़रवरी मार्च अप्रैल मई जून जुलाई अगस्त सितंबर अक्टूबर नवंबर दिसंबर".split(),
    "en": "January February March April May June July August September October November December".split(),
}
WEEKDAYS = {  # Monday first
    "mr": "सोमवार मंगळवार बुधवार गुरुवार शुक्रवार शनिवार रविवार".split(),
    "hi": "सोमवार मंगलवार बुधवार गुरुवार शुक्रवार शनिवार रविवार".split(),
    "en": "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split(),
}
RELATIVE_DAYS = {
    "mr": {0: "आज", 1: "उद्या", 2: "परवा"},
    "hi": {0: "आज", 1: "कल", 2: "परसों"},
    "en": {0: "today", 1: "tomorrow", 2: "day after tomorrow"},
}


def _en_below_100(n: int) -> str:
    if n < 20:
        return _EN_ONES[n]
    tens, ones = divmod(n, 10)
    return _EN_TENS[tens] + ("" if ones == 0 else "-" + _EN_ONES[ones])


def below_100(n: int, lang: Lang) -> str:
    if not 0 <= n < 100:
        raise ValueError(n)
    if lang == "mr":
        return MR_0_99[n]
    if lang == "hi":
        return HI_0_99[n]
    return _en_below_100(n)


def _hundreds(h: int, rest: int, lang: Lang) -> str:
    if lang == "mr":
        if h == 1:
            return "शंभर" if rest == 0 else "एकशे"
        return below_100(h, lang) + "शे"
    if lang == "hi":
        return below_100(h, lang) + " सौ"
    return below_100(h, lang) + " hundred"


def integer_words(n: int, lang: Lang) -> str:
    """Indian numbering (crore / lakh / thousand)."""
    if n < 0:
        raise ValueError("negative numbers are not spoken")
    if n < 100:
        return below_100(n, lang)
    words: list[str] = []
    crore, rem = divmod(n, 10_000_000)
    lakh, rem = divmod(rem, 100_000)
    thousand, rem = divmod(rem, 1_000)
    hundred, rest = divmod(rem, 100)
    if crore:
        words += [integer_words(crore, lang), SCALE[lang]["crore"]]
    if lakh:
        words += [below_100(lakh, lang), SCALE[lang]["lakh"]]
    if thousand:
        words += [below_100(thousand, lang), SCALE[lang]["thousand"]]
    if hundred:
        words.append(_hundreds(hundred, rest, lang))
    if rest:
        words.append(below_100(rest, lang))
    return " ".join(words)


def half_words(n: int, lang: Lang) -> str:
    """n + 0.5 spoken: 1.5 दीड / डेढ़, 2.5 अडीच / ढाई, 8.5 साडेआठ / साढ़े आठ."""
    if lang == "mr":
        return {1: "दीड", 2: "अडीच"}.get(n, "साडे" + integer_words(n, lang))
    if lang == "hi":
        return {1: "डेढ़", 2: "ढाई"}.get(n, "साढ़े " + integer_words(n, lang))
    return integer_words(n, lang) + " and a half"


def inr_words(amount: int, lang: Lang) -> str:
    """₹85,00,000 → 'पंच्याऐंशी लाख'; ₹8,50,000 → 'साडेआठ लाख'; ₹1,20,00,000 → 'एक कोटी वीस लाख'."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    scale = SCALE[lang]
    if 150_000 <= amount < 10_000_000 and amount % 100_000 == 50_000:
        return f"{half_words(amount // 100_000, lang)} {scale['lakh']}"
    if amount >= 15_000_000 and amount % 10_000_000 == 5_000_000:
        return f"{half_words(amount // 10_000_000, lang)} {scale['crore']}"
    words = integer_words(amount, lang)
    if amount < 100_000:
        words += " " + scale["rupees"]
    return words




def phone_words(phone: str, lang: Lang) -> str:
    digits = [d for d in phone if d.isdigit()]
    if len(digits) > 10:
        digits = digits[-10:]
    groups = ["".join(digits[i:i + 5]) for i in range(0, len(digits), 5)]
    return ", ".join(" ".join(below_100(int(d), lang) for d in g) for g in groups)


def date_words(d: date, lang: Lang, today: date) -> str:
    delta = (d - today).days
    day = integer_words(d.day, lang)
    month = MONTHS[lang][d.month - 1]
    weekday = WEEKDAYS[lang][d.weekday()]
    core = f"{day} {month}" if lang != "en" else f"{month} {day}"
    if d.year != today.year:
        core += " " + integer_words(d.year, lang)
    relative = RELATIVE_DAYS[lang].get(delta)
    if relative:
        return f"{relative}, {weekday} {core}"
    return f"{weekday}, {core}"


def _day_part(hour: int, lang: Lang) -> str:
    if lang == "en":
        return "AM" if hour < 12 else "PM"
    parts = {
        "mr": ("सकाळी", "दुपारी", "संध्याकाळी", "रात्री"),
        "hi": ("सुबह", "दोपहर", "शाम", "रात"),
    }[lang]
    if 5 <= hour < 12:
        return parts[0]
    if 12 <= hour < 16:
        return parts[1]
    if 16 <= hour < 20:
        return parts[2]
    return parts[3]


def time_words(t: time, lang: Lang) -> str:
    h12 = t.hour % 12 or 12
    m = t.minute
    part = _day_part(t.hour, lang)
    if lang == "en":
        if m == 0:
            core = integer_words(h12, lang)
        else:
            core = f"{integer_words(h12, lang)} {integer_words(m, lang) if m >= 10 else 'oh ' + integer_words(m, lang)}"
        return f"{core} {part}"
    suffix = "वाजता" if lang == "mr" else "बजे"
    next_hour = h12 % 12 + 1
    if m == 0:
        core = integer_words(h12, lang)
    elif m == 30:
        core = half_words(h12, lang)
    elif m == 15:
        core = ("सव्वा " if lang == "mr" else "सवा ") + integer_words(h12, lang)
    elif m == 45:
        core = ("पावणे" if lang == "mr" else "पौने ") + integer_words(next_hour, lang)
    else:
        if lang == "mr":
            return f"{part} {integer_words(h12, lang)} वाजून {integer_words(m, lang)} मिनिटांनी"
        return f"{part} {integer_words(h12, lang)} बजकर {integer_words(m, lang)} मिनट पर"
    return f"{part} {core} {suffix}"


_DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_OVER_100_LAKH = re.compile(r"(?<![\d.,])(\d{3,}(?:\.\d+)?)\s*(lakhs?|lacs?|लाख)(?![A-Za-z])", re.I)


def crore_not_lakh(text: str, lang: Lang) -> str:
    """'100 lakh' is said '1 crore', '120 लाख' is '1 करोड़ 20 लाख': nobody prices a flat in hundreds of lakh."""
    def fix(m: re.Match[str]) -> str:
        lakh = float(m.group(1))
        crore, rest = divmod(lakh, 100)
        rest_s = f"{rest:.2f}".rstrip("0").rstrip(".")
        devanagari = m.group(2) == "लाख"
        crore_word = ("कोटी" if lang == "mr" else "करोड़") if devanagari else "crore"
        lakh_word = "लाख" if devanagari else "lakh"
        return f"{int(crore)} {crore_word}" + (f" {rest_s} {lakh_word}" if rest else "")
    if not text:
        return text
    return _OVER_100_LAKH.sub(fix, text.translate(_DEV_DIGITS))
