"""Contact parsing and the dialogue state machine."""

from __future__ import annotations

import pytest

from leadsbot import logic


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+994 50 123 45 67", "+994501234567"),
        ("0501234567", "+994501234567"),
        ("501234567", "+994501234567"),
        ("0 50 123 45 67", "+994501234567"),
        ("+7 916 123 45 67", "+79161234567"),
        ("(055) 333-44-22", "+994553334422"),
    ],
)
def test_extract_phone_variants(raw, expected):
    phone, remainder = logic.extract_phone(raw)
    assert phone == expected
    assert remainder.strip() == ""


def test_phone_ignores_short_and_overlong_numbers():
    assert logic.extract_phone("12 34") == (None, "12 34")
    long_number = "1" * 20
    assert logic.extract_phone(long_number) == (None, long_number)


def test_extract_name_rules():
    assert logic.extract_name("Elvin") == "Elvin"
    assert logic.extract_name("Adım: Aysel M.") == "Aysel M"
    assert logic.extract_name("mən Elvin") == "Elvin"
    assert logic.extract_name("Aysel Məmmədova") == "Aysel Məmmədova"


@pytest.mark.parametrize(
    "text",
    [
        "Salam",
        "Salam necəsiz",
        "salam, necesiz?",
        "qiymət necədir",
        "Kurslar haqqında?",
        "Nə vaxt başlayır",
        "kurslar haqqında məlumat almaq istəyirəm",
        "Elvin 123",
        "elvin memmedov",          # lowercase multi-word is not trusted
        "Salam Elvin",             # greetings are not names
    ],
)
def test_questions_and_sentences_are_not_names(text):
    assert logic.extract_name(text) is None


def test_first_message_with_full_contact_is_not_greeted_twice():
    plan = logic.plan_reply(None, None, logic.STATUS_AWAITING_CONTACT, 0, "Elvin, 055 333 44 22")
    assert (plan.name, plan.phone, plan.status) == ("Elvin", "+994553334422", logic.STATUS_DONE)
    assert plan.reply == logic.THANK_YOU


def test_happy_path_split_contact():
    first = logic.plan_reply(None, None, logic.STATUS_AWAITING_CONTACT, 0, "Salam")
    assert (first.status, first.reply, first.hints) == (
        logic.STATUS_AWAITING_CONTACT, logic.GREETING, 1,
    )

    name = logic.plan_reply(first.name, first.phone, first.status, first.hints, "Aysel")
    assert (name.name, name.reply, name.status) == (
        "Aysel", logic.ASK_PHONE, logic.STATUS_AWAITING_PHONE,
    )

    phone = logic.plan_reply(name.name, name.phone, name.status, name.hints, "055 333 44 22")
    assert phone.status == logic.STATUS_DONE
    assert phone.reply == logic.THANK_YOU

    silence = logic.plan_reply(phone.name, phone.phone, phone.status, phone.hints, "qiymət nədir?")
    assert silence.reply is None
    assert silence.status == logic.STATUS_DONE


def test_phone_first_then_name():
    phone = logic.plan_reply(None, None, logic.STATUS_AWAITING_CONTACT, 0, "0501234567")
    assert (phone.phone, phone.status, phone.reply) == (
        "+994501234567", logic.STATUS_AWAITING_NAME, logic.ASK_NAME,
    )
    name = logic.plan_reply(phone.name, phone.phone, phone.status, phone.hints, "Murad")
    assert (name.name, name.status, name.reply) == ("Murad", logic.STATUS_DONE, logic.THANK_YOU)


def test_invalid_number_gets_a_retry_hint():
    plan = logic.plan_reply("Elvin", None, logic.STATUS_AWAITING_PHONE, 1, "12345")
    assert plan.phone is None
    assert plan.reply == logic.ASK_PHONE_RETRY


def test_gives_up_after_max_hints_then_stays_silent():
    status, hints = logic.STATUS_AWAITING_CONTACT, 0
    name = phone = None
    replies = []
    for _ in range(6):
        plan = logic.plan_reply(name, phone, status, hints, "salam")
        name, phone, status, hints = plan.name, plan.phone, plan.status, plan.hints
        replies.append(plan.reply)
    assert replies[0] == logic.GREETING
    assert logic.CLOSING in replies
    assert replies[-1] is None
    assert status == logic.STATUS_MANUAL


def test_does_not_overwrite_known_fields():
    plan = logic.plan_reply("Elvin", None, logic.STATUS_AWAITING_PHONE, 1, "Aysel 055 111 22 33")
    assert plan.name == "Elvin"
    assert plan.status == logic.STATUS_DONE