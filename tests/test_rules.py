from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from esgbot.game.rules import (
    FLIP_FIELDS_MIN,
    SETTING_FIELDS,
    WAGER_MAX,
    WAGER_MIN,
    X_MAX,
    X_MIN,
    Y_DEFAULT,
    Y_MAX,
    Y_MIN,
    Z_MAX,
    Z_MIN,
    FlipRequest,
    Settings,
    SettingsError,
    max_players,
    parse_flip_request,
    parse_settings,
    parse_whole_number,
)

_FIELD_LIMITS: tuple[tuple[str, int, int], ...] = (
    ("Wager", WAGER_MIN, WAGER_MAX),
    ("Private cards", X_MIN, X_MAX),
    ("Extra cards", Y_MIN, Y_MAX),
    ("Scoop multiplier", Z_MIN, Z_MAX),
)

# The Settings keyword (and parse_settings / FlipRequest keyword) each field is stored under.
_KEYWORD: dict[str, str] = {
    "Wager": "wager",
    "Private cards": "x",
    "Extra cards": "y",
    "Scoop multiplier": "z",
}


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_parse_whole_number_accepts_limit_values(field: str, minimum: int, maximum: int) -> None:
    low = parse_whole_number(str(minimum), field=field, minimum=minimum, maximum=maximum)
    high = parse_whole_number(str(maximum), field=field, minimum=minimum, maximum=maximum)
    assert low == minimum
    assert high == maximum


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_parse_whole_number_rejects_just_above_maximum(
    field: str, minimum: int, maximum: int
) -> None:
    with pytest.raises(SettingsError, match=field):
        parse_whole_number(str(maximum + 1), field=field, minimum=minimum, maximum=maximum)


@pytest.mark.parametrize(
    ("field", "minimum", "maximum"),
    [entry for entry in _FIELD_LIMITS if entry[1] > 0],
)
def test_parse_whole_number_rejects_just_below_minimum(
    field: str, minimum: int, maximum: int
) -> None:
    with pytest.raises(SettingsError, match=field):
        parse_whole_number(str(minimum - 1), field=field, minimum=minimum, maximum=maximum)


_ARABIC_INDIC_FIFTY = chr(0x0665) + chr(0x0660)
_FULLWIDTH_FIFTY = chr(0xFF15) + chr(0xFF10)


@pytest.mark.parametrize(
    "text",
    [
        "$50",
        "5.0",
        "1,000",
        "-3",
        "1 0",
        "1_000",
        "5e2",
        _ARABIC_INDIC_FIFTY,
        _FULLWIDTH_FIFTY,
        "fifty",
        "++5",
        "5 ",
    ],
)
def test_parse_whole_number_rejects_malformed_text(text: str) -> None:
    with pytest.raises(SettingsError):
        parse_whole_number(text, field="Wager", minimum=WAGER_MIN, maximum=WAGER_MAX)


def test_parse_whole_number_rejects_empty_text() -> None:
    with pytest.raises(SettingsError, match="missing"):
        parse_whole_number("", field="Wager", minimum=WAGER_MIN, maximum=WAGER_MAX)


def test_parse_whole_number_error_field_matches() -> None:
    error = None
    try:
        parse_whole_number("$50", field="Wager", minimum=WAGER_MIN, maximum=WAGER_MAX)
    except SettingsError as caught:
        error = caught
    assert error is not None
    assert error.field == "Wager"
    assert error.message == str(error)


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_settings_error_field_name_from_parse_settings(
    field: str, minimum: int, maximum: int
) -> None:
    valid = {"wager": "50", "x": "5", "y": "2", "z": "3"}
    valid[_KEYWORD[field]] = str(maximum + 1)
    with pytest.raises(SettingsError) as excinfo:
        parse_settings(**valid)
    assert excinfo.value.field == field


def test_parse_settings_builds_settings() -> None:
    settings = parse_settings(wager="50", x="5", y="2", z="3")
    assert settings == Settings(wager=50, x=5, y=2, z=3)


