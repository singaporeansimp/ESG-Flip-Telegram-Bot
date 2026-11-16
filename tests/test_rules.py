from __future__ import annotations

import pytest

from esgbot.game.rules import (
    SETTING_FIELDS,
    WAGER_MAX,
    WAGER_MIN,
    X_MAX,
    X_MIN,
    Y_MAX,
    Y_MIN,
    Z_MAX,
    Z_MIN,
    Settings,
    SettingsError,
    max_players,
    parse_settings,
    parse_settings_tokens,
    parse_whole_number,
)

_FIELD_LIMITS: tuple[tuple[str, int, int], ...] = (
    ("wager", WAGER_MIN, WAGER_MAX),
    ("X", X_MIN, X_MAX),
    ("Y", Y_MIN, Y_MAX),
    ("Z", Z_MIN, Z_MAX),
)


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
        parse_whole_number(text, field="wager", minimum=WAGER_MIN, maximum=WAGER_MAX)


def test_parse_whole_number_rejects_empty_text() -> None:
    with pytest.raises(SettingsError, match="missing"):
        parse_whole_number("", field="wager", minimum=WAGER_MIN, maximum=WAGER_MAX)


def test_parse_whole_number_error_field_matches() -> None:
    error = None
    try:
        parse_whole_number("$50", field="wager", minimum=WAGER_MIN, maximum=WAGER_MAX)
    except SettingsError as caught:
        error = caught
    assert error is not None
    assert error.field == "wager"
    assert error.message == str(error)


@pytest.mark.parametrize(("field", "minimum", "maximum"), _FIELD_LIMITS)
def test_settings_error_field_name_from_parse_settings(
    field: str, minimum: int, maximum: int
) -> None:
    valid = {"wager": "50", "x": "5", "y": "2", "z": "3"}
    valid[field.lower()] = str(maximum + 1)
    with pytest.raises(SettingsError) as excinfo:
        parse_settings(**valid)
    assert excinfo.value.field == field


def test_parse_settings_builds_settings() -> None:
    settings = parse_settings(wager="50", x="5", y="2", z="3")
    assert settings == Settings(wager=50, x=5, y=2, z=3)


def test_parse_settings_tokens_matches_parse_settings() -> None:
    settings = parse_settings_tokens(["50", "5", "2", "3"])
    assert settings == parse_settings(wager="50", x="5", y="2", z="3")


@pytest.mark.parametrize("tokens", [[], ["50"], ["50", "5", "2"], ["50", "5", "2", "3", "4"]])
def test_parse_settings_tokens_rejects_wrong_count(tokens: list[str]) -> None:
    with pytest.raises(SettingsError, match="four whole numbers"):
        parse_settings_tokens(tokens)


def test_setting_fields_order() -> None:
    assert SETTING_FIELDS == ("wager", "X", "Y", "Z")


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
_KEYWORD: dict[str, str] = {"wager": "wager", "X": "x", "Y": "y", "Z": "z"}


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
    # True == 1 is inside the limits of wager and Z, so only the type check stops it.
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
