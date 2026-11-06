"""Conversation logic: parse a contact out of a DM and decide what to reply.

Pure functions only - no I/O, no database, no network. This is the part that
must be easy to reason about and to test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GREETING = (
    "Salam! Müraciətiniz üçün təşəkkür edirik. "
    "Zəhmət olmasa, adınızı və əlaqə nömrənizi qeyd edin. "
    "Əməkdaşımız sizinlə telefon vasitəsilə əlaqə saxlayacaq."
)
ASK_NAME = "Zəhmət olmasa, adınızı da qeyd edin."
ASK_PHONE = "Təşəkkürlər! Zəhmət olmasa, əlaqə nömrənizi də yazın."
THANK_YOU = (
    "Təşəkkür edirik! Məlumatlarınız qeydə alındı. "
    "Əməkdaşımız sizinlə əlaqə saxlayacaq."
)
CLOSING = (
    "Məlumatlarınızı qeydə ala bilmədik. İstənilən vaxt yenidən yazın - "
    "əməkdaşımız sizə kömək edəcək."
)

STATUS_NEW = "new"
STATUS_COLLECTING = "collecting"
STATUS_DONE = "done"
STATUS_MANUAL = "manual"
TERMINAL_STATUSES = {STATUS_DONE, STATUS_MANUAL}

COUNTRY_PREFIX = "994"

# 9-15 digits, tolerating spaces, dashes, brackets and an explicit +.
PHONE_CANDIDATE = re.compile(r"(?<!\w)\+?\d[\d\s()\-]{7,}\d(?!\w)")
NAME_PREFIX = re.compile(
    r"(?i)^(adım|adim|mənim adım|menim adim|mən|men|ad|name)\s*[:\-]?\s*"
)
NAME_WORD = re.compile(r"[A-Za-zƏəĞğİıÖöŞşÜüÇçА-Яа-яЁё'\-]+")
NOT_A_NAME = {
    "salam", "salamlar", "hello", "hi", "privet", "bəli", "beli", "yox", "ok",
    "okay", "təşəkkür", "tesekkur", "sağol", "sagol", "narahat", "maraqlanıram",
}

# Words that mark a message as a question rather than a self-introduction.
QUESTION_WORDS = {
    "qiymət", "qiymet", "qiyməti", "qiymeti", "nədir", "nedir", "necə", "nece",
    "necədir", "necedir", "nə", "ne", "hara", "harada", "hansı", "hansi",
    "neçə", "nece", "vaxt", "kurs", "kurslar", "kursun", "olur", "var", "yoxdur",
    "məlumat", "melumat", "haqqında", "haqqinda", "istəyirəm", "isteyirem",
    "what", "how", "when", "where", "price", "cost", "info",
}


@dataclass(frozen=True)
class Plan:
    """Result of processing one incoming message."""

    name: str | None
    phone: str | None
    status: str
    hints: int
    reply: str | None


def normalize_phone(digits: str, had_plus: bool) -> str:
    """Turn a local or international digit string into a dialable number."""
    if digits.startswith(COUNTRY_PREFIX) and len(digits) == 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+" + COUNTRY_PREFIX + digits[1:]
    if len(digits) == 9:
        return "+" + COUNTRY_PREFIX + digits
    return ("+" if had_plus else "") + digits


def extract_phone(text: str) -> tuple[str | None, str]:
    """Return (normalized phone, text without the phone) or (None, text)."""
    match = PHONE_CANDIDATE.search(text)
    if not match:
        return None, text
    candidate = match.group().strip()
    digits = re.sub(r"\D", "", candidate)
    if not 9 <= len(digits) <= 15:
        return None, text
    phone = normalize_phone(digits, candidate.startswith("+"))
    remainder = (text[: match.start()] + " " + text[match.end():])
    remainder = re.sub(r"[()]", " ", remainder).strip()
    return phone, re.sub(r"\s{2,}", " ", remainder)


def extract_name(text: str) -> str | None:
    """Accept a short stand-alone name such as ``Elvin`` or ``Adım: Aysel M.``."""
    if text.strip().endswith("?"):
        return None
    candidate = NAME_PREFIX.sub("", text).strip(" .,;:!?\n\t")
    if not 2 <= len(candidate) <= 60:
        return None
    words = candidate.split()
    if not 1 <= len(words) <= 4:
        return None
    if not all(NAME_WORD.fullmatch(word) for word in words):
        return None
    if candidate.lower() in NOT_A_NAME:
        return None
    if any(word.lower() in QUESTION_WORDS for word in words):
        return None
    return candidate


def plan_reply(
    name: str | None,
    phone: str | None,
    status: str,
    hints: int,
    text: str,
    max_hints: int = 3,
) -> Plan:
    """Decide the next state and reply for one incoming DM."""
    if status in TERMINAL_STATUSES:
        return Plan(name, phone, status, hints, None)

    found_phone, remainder = extract_phone(text)
    found_name = extract_name(remainder)
    if found_name is None and found_phone is None:
        found_name = extract_name(text)

    new_name = name or found_name
    new_phone = phone or found_phone

    if new_name and new_phone:
        return Plan(new_name, new_phone, STATUS_DONE, hints, THANK_YOU)

    if status == STATUS_NEW and not new_name and not new_phone:
        # First contact without any usable data: greet, don't burn a hint yet.
        return Plan(None, None, STATUS_COLLECTING, 0, GREETING)

    next_hints = hints + 1
    if next_hints > max_hints:
        return Plan(new_name, new_phone, STATUS_MANUAL, next_hints, CLOSING)
    reply = ASK_PHONE if new_name else ASK_NAME
    return Plan(new_name, new_phone, STATUS_COLLECTING, next_hints, reply)