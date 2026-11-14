"""Shared fact classification contracts."""
from typing import Literal, get_args

Category = Literal[
    "personal_details", "family", "professional_details", "sports", "travel",
    "food", "music", "health", "technology", "hobbies", "fashion",
    "entertainment", "milestones", "user_preferences", "misc",
]
CATEGORIES = get_args(Category)


def normalize_categories(values: list[Category] | tuple[Category, ...]) -> list[Category]:
    if not isinstance(values, (list, tuple)) or any(value not in CATEGORIES for value in values):
        raise ValueError("Invalid fact categories")
    return list(dict.fromkeys(values))


def validate_categories(values: list[Category]) -> None:
    if normalize_categories(values) != values:
        raise ValueError("Fact categories must be a list of unique labels")
