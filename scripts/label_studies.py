"""
Standalone executable: converts a hospital node's raw study-record JSON into
a second, richer table with an LLM-derived GenericCategory and structured
FindingTags, using the Claude API.

Run once per node, e.g.:
    HOSPITAL_NODE=BCH python scripts/label_studies.py

Run BCH, then MGH, then BWH IN THAT ORDER (sequentially, not in parallel).
Each run grows the shared scripts/tag_vocabulary.json, and later nodes reuse
the standardized tag wording established by earlier nodes. Running the three
in parallel means the same finding can end up labeled with different wording
on different nodes, which breaks cross-node search.

Safe to interrupt and re-run: already-labeled StudyIDs in the output file are
skipped.

Requires ANTHROPIC_API_KEY to be set in the environment.
"""

import json
import os
import sys
from pathlib import Path

import anthropic
from pydantic import BaseModel

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from models import FindingTag  # noqa: E402
from taxonomy import GenericCategory  # noqa: E402

NODE_DATA_MAP = {
    "BCH": "bch_data",
    "MGH": "mgh_data",
    "BWH": "bwh_data",
}

MODEL = os.environ.get("LABEL_MODEL", "claude-opus-5")
VOCAB_PATH = SCRIPT_DIR / "tag_vocabulary.json"

SYSTEM_PROMPT = """You are a radiology report structured-labeling assistant for a federated \
medical imaging search system spanning three hospitals. Every hospital runs this same \
labeling process against its own studies, so the resulting label vocabulary must be \
identical in wording across institutions.

You will be given the free-text `Diagnosis` field of one radiology study. This field \
contains an OBJECTIVE FINDINGS section (what the images literally show) followed, in most \
but not all reports, by an "Impression" section (the radiologist's diagnostic conclusion).

Your job has two parts:

1. GENERIC CATEGORY: assign exactly one broad category from this fixed list, based on the \
overall subject of the study:
   Neuro, Cardiac, Coronary/Vascular, Pulmonary, GI, Renal/GU, MSK, OB/Fetal, Other

2. FINDING TAGS: extract structured tags from the OBJECTIVE FINDINGS portion only -- ignore \
the Impression/conclusion section when deciding tags. For every notable finding mentioned in \
the objective findings (whether it is present, explicitly ruled out/negated, or stated \
equivocally), emit one tag with:
   - "dimension": one of "location", "finding_type", "size", "other"
   - "value": a short, standardized clinical term for the finding (e.g. "Left MCA Territory", \
"Ischemic Infarct", "3.5 cm")
   - "status": "present" if the objective findings state it is there, "absent" if the \
objective findings explicitly state it is NOT there (e.g. "no intracranial hemorrhage"), or \
"uncertain" if the language is equivocal (e.g. "cannot exclude", "possible")

Critical: capture negative findings as "absent" tags rather than omitting them. A doctor \
searching for "cortical migration abnormality" should NOT match a study where the report \
explicitly says there is no such abnormality -- that only works if you emit the tag with \
status "absent".

Standardization is critical for cross-hospital search: before inventing a new tag value, \
check the "known vocabulary" you are given for the same dimension (and ideally the same \
category) and reuse the exact existing wording if it refers to the same finding. Only mint a \
new value, in standard clinical terminology, when nothing existing fits."""


class LabelingResult(BaseModel):
    generic_category: GenericCategory
    finding_tags: list[FindingTag]


def load_vocabulary() -> dict:
    if VOCAB_PATH.exists():
        return json.loads(VOCAB_PATH.read_text())
    return {}


def save_vocabulary(vocab: dict) -> None:
    VOCAB_PATH.write_text(json.dumps(vocab, indent=2, sort_keys=True))


def update_vocabulary(vocab: dict, category: str, tags: list[FindingTag]) -> None:
    bucket = vocab.setdefault(category, {})
    for tag in tags:
        values = bucket.setdefault(tag.dimension, [])
        if tag.value not in values:
            values.append(tag.value)


def render_vocabulary(vocab: dict) -> str:
    if not vocab:
        return "(empty -- you are labeling the first record; use clear, standardized clinical terminology)"
    return json.dumps(vocab, indent=2, sort_keys=True)


def label_diagnosis(client: anthropic.Anthropic, diagnosis: str, vocab: dict) -> LabelingResult:
    user_content = (
        "Known vocabulary so far (reuse the exact existing wording whenever a finding "
        "matches one of these; only mint a new value if nothing fits):\n"
        f"{render_vocabulary(vocab)}\n\n"
        "Diagnosis text for this study:\n"
        f"{diagnosis}"
    )
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4096,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
        output_format=LabelingResult,
    )
    return response.parsed_output


def main() -> None:
    hospital_node = os.environ.get("HOSPITAL_NODE", "").upper()
    if hospital_node not in NODE_DATA_MAP:
        print(
            f"HOSPITAL_NODE must be one of {', '.join(NODE_DATA_MAP)}; "
            f"got '{os.environ.get('HOSPITAL_NODE', '')}'",
            file=sys.stderr,
        )
        sys.exit(1)

    data_stem = NODE_DATA_MAP[hospital_node]
    source_path = REPO_ROOT / "data" / f"{data_stem}.json"
    output_path = REPO_ROOT / "data" / f"{data_stem}_labeled.json"

    source_records = json.loads(source_path.read_text())

    labeled_by_id = {}
    if output_path.exists():
        for rec in json.loads(output_path.read_text()):
            labeled_by_id[rec["StudyID"]] = rec

    vocab = load_vocabulary()
    client = anthropic.Anthropic()

    total = len(source_records)
    already_done = sum(1 for r in source_records if r["StudyID"] in labeled_by_id)
    print(f"[{hospital_node}] {already_done}/{total} already labeled; resuming.", file=sys.stderr)

    for i, raw in enumerate(source_records):
        study_id = raw["StudyID"]
        if study_id in labeled_by_id:
            continue

        try:
            result = label_diagnosis(client, raw["Diagnosis"], vocab)
        except Exception as exc:  # log and leave unlabeled so a re-run retries it
            print(f"[{hospital_node}] {i + 1}/{total} {study_id}: FAILED ({exc})", file=sys.stderr)
            continue

        labeled_by_id[study_id] = {
            **raw,
            "GenericCategory": result.generic_category.value,
            "FindingTags": [tag.model_dump() for tag in result.finding_tags],
        }
        update_vocabulary(vocab, result.generic_category.value, result.finding_tags)
        save_vocabulary(vocab)

        ordered = [labeled_by_id[r["StudyID"]] for r in source_records if r["StudyID"] in labeled_by_id]
        output_path.write_text(json.dumps(ordered, indent=2))

        print(
            f"[{hospital_node}] {i + 1}/{total} {study_id} -> {result.generic_category.value} "
            f"({len(result.finding_tags)} tags)",
            file=sys.stderr,
        )

    print(f"[{hospital_node}] done. Labeled data at {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
