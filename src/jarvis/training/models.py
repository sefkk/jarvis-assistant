"""Data models for the training log.

`WorkoutInput` and `SetInput` are what the model fills from the user's natural-language description;
their field descriptions carry the mapping rules. `WorkoutRow`/`SetRow` are what the store returns.
`HealthWorkout` is the validated shape every Health adapter produces before anything is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.security import sanitize

MAX_TEXT_CHARS = 200  # type / exercise
MAX_NOTES_CHARS = 2000
MAX_SETS = 200

# Common Turkish and English words -> one canonical type. Unknown words are stored as the user said them.
TYPE_ALIASES: dict[str, str] = {
    # running
    "koşu": "running",
    "kosu": "running",
    "koşmak": "running",
    "koştum": "running",
    "run": "running",
    "running": "running",
    "jog": "running",
    "jogging": "running",
    "outdoor run": "running",
    "indoor run": "running",
    "treadmill": "running",
    # walking
    "yürüyüş": "walking",
    "yuruyus": "walking",
    "yürüdüm": "walking",
    "walk": "walking",
    "walking": "walking",
    "outdoor walk": "walking",
    "indoor walk": "walking",
    "hike": "hiking",
    "hiking": "hiking",
    # cycling
    "bisiklet": "cycling",
    "bike": "cycling",
    "biking": "cycling",
    "cycle": "cycling",
    "cycling": "cycling",
    "outdoor cycle": "cycling",
    "indoor cycle": "cycling",
    "spinning": "cycling",
    # strength
    "ağırlık": "strength",
    "agirlik": "strength",
    "kuvvet": "strength",
    "weights": "strength",
    "weight training": "strength",
    "weightlifting": "strength",
    "lifting": "strength",
    "gym": "strength",
    "strength": "strength",
    "strength training": "strength",
    "traditional strength training": "strength",
    "functional strength training": "strength",
    "resistance": "strength",
    # hiit
    "hiit": "hiit",
    "interval": "hiit",
    "intervals": "hiit",
    "high intensity interval training": "hiit",
    # others worth normalizing
    "sprint": "sprint",
    "sprints": "sprint",
    "sürat": "sprint",
    "surat": "sprint",
    "yüzme": "swimming",
    "yuzme": "swimming",
    "swim": "swimming",
    "swimming": "swimming",
    "futbol": "football",
    "soccer": "football",
    "football": "football",
    "basketbol": "basketball",
    "basketball": "basketball",
    "tenis": "tennis",
    "tennis": "tennis",
    "yoga": "yoga",
    "pilates": "pilates",
    "core training": "core",
    "core": "core",
    "esneme": "stretching",
    "stretching": "stretching",
    "flexibility": "stretching",
    "mobility": "stretching",
    "kürek": "rowing",
    "rowing": "rowing",
    "elliptical": "elliptical",
    "stair climbing": "stairs",
    "stairs": "stairs",
    "cooldown": "cooldown",
    "mixed cardio": "cardio",
    "cardio": "cardio",
}

_SPACE_RE = re.compile(r"\s+")


def normalize_text(text: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Lowercase, single spaces, no control or invisible characters, capped length."""
    cleaned = sanitize.for_model(text or "")
    cleaned = _SPACE_RE.sub(" ", cleaned).strip().lower()
    return cleaned[:max_chars]


def normalize_type(text: str) -> str:
    """Free text -> canonical workout type when known, otherwise the cleaned text itself."""
    cleaned = normalize_text(text)
    return TYPE_ALIASES.get(cleaned, cleaned)


def normalize_exercise(text: str) -> str:
    return normalize_text(text)