def test_parse_flip_request_with_every_value_matches_parse_settings() -> None:
    request = parse_flip_request(["50", "5", "2", "3"])
    assert request.resolve(4) == parse_settings(wager="50", x="5", y="2", z="3")


@pytest.mark.parametrize("tokens", [[], ["50"], ["50", "5", "2", "3", "4"]])
def test_parse_flip_request_rejects_wrong_count(tokens: list[str]) -> None:
    with pytest.raises(SettingsError, match="wager and the number of private cards"):
        parse_flip_request(tokens)


# --- parse_flip_request: every shape -------------------------------------------


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        pytest.param(["50", "5"], FlipRequest(50, 5, None, None), id="two"),
        pytest.param(["50", "5", "2"], FlipRequest(50, 5, 2, None), id="three"),
        pytest.param(["50", "5", "2", "3"], FlipRequest(50, 5, 2, 3), id="four"),
        pytest.param(["1", "21", "0"], FlipRequest(1, 21, 0, None), id="y-zero"),
    ],
)
def test_parse_flip_request_reads_every_argument_shape(
    tokens: list[str], expected: FlipRequest
) -> None:
    request = parse_flip_request(tokens)
    assert request == expected
    assert (request.wager, request.x, request.y, request.z) == (
        expected.wager,
        expected.x,
        expected.y,
        expected.z,
    )


@pytest.mark.parametrize(
    ("tokens", "y_defaulted", "z_defaulted"),
    [
        pytest.param(["50", "5"], True, True, id="two"),
        pytest.param(["50", "5", "2"], False, True, id="three"),
        pytest.param(["50", "5", "2", "3"], False, False, id="four"),
    ],
)
def test_parse_flip_request_flags_what_was_left_out(
    tokens: list[str], y_defaulted: bool, z_defaulted: bool
) -> None:
    request = parse_flip_request(tokens)
    assert request.y_defaulted is y_defaulted
    assert request.z_defaulted is z_defaulted


def test_parse_flip_request_accepts_any_sequence() -> None:
    assert parse_flip_request(("50", "5", "2")) == FlipRequest(50, 5, 2, None)


def test_a_third_value_is_y_never_z() -> None:
    request = parse_flip_request(["50", "5", "3"])
    assert request.y == 3
    assert request.z is None


def test_flip_fields_min_and_limits_are_unchanged() -> None:
    assert FLIP_FIELDS_MIN == 2
    assert SETTING_FIELDS[:FLIP_FIELDS_MIN] == ("Wager", "Private cards")
    assert (WAGER_MIN, WAGER_MAX) == (1, 1_000_000)
    assert (X_MIN, X_MAX) == (5, 21)
    assert (Y_MIN, Y_MAX) == (0, 21)
    assert (Z_MIN, Z_MAX) == (1, 100)
    assert Y_DEFAULT == Y_MAX == 21


# --- parse_flip_request: wrong counts ------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 5, 6])
def test_wrong_count_message_names_the_fields_and_shows_both_examples(count: int) -> None:
    with pytest.raises(SettingsError) as excinfo:
        parse_flip_request(["5"] * count)
    message = str(excinfo.value)
    assert "the wager and the number of private cards" in message
    assert "the extra cards and the scoop multiplier" in message
    assert "50 5" in message
    assert "50 5 2 3" in message
    assert message.endswith(f"Got {count}.")


# --- parse_flip_request: bad values in every position --------------------------

_GOOD = ["50", "5", "2", "3"]
_SHAPES = [
    pytest.param(length, position, id=f"{length}-tokens-field-{position}")
    for length in (4, 3, 2)
    for position in range(length)
]


def _tokens_with(position: int, text: str, length: int) -> list[str]:
    tokens = _GOOD[:length]
    tokens[position] = text
    return tokens


@pytest.mark.parametrize(("length", "position"), _SHAPES)
@pytest.mark.parametrize("text", ["abc", "-2", "0x5", "5.0", ""])
def test_a_malformed_value_names_its_own_field(length: int, position: int, text: str) -> None:
    with pytest.raises(SettingsError) as excinfo:
        parse_flip_request(_tokens_with(position, text, length))
    assert excinfo.value.field == SETTING_FIELDS[position]


