from dataclasses import asdict, dataclass


@dataclass
class QuestionConfig:
    question_id: str
    columns: list[str]
    max_points: float = 10.0
    rubric: str = ""
    model_answer: str = ""
    extra_instructions: str = ""
    rubric_preset: str = ""
    question_title: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def auto_map_questions(response_columns: list[str], start_index: int = 1) -> list[QuestionConfig]:
    out: list[QuestionConfig] = []
    qnum = start_index
    for col in response_columns:
        out.append(
            QuestionConfig(
                question_id=f"Q{qnum}",
                columns=[col],
                max_points=10.0,
            )
        )
        qnum += 1
    return out


def next_question_id(existing: list[QuestionConfig]) -> str:
    n = 1
    used = {q.question_id.upper() for q in existing}
    while f"Q{n}" in used:
        n += 1
    return f"Q{n}"

