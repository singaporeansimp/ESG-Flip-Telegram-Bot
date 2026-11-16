"""The gallery of every message, checked as a whole rather than one at a time.

``tests/samples.py`` renders every message the bot can send, from flips really
played, and this walks the lot. What it pins is what has to be true of *all* of
them, however the wording changes next:

* nothing outgrows Telegram's limits, in the largest case of each kind;
* no message falls back on the letters ``X``, ``Y`` and ``Z``, which only the
  documents use -- apart from the one legend that exists to connect the two;
* the HTML is the two tags this bot uses, opened and closed on one line, which
  is what lets :func:`~esgbot.telegram.render.render` cut a long message at a
  newline;
* a pop-up is plain text, because a callback answer is never parsed;
* money reads the same everywhere;
* and a name somebody chose for themselves reaches Telegram escaped.

The wording of any one message is pinned in ``test_telegram_render.py``. This is
the net under all of them.
"""

from __future__ import annotations

import re

import pytest

from esgbot.engine import Notice, VoidReason, VoidView
from esgbot.game.rules import FlipRequest, Settings
from esgbot.telegram import render
from tests.samples import ALERT, MESSAGE, Sample, build_gallery

GALLERY = build_gallery()
"""Rendered once for the whole module: building it plays a dozen real flips."""

_TAGS = re.compile(r"</?(?:b|code)>")
"""The only two tags any message uses."""

_LETTER = re.compile(r"(?<![0-9A-Za-z])[XYZ](?![0-9A-Za-z])")
"""A standalone ``X``, ``Y`` or ``Z``: a letter where a name should be."""

_LEGEND = "What each number means"
"""The heading of the one block allowed to mention the letters."""

_BARE_THOUSANDS = re.compile(r"\$\d{4,}")
"""An amount written without thousands separators, which none should be."""


def _messages() -> list[Sample]:
    return [sample for sample in GALLERY if sample.kind == MESSAGE]


def _alerts() -> list[Sample]:
    return [sample for sample in GALLERY if sample.kind == ALERT]


def test_the_gallery_covers_every_kind_of_message() -> None:
    sections = {sample.section for sample in GALLERY}
    assert sections == {
        "Starting a flip",
        "Repeating a flip",
        "The lobby",
        "The deal",
        "The showdown",
        "The result",
        "A flip that was voided",
        "Peek",
        "Button answers",
        "Stats",
        "Verifying a flip",
    }
    assert _messages()
    assert _alerts()
    assert all(sample.text.strip() for sample in GALLERY)


def test_every_button_answer_the_engine_can_give_is_in_the_gallery() -> None:
    answers = {sample.text for sample in _alerts()}
    for notice in Notice:
        assert render.render_notice(notice) in answers


def test_every_reason_a_flip_can_be_voided_for_is_in_the_gallery() -> None:
    lines = {line for sample in _messages() for line in sample.text.splitlines()}
    for reason in VoidReason:
        notice = render.render_void(VoidView(token="tok", reason=reason))
        because = notice.splitlines()[1]
        assert because in lines, f"no sample says why a flip voided for {reason.value}"


def test_every_message_fits_telegrams_limit() -> None:
    for sample in _messages():
        assert len(sample.text) <= render.MESSAGE_LIMIT, sample.title


def test_every_pop_up_fits_the_alert_limit() -> None:
    for sample in _alerts():
        assert len(sample.text) <= render.ALERT_LIMIT, sample.title


def test_the_largest_hand_a_flip_can_deal_still_fits_a_pop_up() -> None:
    """The one pop-up with no room to spare: every card shown, every board still named.

    A longer pop-up than this one exists -- the readings are written in the
    roomiest form that fits, so a small hand fills the same 200 characters with
    nicer words -- but this is the hand that has the least room for them, and
    what gives way is the wording and never a card or a line.
    """
    (biggest,) = [sample for sample in _alerts() if " 21 cards" in sample.text]
    _, hand, legend, *made = biggest.text.splitlines()
    assert len(hand.split()) == 21
    assert legend == "* just dealt"
    assert [line.split(":")[0] for line in made] == ["B1", "B2", "HS"]
    assert len(biggest.text) <= render.ALERT_LIMIT