@pytest.mark.parametrize(("length", "position"), _SHAPES)
def test_values_just_outside_the_limits_name_their_field(length: int, position: int) -> None:
    field, minimum, maximum = _FIELD_LIMITS[position]
    for value in (minimum - 1, maximum + 1):
        if value < 0:
            continue
        with pytest.raises(SettingsError) as excinfo:
            parse_flip_request(_tokens_with(position, str(value), length))
        assert excinfo.value.field == field


@pytest.mark.parametrize(("length", "position"), _SHAPES)
def test_values_on_the_limits_are_accepted_in_every_shape(length: int, position: int) -> None:
    _, minimum, maximum = _FIELD_LIMITS[position]
    for value in (minimum, maximum):
        request = parse_flip_request(_tokens_with(position, str(value), length))
        assert (request.wager, request.x, request.y, request.z)[position] == value


def test_the_first_bad_field_is_the_one_reported() -> None:
    with pytest.raises(SettingsError) as excinfo:
        parse_flip_request(["abc", "99", "99", "0"])
    assert excinfo.value.field == "Wager"
    with pytest.raises(SettingsError) as excinfo:
        parse_flip_request(["50", "99", "99", "0"])
    assert excinfo.value.field == "Private cards"
    with pytest.raises(SettingsError) as excinfo:
        parse_flip_request(["50", "5", "99", "0"])
    assert excinfo.value.field == "Extra cards"


@pytest.mark.parametrize("tokens", [["50", "five"], ["abc", "5"], ["50", "5", "-2"]])
def test_short_forms_still_reject_bad_values(tokens: list[str]) -> None:
    with pytest.raises(SettingsError):
        parse_flip_request(tokens)


def test_a_bad_y_is_caught_when_z_is_absent() -> None:
    with pytest.raises(SettingsError) as excinfo:
        parse_flip_request(["50", "5", "22"])
    assert excinfo.value.field == "Extra cards"


def test_a_zero_z_is_rejected() -> None:
    with pytest.raises(SettingsError) as excinfo:
        parse_flip_request(["50", "5", "2", "0"])
    assert excinfo.value.field == "Scoop multiplier"


# --- FlipRequest validates what it is given ------------------------------------

_REQUEST_VALID: dict[str, int | None] = {"wager": 50, "x": 5, "y": 2, "z": 3}


