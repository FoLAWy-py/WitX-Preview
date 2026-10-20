"""Matching a searched app name against the apps installed on a device."""

from witx_gateway import apps


def test_the_app_whose_id_or_name_holds_the_words_comes_first() -> None:
    installed = {
        "com.huawei.calculator.widget": None,
        "com.huawei.calculator": None,
        "com.android.settings": None,
        "com.apple.reminders": "提醒事项",
    }
    assert apps.match("计算器 calculator", installed) == [
        ("com.huawei.calculator", None),  # the main app before its plug-in
        ("com.huawei.calculator.widget", None),
    ]
    assert apps.match("提醒事项", installed) == [("com.apple.reminders", "提醒事项")]


def test_words_found_in_every_app_id_match_nothing() -> None:
    assert apps.match("android app", {"com.android.settings": None}) == []


def test_an_app_named_by_a_word_of_the_query_comes_before_one_that_only_contains_it() -> None:
    installed = {"com.ownbook.notes": "simNote", "com.apple.mobilenotes": "Notes"}
    assert apps.match("Apple Notes 备忘录 notes", installed)[0] == (
        "com.apple.mobilenotes",
        "Notes",
    )
