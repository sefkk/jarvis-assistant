from io import StringIO

from rich.console import Console

from jarvis.profile import Profile, load_profile, parse_profile


def test_profile_round_trip(tmp_path):
    p = Profile(
        fields={
            "name": "Kerem Yılmaz",
            "address_as": "Kerem",
            "job": "dev",
            "location": "Istanbul",
            "languages": "Turkish, English",
            "weather_location": "Istanbul",
        },
        preferences="short answers\nno emoji",
    )
    path = tmp_path / "profile.md"
    path.write_text(p.to_markdown())
    loaded = load_profile(path)
    assert loaded.fields == p.fields and loaded.preferences == "short answers\nno emoji" and loaded.name == "Kerem"
    assert load_profile(tmp_path / "missing.md") is None
    assert parse_profile("- Name: X\n").name == "X"


def test_the_turkish_template_labels_mean_the_same_fields():
    text = "# Profil\n\n- Ad: Ada Örnek\n- Hitap: Ada\n- Konum: İzmir\n- Hava durumu konumu: İzmir\n\n## Tercihler\n\nkısa cevaplar\n"
    profile = parse_profile(text)
    assert profile.fields == {"name": "Ada Örnek", "address_as": "Ada", "location": "İzmir", "weather_location": "İzmir"}
    assert profile.name == "Ada" and profile.preferences == "kısa cevaplar"


def test_setup_budget_question_rejects_bad_input():
    from jarvis.setup_wizard import AnswersPrompter, ask_budget

    answers = iter(["abc", "-2", "$1,5"])

    class Scripted(AnswersPrompter):
        def ask(self, question, default=""):
            return next(answers)

    console = Console(file=StringIO(), force_terminal=False)
    assert ask_budget(Scripted(console, {}), 3.0) == 1.5