def _request_with(field: str, value: object) -> FlipRequest:
    arguments: dict[str, object] = {**_REQUEST_VALID, _KEYWORD[field]: value}
    return FlipRequest(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_request_accepts_each_fields_exact_minimum_and_maximum(
    field: str, minimum: int, maximum: int
) -> None:
    assert getattr(_request_with(field, minimum), _KEYWORD[field]) == minimum
    assert getattr(_request_with(field, maximum), _KEYWORD[field]) == maximum


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_request_rejects_just_above_each_fields_maximum(
    field: str, minimum: int, maximum: int
) -> None:
    with pytest.raises(SettingsError) as excinfo:
        _request_with(field, maximum + 1)
    assert excinfo.value.field == field


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_request_rejects_just_below_each_fields_minimum(
    field: str, minimum: int, maximum: int
) -> None:
    with pytest.raises(SettingsError) as excinfo:
        _request_with(field, minimum - 1)
    assert excinfo.value.field == field


@pytest.mark.parametrize("field", SETTING_FIELDS)
@pytest.mark.parametrize("value", [5.0, "5"], ids=["float", "str"])
def test_request_rejects_a_value_that_is_not_an_integer(field: str, value: object) -> None:
    with pytest.raises(SettingsError) as excinfo:
        _request_with(field, value)
    assert excinfo.value.field == field


@pytest.mark.parametrize("field", SETTING_FIELDS)
def test_request_rejects_true_though_bool_is_an_int(field: str) -> None:
    with pytest.raises(SettingsError) as excinfo:
        _request_with(field, True)
    assert excinfo.value.field == field


@pytest.mark.parametrize("field", ["Wager", "Private cards"])
def test_request_rejects_none_for_the_wager_and_x(field: str) -> None:
    with pytest.raises(SettingsError) as excinfo:
        _request_with(field, None)
    assert excinfo.value.field == field


def test_request_accepts_none_for_y_and_z() -> None:
    assert FlipRequest(50, 5) == FlipRequest(50, 5, None, None)
    assert FlipRequest(50, 5, y=None, z=None).y is None
    assert FlipRequest(50, 5, y=2, z=None).z is None


def test_request_is_frozen() -> None:
    request = FlipRequest(50, 5)
    with pytest.raises(AttributeError):
        request.wager = 1  # type: ignore[misc]


# --- resolve -------------------------------------------------------------------


@pytest.mark.parametrize("players", [2, 3, 8])
def test_resolve_fills_in_y_as_the_maximum_and_z_as_the_player_count(players: int) -> None:
    settings = FlipRequest(50, 5).resolve(players)
    assert settings == Settings(wager=50, x=5, y=21, z=players)
    assert settings.y == Y_DEFAULT == Y_MAX


@pytest.mark.parametrize("players", [2, 3, 8])
def test_resolve_fills_in_only_z_when_y_is_given(players: int) -> None:
    assert FlipRequest(50, 5, 2).resolve(players) == Settings(wager=50, x=5, y=2, z=players)


@pytest.mark.parametrize("players", [2, 3, 8])
def test_resolve_passes_given_values_through_untouched(players: int) -> None:
    assert FlipRequest(50, 5, 0, 7).resolve(players) == Settings(wager=50, x=5, y=0, z=7)


@pytest.mark.parametrize("players", [1, 2, 3, 8])
@pytest.mark.parametrize(
    "settings",
    [Settings(50, 5, 2, 3), Settings(1, 21, 0, 1), Settings(1_000_000, 5, 21, 100)],
)
def test_explicit_settings_round_trip_through_a_request(settings: Settings, players: int) -> None:
    assert FlipRequest.from_settings(settings).resolve(players) == settings


@pytest.mark.parametrize("players", [0, -1, -10])
def test_resolve_rejects_a_player_count_below_one(players: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        FlipRequest(50, 5).resolve(players)
    with pytest.raises(ValueError, match="at least 1"):
        FlipRequest(50, 5, 2, 3).resolve(players)


def test_a_defaulted_z_follows_the_player_count_and_a_given_z_does_not() -> None:
    defaulted = FlipRequest(50, 5, 2)
    given = FlipRequest(50, 5, 2, 3)
    assert defaulted.resolve(2) != defaulted.resolve(3)
    assert given.resolve(2) == given.resolve(3)


def test_a_defaulted_y_resolves_the_same_at_any_table_size() -> None:
    request = FlipRequest(50, 5, None, 3)
    assert request.resolve(2).y == request.resolve(8).y == Y_DEFAULT


def test_a_typed_out_flip_resolves_equal_to_its_defaulted_twin_at_that_table() -> None:
    assert FlipRequest(50, 5).resolve(3) == FlipRequest(50, 5, 21, 3).resolve(3)


# --- from_settings -------------------------------------------------------------

_FULL = Settings(wager=50, x=5, y=21, z=3)


@pytest.mark.parametrize(
    ("y_defaulted", "z_defaulted", "y", "z"),
    [
        pytest.param(False, False, 21, 3, id="neither"),
        pytest.param(True, False, None, 3, id="y-only"),
        pytest.param(False, True, 21, None, id="z-only"),
        pytest.param(True, True, None, None, id="both"),
    ],
)
def test_from_settings_blanks_the_defaulted_values(
    y_defaulted: bool, z_defaulted: bool, y: int | None, z: int | None
) -> None:
    request = FlipRequest.from_settings(_FULL, y_defaulted=y_defaulted, z_defaulted=z_defaulted)
    assert request == FlipRequest(50, 5, y, z)
    assert request.y_defaulted is y_defaulted
    assert request.z_defaulted is z_defaulted


def test_from_settings_defaults_to_nothing_defaulted() -> None:
    request = FlipRequest.from_settings(_FULL)
    assert not request.y_defaulted
    assert not request.z_defaulted


@pytest.mark.parametrize("y_defaulted", [False, True])
@pytest.mark.parametrize("z_defaulted", [False, True])
def test_from_settings_then_resolve_gives_back_the_settings(
    y_defaulted: bool, z_defaulted: bool
) -> None:
    request = FlipRequest.from_settings(_FULL, y_defaulted=y_defaulted, z_defaulted=z_defaulted)
    assert request.resolve(3) == _FULL


# --- max_players ---------------------------------------------------------------


@pytest.mark.parametrize("x", range(X_MIN, X_MAX + 1))
def test_request_max_players_matches_the_function_and_settings(x: int) -> None:
    request = FlipRequest(50, x)
    assert request.max_players == max_players(x) == 42 // x
    assert request.max_players == Settings(50, x, 2, 1).max_players


def test_request_max_players_documented_values() -> None:
    assert FlipRequest(50, 5).max_players == 8
    assert FlipRequest(50, 21).max_players == 2


def test_setting_fields_order() -> None:
    assert SETTING_FIELDS == ("Wager", "Private cards", "Extra cards", "Scoop multiplier")


def test_max_players_documented_values() -> None:
    assert max_players(5) == 8
    assert max_players(21) == 2


def test_max_players_shrinks_as_x_grows() -> None:
    values = [max_players(x) for x in range(X_MIN, X_MAX + 1)]
    assert values == sorted(values, reverse=True)
    for x in range(X_MIN, X_MAX + 1):
        assert max_players(x) == 42 // x


def test_max_players_rejects_non_positive_x() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        max_players(0)


def test_settings_max_players_property() -> None:
    settings = Settings(wager=50, x=5, y=0, z=1)
    assert settings.max_players == max_players(5) == 8


def test_settings_pot() -> None:
    settings = Settings(wager=50, x=5, y=0, z=1)
    assert settings.pot(3) == 150


def test_settings_pot_rejects_non_positive_player_count() -> None:
    settings = Settings(wager=50, x=5, y=0, z=1)
    with pytest.raises(ValueError, match="at least 1"):
        settings.pot(0)


# --- Settings validates itself -------------------------------------------------

_VALID: dict[str, int] = {"wager": 50, "x": 5, "y": 2, "z": 3}


def _settings_with(field: str, value: object) -> Settings:
    """Settings whose one named field is ``value``; the rest are valid."""
    arguments: dict[str, object] = {**_VALID, _KEYWORD[field]: value}
    return Settings(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_settings_accepts_each_fields_exact_minimum_and_maximum(
    field: str, minimum: int, maximum: int
) -> None:
    assert getattr(_settings_with(field, minimum), _KEYWORD[field]) == minimum
    assert getattr(_settings_with(field, maximum), _KEYWORD[field]) == maximum


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_settings_rejects_just_above_each_fields_maximum(
    field: str, minimum: int, maximum: int
) -> None:
    with pytest.raises(SettingsError) as excinfo:
        _settings_with(field, maximum + 1)
    assert excinfo.value.field == field


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_settings_rejects_just_below_each_fields_minimum(
    field: str, minimum: int, maximum: int
) -> None:
    with pytest.raises(SettingsError) as excinfo:
        _settings_with(field, minimum - 1)
    assert excinfo.value.field == field


@pytest.mark.parametrize("field", SETTING_FIELDS)
@pytest.mark.parametrize("value", [5.0, "5", None], ids=["float", "str", "none"])
def test_settings_rejects_a_value_that_is_not_an_integer(field: str, value: object) -> None:
    with pytest.raises(SettingsError) as excinfo:
        _settings_with(field, value)
    assert excinfo.value.field == field


@pytest.mark.parametrize("field", SETTING_FIELDS)
def test_settings_rejects_true_though_bool_is_an_int(field: str) -> None:
    # True == 1 is inside the wager's and the scoop multiplier's limits, so only the type check
    # stops it.
    with pytest.raises(SettingsError) as excinfo:
        _settings_with(field, True)
    assert excinfo.value.field == field


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_settings_error_is_a_value_error_and_a_sentence_naming_the_field(
    field: str, minimum: int, maximum: int
) -> None:
    with pytest.raises(ValueError, match=field) as excinfo:
        _settings_with(field, maximum + 1)
    error = excinfo.value
    assert isinstance(error, SettingsError)
    assert error.field in SETTING_FIELDS
    assert error.message == str(error)
    assert str(error).startswith(f"{field} must be ")
    assert str(error).endswith(".")
    assert f"{minimum:,}" in str(error)
    assert f"{maximum:,}" in str(error)


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_parse_settings_builds_the_same_settings_at_the_boundaries(
    field: str, minimum: int, maximum: int
) -> None:
    for value in (minimum, maximum):
        text = {"wager": "50", "x": "5", "y": "2", "z": "3"}
        text[_KEYWORD[field]] = str(value)
        assert parse_settings(**text) == _settings_with(field, value)


# --- messages name settings in words, never by letter --------------------------

_BARE_LETTER = re.compile(r"(?<![0-9A-Za-z])[XYZ](?![0-9A-Za-z])")


def _error_messages(field: str, minimum: int, maximum: int) -> list[str]:
    """Every kind of complaint rules.py can make about one field, as message text."""
    keyword = _KEYWORD[field]
    position = SETTING_FIELDS.index(field)
    good = ["50", "5", "2", "3"]
    messages = [
        _raise_text(lambda: parse_whole_number("", field=field, minimum=minimum, maximum=maximum)),
        _raise_text(
            lambda: parse_whole_number("abc", field=field, minimum=minimum, maximum=maximum)
        ),
        _raise_text(
            lambda: parse_whole_number(
                str(maximum + 1), field=field, minimum=minimum, maximum=maximum
            )
        ),
        _raise_text(lambda: _settings_with(field, maximum + 1)),
        _raise_text(lambda: _settings_with(field, True)),
        _raise_text(lambda: _request_with(field, maximum + 1)),
        _raise_text(lambda: parse_settings(**{**_TEXT, keyword: str(maximum + 1)})),
        _raise_text(lambda: parse_flip_request([*good[:position], "abc", *good[position + 1 :]])),
    ]
    return messages


_TEXT = {"wager": "50", "x": "5", "y": "2", "z": "3"}


def _raise_text(call: Callable[[], object]) -> str:
    with pytest.raises(SettingsError) as excinfo:
        call()
    return str(excinfo.value)


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_no_message_names_a_setting_by_its_letter(field: str, minimum: int, maximum: int) -> None:
    for message in _error_messages(field, minimum, maximum):
        assert field in message
        assert _BARE_LETTER.search(message) is None, message


@pytest.mark.parametrize("count", [0, 1, 5])
def test_the_wrong_count_message_names_no_setting_by_its_letter(count: int) -> None:
    message = _raise_text(lambda: parse_flip_request(["5"] * count))
    assert _BARE_LETTER.search(message) is None, message


def test_the_letter_pattern_finds_a_bare_letter_and_nothing_else() -> None:
    for bare in ("X must be", "Set Y.", "(Z)", "x2 and Z"):
        assert _BARE_LETTER.search(bare), bare
    for clean in ("Extra cards", "XL", "5X", "Wager 5 Zoo"):
        assert _BARE_LETTER.search(clean) is None, clean


def test_setting_names_are_the_words_the_rest_of_the_bot_uses() -> None:
    assert SETTING_FIELDS == ("Wager", "Private cards", "Extra cards", "Scoop multiplier")
    for name in SETTING_FIELDS:
        assert len(name) > 1
        assert name[0].isupper()
