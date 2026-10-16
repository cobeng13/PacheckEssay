import csv
import json
from pathlib import Path
from statistics import mean
from typing import Any

from .csv_ingest import StudentRecord
from .questions import QuestionConfig
from .utils import now_timestamp, sanitize_filename


def prepare_run_dir(base_output_dir: str, timestamped_subfolder: bool, resume_existing: bool = False) -> Path:
    root = Path(base_output_dir)
    if timestamped_subfolder:
        if resume_existing:
            candidates = [p for p in root.glob("output_run_*") if p.is_dir()]
            if candidates:
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                root = candidates[0]
            else:
                root = root / f"output_run_{now_timestamp()}"
        else:
            root = root / f"output_run_{now_timestamp()}"
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "per_student").mkdir(parents=True, exist_ok=True)
    (root / "per_question").mkdir(parents=True, exist_ok=True)
    return root


def write_log(log_path: Path, line: str):
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def question_txt(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"{result['student_name']}")
    lines.append(f"Question: {result['question_id']}")
    lines.append(f"Score: {result['score_total']} / {result['max_points']}")
    lines.append("")
    lines.append("Criteria:")
    for c in result.get("criteria", []):
        lines.append(f"- {c.get('name', '')}: {c.get('points_awarded', '')}")
        lines.append(f"  Why: {c.get('justification', '')}")
    lines.append("")
    lines.append("Feedback:")
    lines.append(result.get("feedback", ""))
    lines.append("")
    flags = result.get("flags", {})
    lines.append("Flags:")
    lines.append(f"- Off topic: {flags.get('off_topic', False)}")
    lines.append(f"- Too short: {flags.get('too_short', False)}")
    lines.append(f"- Suspected plagiarism: {flags.get('suspected_plagiarism', False)}")
    lines.append(f"- Policy issue: {flags.get('policy_issue', False)}")
    lines.append("----")
    return "\n".join(lines) + "\n"


def combined_student_txt(student: StudentRecord, results: list[dict[str, Any]]) -> str:
    total_score = sum(float(r.get("score_total", 0) or 0) for r in results)
    total_max = sum(float(r.get("max_points", 0) or 0) for r in results)
    lines: list[str] = [student.display_name]
    lines.append(f"Total: {total_score} / {total_max}")
    lines.append("")
    for r in sorted(results, key=lambda x: x.get("question_id", "")):
        lines.append(f"{r.get('question_id', '')}: {r.get('score_total', 0)} / {r.get('max_points', 0)}")
        lines.append("Feedback:")
        lines.append(r.get("feedback", ""))
        lines.append("")
    lines.append("----")
    return "\n".join(lines) + "\n"


def overall_feedback_text(results: list[dict[str, Any]]) -> str:
    return " ".join([r.get("feedback", "").strip() for r in results if r.get("feedback", "").strip()])


def per_question_json_path(run_dir: Path, question_id: str, safe_stem: str) -> Path:
    qdir = run_dir / "per_question" / question_id
    qdir.mkdir(parents=True, exist_ok=True)
    return qdir / f"{safe_stem}.json"


def per_question_txt_path(run_dir: Path, question_id: str, safe_stem: str) -> Path:
    qdir = run_dir / "per_question" / question_id
    qdir.mkdir(parents=True, exist_ok=True)
    return qdir / f"{safe_stem}.txt"


def write_per_question_outputs(
    run_dir: Path, question_id: str, safe_stem: str, result: dict[str, Any], export_txt: bool
) -> Path:
    jp = per_question_json_path(run_dir, question_id, safe_stem)
    with jp.open("w", encoding="utf-8") as jf:
        json.dump(result, jf, ensure_ascii=False, indent=2)
    if export_txt:
        tp = per_question_txt_path(run_dir, question_id, safe_stem)
        tp.write_text(question_txt(result), encoding="utf-8")
    return jp


