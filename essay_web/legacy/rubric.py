"""Pure rubric/prompt helpers adapted from the desktop grader; no API client."""


import math
import re
from typing import Any


DEFAULT_SYSTEM_INSTRUCTION = (
    "You are a strict but fair grading assistant. Grade only against the supplied rubric. "
    "Do not invent evidence. Justify each criterion score briefly and clearly."
)


def build_user_content(
    *,
    rubric: str,
    model_answer: str,
    extra_instructions: str,
    student_response: str,
    question_id: str,
    question_title: str,
    max_points: float,
) -> str:
    effective_max_points = resolve_effective_max_points(rubric, max_points)
    return (
        "Grade this single response strictly using the provided rubric.\n\n"
        f"Question ID: {question_id}\n"
        f"Question title: {question_title or ''}\n"
        f"Configured max points: {max_points}\n"
        f"Effective max points: {effective_max_points}\n\n"
        "=== RUBRIC ===\n"
        f"{rubric}\n\n"
        "=== MODEL ANSWER (optional reference) ===\n"
        f"{model_answer or '(none)'}\n\n"
        "=== EXTRA INSTRUCTIONS ===\n"
        f"{extra_instructions or '(none)'}\n\n"
        "=== STUDENT RESPONSE ===\n"
        f"{student_response}\n\n"
        "Use the rubric's criterion point values as authoritative whenever they differ from the configured max points. "
        "Make each criterion's points_awarded consistent with its rubric allocation, and make score_total equal the sum of the criterion points_awarded. "
        "Return ONLY a JSON object matching the schema."
    )


def estimate_text_tokens(text: str) -> int:
    return max(1, math.ceil(len(text or "") / 4))


def estimate_criteria_count(rubric: str) -> int:
    lines = [line.strip() for line in (rubric or "").splitlines() if line.strip()]
    bullet_like = sum(
        1
        for line in lines
        if line.startswith(("-", "*"))
        or (len(line) > 1 and line[0].isdigit() and line[1] in (".", ")"))
    )
    if bullet_like:
        return min(max(bullet_like, 1), 8)
    return min(max(len(lines) // 3, 1), 5) if lines else 3


def estimate_output_tokens(rubric: str) -> int:
    criteria_count = estimate_criteria_count(rubric)
    return 90 + (criteria_count * 45)


def estimate_call_tokens(
    *,
    system_instruction: str,
    rubric: str,
    model_answer: str,
    extra_instructions: str,
    student_response: str,
    question_id: str,
    question_title: str,
    max_points: float,
) -> tuple[int, int]:
    user_content = build_user_content(
        rubric=rubric,
        model_answer=model_answer,
        extra_instructions=extra_instructions,
        student_response=student_response,
        question_id=question_id,
        question_title=question_title,
        max_points=max_points,
    )
    input_tokens = estimate_text_tokens(system_instruction) + estimate_text_tokens(user_content)
    output_tokens = estimate_output_tokens(rubric)
    return input_tokens, output_tokens


def _clean_criterion_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip(" -:\t")).strip()


def extract_rubric_criteria(rubric: str) -> list[dict[str, float | str]]:
    criteria: list[dict[str, float | str]] = []
    seen: set[tuple[str, float]] = set()
    pattern = re.compile(
        r"(?P<name>.+?)\s*\((?P<points>\d+(?:\.\d+)?)\s*points?\s*(?:possible|max(?:imum)?)\)",
        re.IGNORECASE,
    )
    for raw_line in (rubric or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = pattern.search(line)
        if not match:
            continue
        name = _clean_criterion_name(match.group("name"))
        points = float(match.group("points"))
        if not name:
            continue
        key = (name.casefold(), points)
        if key in seen:
            continue
        seen.add(key)
        criteria.append({"name": name, "max_points": points})
    return criteria


def resolve_effective_max_points(rubric: str, configured_max_points: float) -> float:
    rubric_criteria = extract_rubric_criteria(rubric)
    if rubric_criteria:
        return float(sum(float(c["max_points"]) for c in rubric_criteria))
    return float(configured_max_points)


def _schema(max_points: float) -> dict[str, Any]:
    return {
        "name": "question_grading_result",
        "schema": {
            "type": "object",
            "properties": {
                "score_total": {"type": "number", "minimum": 0, "maximum": max_points},
                "criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "points_awarded": {"type": "number"},
                            "justification": {"type": "string"},
                        },
                        "required": ["name", "points_awarded", "justification"],
                        "additionalProperties": False,
                    },
                },
                "feedback": {"type": "string"},
                "flags": {
                    "type": "object",
                    "properties": {
                        "off_topic": {"type": "boolean"},
                        "too_short": {"type": "boolean"},
                        "suspected_plagiarism": {"type": "boolean"},
                        "policy_issue": {"type": "boolean"},
                    },
                    "required": ["off_topic", "too_short", "suspected_plagiarism", "policy_issue"],
                    "additionalProperties": False,
                },
            },
            "required": ["score_total", "criteria", "feedback", "flags"],
            "additionalProperties": False,
        },
        "strict": True,
    }