def test_no_message_names_a_setting_by_letter() -> None:
    for sample in _messages():
        for match in _LETTER.finditer(sample.text):
            start, end = match.span()
            where = f"...{sample.text[max(0, start - 40) : end + 40]}..."
            in_legend = _LEGEND in sample.text
            parenthesised = (
                sample.text[start - 1 : start] == "(" and sample.text[end : end + 1] == ")"
            )
            assert in_legend, f"{sample.title} says {match.group()!r} on its own: {where}"
            assert parenthesised, f"{sample.title} says a bare {match.group()!r}: {where}"


def test_only_the_usage_legend_mentions_the_letters_at_all() -> None:
    mentions = [sample.title for sample in _messages() if _LETTER.search(sample.text)]
    assert mentions
    assert all(_LEGEND in sample.text for sample in _messages() if _LETTER.search(sample.text))
    for sample in _messages():
        if _LEGEND in sample.text:
            assert "(X)" in sample.text
            assert "(Y)" in sample.text
            assert "(Z)" in sample.text


def test_every_setting_is_named_the_same_way_everywhere() -> None:
    said = " ".join(sample.text for sample in _messages())
    assert "private cards" in said
    assert "extra cards" in said
    assert "scoop" in said
    # The names the bot used to use, and the jargon it never should: a flip's
    # cards are private cards, not hole cards, wherever they are mentioned.
    assert "hole card" not in said.lower()


def test_every_message_uses_only_the_two_tags_and_closes_them_on_the_line() -> None:
    for sample in _messages():
        for line in sample.text.splitlines():
            assert line.count("<b>") == line.count("</b>"), f"{sample.title}: {line}"
            assert line.count("<code>") == line.count("</code>"), f"{sample.title}: {line}"
            assert "<" not in _TAGS.sub("", line), f"{sample.title} has other markup: {line}"


def test_a_pop_up_is_plain_text() -> None:
    for sample in _alerts():
        assert "<" not in sample.text
        assert "&" not in sample.text


def test_money_is_written_the_same_way_everywhere() -> None:
    for sample in GALLERY:
        assert not _BARE_THOUSANDS.search(sample.text), sample.title
    said = " ".join(sample.text for sample in _messages())
    assert "$1,000,000" in said
    assert "+$" in said
    assert "-$" in said


def test_a_name_that_would_be_markup_is_escaped_wherever_it_appears() -> None:
    hostile = [sample for sample in _messages() if "Ev&amp;il" in sample.text]
    assert len(hostile) >= 3
    for sample in hostile:
        # The raw name is "<b>Ev&il</b> Bob", and none of it survives as markup.
        assert "Ev&il" not in sample.text
        assert "&lt;b&gt;Ev&amp;il&lt;/b&gt;" in sample.text


def test_a_message_longer_than_telegram_allows_is_cut_on_a_line_boundary() -> None:
    # The backstop, not a routine: nothing above comes near it. A cut message
    # keeps whole lines, so the HTML it leaves is still balanced.
    long = "\n".join(f"<b>line {index}</b>" for index in range(600))
    cut = render._fit(long)
    assert len(cut) <= render.MESSAGE_LIMIT
    assert cut.endswith("…")
    for line in cut.splitlines():
        assert line.count("<b>") == line.count("</b>")


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        pytest.param(Settings(wager=50, x=5, y=2, z=3), "scoop pays 3x the wager", id="explicit"),
        pytest.param(
            Settings(wager=50, x=5, y=0, z=1), "scoop pays the wager, no bonus", id="no-bonus"
        ),
    ],
)
def test_the_scoop_multiplier_is_explained_rather_than_lettered(
    settings: Settings, expected: str
) -> None:
    assert expected in render._settings_strip(settings)
    assert "Z" not in render._settings_strip(settings)


def test_a_setting_the_bot_fills_in_says_so_instead_of_showing_its_figure() -> None:
    strip = render._request_strip(FlipRequest(wager=50, x=5))
    assert "as many extra cards as fit" in strip
    assert "scoop pays the wager times the number of players" in strip
    assert "21" not in strip
