"""Conversation logic: parse a contact out of a DM and decide what to reply.

Pure functions only - no I/O, no database, no network. The dialogue is an explicit
state machine so the bot always knows which field it is waiting for:

    awaiting_contact -> awaiting_name / awaiting_phone -> done
                     \\-> manual (staff takes over, bot goes silent)
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
ASK_PHONE_RETRY = (
    "Nömrə tam görünmür. Zəhmət olmasa, əlaqə nömrənizi tam yazın, "
    "məsələn: +994 50 123 45 67"
)
THANK_YOU = (
    "Təşəkkür edirik! Məlumatlarınız qeydə alındı. "
    "Əməkdaşımız sizinlə əlaqə saxlayacaq."
)
CLOSING = (
    "Məlumatlarınızı qeydə ala bilmədik. İstənilən vaxt yenidən yazın - "
    "əməkdaşımız sizə kömək edəcək."
)

STATUS_AWAITING_CONTACT = "awaiting_contact"
STATUS_AWAITING_NAME = "awaiting_name"
STATUS_AWAITING_PHONE = "awaiting_phone"
STATUS_DONE = "done"
STATUS_MANUAL = "manual"

WAITING_STATUSES = {
    STATUS_AWAITING_CONTACT,
    STATUS_AWAITING_NAME,
    STATUS_AWAITING_PHONE,
}
TERMINAL_STATUSES = {STATUS_DONE, STATUS_MANUAL}

COUNTRY_PREFIX = "994"

# 9-15 digits, tolerating spaces, dashes, brackets and an explicit +.
PHONE_CANDIDATE = re.compile(r"(?<!\w)\+?\d[\d\s()\-]{7,}\d(?!\w)")
NAME_PREFIX = re.compile(
    r"(?i)^(adım|adim|mənim adım|menim adim|mən|men|ad|name)\s*[:\-]?\s*"
)
NAME_WORD = re.compile(r"[A-Za-zƏəĞğİıÖöŞşÜüÇçА-Яа-яЁё'\-]+")

# Words that are never a person's name.
NOT_A_NAME = {
    "salam", "salamlar", "salamm", "əleyküm", "aleykum", "əleykümsalam",
    "necəsiz", "necesiz", "necəsiniz", "necesiniz", "necəsən", "necesen",
    "hello", "hi", "hey", "privet", "bəli", "beli", "yox", "xeyr", "ok", "okay",
    "təşəkkür", "tesekkur", "təşəkkürlər", "tesekkurler", "sağol", "sagol",
    "əla", "ela", "good", "fine", "thanks", "thank", "you", "narahat",
    "maraqlanıram", "maraqlaniram",
}

# Words that mark a message as a question/interest rather than a self-introduction.
QUESTION_WORDS = {
    "qiymət", "qiymet", "qiyməti", "qiymeti", "nədir", "nedir", "necə", "nece",
    "necədir", "necedir", "nə", "ne", "hara", "harada", "hansı", "hansi",
    "neçə", "vaxt", "kurs", "kurslar", "kursun", "kurslara", "olur",
    "var", "yoxdur", "məlumat", "melumat", "haqqında", "haqqinda",
    "istəyirəm", "isteyirem", "öyrənmək", "oyrenmek", "başlayır", "baslayir",
    "ünvan", "unvan", "qeydiyyat", "müddət", "muddet", "ödəniş", "odenis",
    "endirim", "detallar", "detay", "müraciət", "muraciet", "sual", "sualım",
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
    remainder = text[: match.start()] + " " + text[match.end():]
    remainder = re.sub(r"[()]", " ", remainder).strip()
    return phone, re.sub(r"\s{2,}", " ", remainder)


def looks_like_number_attempt(text: str) -> bool:
    """True when the message is mostly digits - i.e. a phone number typed badly."""
    stripped = re.sub(r"[\s()\-\+]", "", text)
    if not stripped:
        return False
    digits = sum(ch.isdigit() for ch in stripped)
    return digits >= 5 and digits / len(stripped) >= 0.7


def extract_name(text: str) -> str | None:
    """Accept a stand-alone person name: ``Elvin`` or ``Aysel Məmmədova``.

    Deliberately conservative: a greeting, a question or a sentence is never a
    name. Multi-word candidates must be capitalised like a real name.
    """
    raw = text.strip()
    if not raw or "?" in raw:
        return None
    candidate = NAME_PREFIX.sub("", raw).strip(" .,;:!\n\t")
    if not 2 <= len(candidate) <= 60:
        return None
    words = candidate.split()
    if not 1 <= len(words) <= 3:
        return None
    if not all(NAME_WORD.fullmatch(word) for word in words):
        return None
    lowered = [word.lower() for word in words]
    if any(word in NOT_A_NAME or word in QUESTION_WORDS for word in lowered):
        return None
    if len(words) > 1 and not all(word[0].isupper() for word in words):
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

    # Keep the state honest: it names the field we are missing.
    if new_phone:
        next_status = STATUS_AWAITING_NAME
    elif new_name:
        next_status = STATUS_AWAITING_PHONE
    else:
        next_status = STATUS_AWAITING_CONTACT

    first_message = (
        status == STATUS_AWAITING_CONTACT and hints == 0 and not name and not phone
    )
    if first_message and not new_name and not new_phone:
        # The greeting is itself a prompt: count it so the bot never greets twice.
        return Plan(None, None, STATUS_AWAITING_CONTACT, 1, GREETING)

    next_hints = hints + 1
    if next_hints > max_hints:
        return Plan(new_name, new_phone, STATUS_MANUAL, next_hints, CLOSING)

    if not new_phone and looks_like_number_attempt(text):
        reply = ASK_PHONE_RETRY
    elif new_name:
        reply = ASK_PHONE
    else:
        reply = ASK_NAME
    return Plan(new_name, new_phone, next_status, next_hints, reply)