def clean_notes(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = sanitize.for_model(text).strip()
    return cleaned[:MAX_NOTES_CHARS] or None


# --- model input ------------------------------------------------------------------------


class SetInput(BaseModel):
    """One set, rep, interval or drill inside a workout."""

    model_config = ConfigDict(extra="forbid")

    exercise: str = Field(
        description=(
            "What was done, lowercase free text, e.g. 'sprint', 'squat', 'bench press', '60m'. "
            "Repeat the same exercise name for every set of that exercise."
        )
    )
    reps: int | None = Field(default=None, ge=0, description="Repetitions in this set (gym sets). Omit for distance reps.")
    distance_m: float | None = Field(default=None, ge=0, description="Distance of this rep in metres (60 for '60m').")
    time_s: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Time of this rep in seconds. Only when the user gave a time for THIS set. "
            "If only a best time was given ('6x60m, best 7.1'), put it on exactly one set with is_best=true "
            "and leave time_s empty on the others. Never invent times."
        ),
    )
    weight_kg: float | None = Field(default=None, ge=0, description="Load in kilograms (gym sets).")
    rest_s: float | None = Field(default=None, ge=0, description="Rest after this set in seconds, if the user said so.")
    is_best: bool = Field(default=False, description="True for the set the user called the best / fastest one.")
    notes: str | None = Field(default=None, description="Anything else said about this set.")

    @field_validator("exercise")
    @classmethod
    def _exercise(cls, value: str) -> str:
        cleaned = normalize_exercise(value)
        if not cleaned:
            raise ValueError("exercise must not be empty")
        return cleaned

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: str | None) -> str | None:
        return clean_notes(value)


class WorkoutInput(BaseModel):
    """One training session as described by the user.

    How to map natural language:
    - '6x60m, best 7.1'  -> type='sprint', six sets with exercise='sprint', distance_m=60; time_s=7.1 only on
      ONE set with is_best=true; the other five sets have no time_s (never invent times).
    - '3x10 squat 60kg'  -> type='strength', three sets exercise='squat', reps=10, weight_kg=60.
    - 'ran 5 km in 25 min' -> type='running', no sets, distance_m=5000, duration_min=25.
    - '45 min bike'       -> type='cycling', duration_min=45.
    Turkish words are fine (koşu, yürüyüş, bisiklet, ağırlık, kuvvet); they are normalized.
    """

    model_config = ConfigDict(extra="forbid")

    date: str | None = Field(
        default=None,
        description=(
            "When it happened: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' in the user's local time. "
            "Omit for 'today/now'. 'yesterday' -> yesterday's date."
        ),
    )
    type: str = Field(
        description=(
            "Kind of session, lowercase free text: running, walking, cycling, strength, hiit, sprint, swimming, "
            "football, yoga... Use what the user said if it is none of these."
        )
    )
    sets: list[SetInput] = Field(
        default_factory=list,
        max_length=MAX_SETS,
        description="Sets, reps, intervals or drills (one entry per set). Empty for a plain run/walk/ride.",
    )
    duration_min: float | None = Field(default=None, ge=0, description="Total session duration in minutes, if given.")
    distance_m: float | None = Field(
        default=None, ge=0, description="Total session distance in metres (5 km -> 5000), if given and not per set."
    )
    notes: str | None = Field(default=None, description="How it felt, conditions, anything else the user said.")

    @field_validator("type")
    @classmethod
    def _type(cls, value: str) -> str:
        cleaned = normalize_type(value)
        if not cleaned:
            raise ValueError("type must not be empty")
        return cleaned

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: str | None) -> str | None:
        return clean_notes(value)

    @field_validator("date")
    @classmethod
    def _date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


# --- validated Health record (every adapter produces this) ----------------------------------


class HealthWorkout(BaseModel):
    """A workout from the iPhone, validated before it touches the database."""

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    started_at: datetime
    ended_at: datetime | None = None
    duration_s: float | None = Field(default=None, ge=0, le=7 * 24 * 3600)
    distance_m: float | None = Field(default=None, ge=0, le=5_000_000)
    energy_kcal: float | None = Field(default=None, ge=0, le=100_000)
    avg_hr: float | None = Field(default=None, ge=0, le=300)
    max_hr: float | None = Field(default=None, ge=0, le=300)
    notes: str | None = Field(default=None, max_length=MAX_NOTES_CHARS)

    @field_validator("started_at", "ended_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must carry a UTC offset")
        return value


# --- rows returned by the store -------------------------------------------------------------


@dataclass
class SetRow:
    id: int
    workout_id: int
    exercise: str
    reps: int | None
    distance_m: float | None
    time_s: float | None
    weight_kg: float | None
    rest_s: float | None
    is_best: bool
    notes: str | None


@dataclass
class WorkoutRow:
    id: int
    source: str
    external_id: str | None
    started_at: datetime
    ended_at: datetime | None
    type: str
    duration_s: float | None
    distance_m: float | None
    energy_kcal: float | None
    avg_hr: float | None
    max_hr: float | None
    notes: str | None
    created_at: str
    sets: list[SetRow] = field(default_factory=list)