def load_existing_result(run_dir: Path, question_id: str, safe_stem: str) -> dict[str, Any] | None:
    jp = per_question_json_path(run_dir, question_id, safe_stem)
    if not jp.exists():
        return None
    try:
        return json.loads(jp.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_run_config(run_dir: Path, config: dict[str, Any]):
    path = run_dir / "run_config.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def write_per_student_reports(
    run_dir: Path,
    students: list[StudentRecord],
    questions: list[QuestionConfig],
    results_index: dict[tuple[str, str], dict[str, Any]],
):
    for student in students:
        results = []
        for q in questions:
            r = results_index.get((student.student_id, q.question_id))
            if r:
                results.append(r)
        total_score = sum(float(r.get("score_total", 0) or 0) for r in results)
        total_max = sum(float(r.get("max_points", 0) or 0) for r in results)
        payload = {
            "student": {
                "student_id": student.student_id_raw or "",
                "student_key": student.student_id,
                "name": student.display_name,
            },
            "results": results,
            "totals": {"total_score": total_score, "total_max_points": total_max},
            "overall_feedback": overall_feedback_text(results),
        }
        out_json = run_dir / "per_student" / f"{student.safe_stem}.json"
        out_txt = run_dir / "per_student" / f"{student.safe_stem}.txt"
        out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        out_txt.write_text(combined_student_txt(student, results), encoding="utf-8")


def export_gradebook_final(
    run_dir: Path,
    students: list[StudentRecord],
    questions: list[QuestionConfig],
    results_index: dict[tuple[str, str], dict[str, Any]],
):
    fieldnames = ["StudentID", "Name"]
    for q in questions:
        fieldnames.extend([f"{q.question_id} Score", f"{q.question_id} Feedback"])
    fieldnames.extend(["Total Score", "Total Max Points", "Flags Summary"])

    out_csv = run_dir / "gradebook_final.csv"
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for student in students:
            row: dict[str, Any] = {"StudentID": student.student_id_raw or "", "Name": student.display_name}
            total_score = 0.0
            total_max = 0.0
            flags_parts: list[str] = []
            for q in questions:
                r = results_index.get((student.student_id, q.question_id))
                if r:
                    score = float(r.get("score_total", 0) or 0)
                    total_score += score
                    total_max += float(r.get("max_points", q.max_points) or q.max_points)
                    row[f"{q.question_id} Score"] = score
                    row[f"{q.question_id} Feedback"] = r.get("feedback", "")
                    flags = r.get("flags", {})
                    active = [k for k, v in flags.items() if bool(v)]
                    if active:
                        flags_parts.append(f"{q.question_id}:{'|'.join(active)}")
                else:
                    row[f"{q.question_id} Score"] = ""
                    row[f"{q.question_id} Feedback"] = ""
                    total_max += q.max_points
            row["Total Score"] = total_score
            row["Total Max Points"] = total_max
            row["Flags Summary"] = "; ".join(flags_parts)
            writer.writerow(row)


def export_summary_per_question(
    run_dir: Path,
    students: list[StudentRecord],
    questions: list[QuestionConfig],
    results_index: dict[tuple[str, str], dict[str, Any]],
):
    out_csv = run_dir / "summary_per_question.csv"
    fieldnames = [
        "Question ID",
        "Max points",
        "Num graded",
        "Average score",
        "Num flagged off_topic",
        "Num flagged too_short",
        "Num flagged suspected_plagiarism",
        "Num flagged policy_issue",
    ]
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for q in questions:
            scores: list[float] = []
            counts = {"off_topic": 0, "too_short": 0, "suspected_plagiarism": 0, "policy_issue": 0}
            for s in students:
                r = results_index.get((s.student_id, q.question_id))
                if not r:
                    continue
                scores.append(float(r.get("score_total", 0) or 0))
                flags = r.get("flags", {})
                for key in counts:
                    if bool(flags.get(key, False)):
                        counts[key] += 1
            writer.writerow(
                {
                    "Question ID": q.question_id,
                    "Max points": q.max_points,
                    "Num graded": len(scores),
                    "Average score": round(mean(scores), 4) if scores else "",
                    "Num flagged off_topic": counts["off_topic"],
                    "Num flagged too_short": counts["too_short"],
                    "Num flagged suspected_plagiarism": counts["suspected_plagiarism"],
                    "Num flagged policy_issue": counts["policy_issue"],
                }
            )


def export_lms_upload_csv(
    run_dir: Path,
    students: list[StudentRecord],
    questions: list[QuestionConfig],
    results_index: dict[tuple[str, str], dict[str, Any]],
):
    out_dir = run_dir / "ForLMSUpload"
    out_dir.mkdir(parents=True, exist_ok=True)
    fieldnames = ["name", "mark", "feedback"]
    for q in questions:
        out_csv = out_dir / f"{sanitize_filename(q.question_id)}.csv"
        with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for student in students:
                result = results_index.get((student.student_id, q.question_id))
                writer.writerow(
                    {
                        "name": student.display_name,
                        "mark": float(result.get("score_total", 0) or 0) if result else "",
                        "feedback": result.get("feedback", "") if result else "",
                    }
                )
