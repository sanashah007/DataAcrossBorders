"""
Offline stand-in for label_studies.py: produces the SAME output schema
(GenericCategory + FindingTags, written to LLM_output/) but using plain
keyword/regex matching instead of the Claude API -- no API key, no network
calls, no cost.

This is deliberately NOT a general clinical NLP system. The keyword list
below was hand-tuned against the actual Diagnosis text in this repo's
data/*.json files, so it works reasonably well on THIS synthetic dataset and
will not generalize to arbitrary radiology reports. Swap in label_studies.py
(which uses Claude and does generalize) once you have an ANTHROPIC_API_KEY --
it writes to the same LLM_output/{node}_data_labeled.json files.

Run for all three nodes at once (no HOSPITAL_NODE env var needed, since this
is static rule matching rather than a growing, order-dependent vocabulary):
    python scripts/label_studies_offline.py
"""

import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from models import LabeledStudyRecord  # noqa: E402

NODE_DATA_MAP = {
    "BCH": "bch_data",
    "MGH": "mgh_data",
    "BWH": "bwh_data",
}

# This dataset only ever uses these three BodyPartExamined values, and each
# maps 1:1 onto one of the fixed generic categories -- no inference needed.
BODY_PART_TO_CATEGORY = {
    "BRAIN": "Neuro",
    "HEART": "Cardiac",
    "FETAL": "OB/Fetal",
}

# (keyword substring, dimension, standardized value). Matched case-insensitively.
# Order matters only in that longer/more-specific phrases are listed before
# shorter ones that might otherwise shadow them.
KEYWORDS = [
    # --- location ---
    ("left ventricle", "location", "Left Ventricle"),
    ("right ventricle", "location", "Right Ventricle"),
    ("biventricular", "location", "Biventricular"),
    ("interventricular septum", "location", "Interventricular Septum"),
    ("frontal lobe", "location", "Frontal Lobe"),
    ("temporal lobe", "location", "Temporal Lobe"),
    ("parietal lobe", "location", "Parietal Lobe"),
    ("parieto-occipital", "location", "Parieto-Occipital"),
    ("occipital lobe", "location", "Occipital Lobe"),
    ("occipital bone", "location", "Occipital Bone"),
    ("occipital skull", "location", "Occipital Bone"),
    ("basal ganglia", "location", "Basal Ganglia"),
    ("internal capsule", "location", "Internal Capsule"),
    ("cerebellum", "location", "Cerebellum"),
    ("cerebellar", "location", "Cerebellum"),
    ("brainstem", "location", "Brainstem"),
    ("cerebral hemisphere", "location", "Cerebral Hemisphere"),
    ("lateral ventricle", "location", "Lateral Ventricle"),
    ("thoracolumbar", "location", "Thoracolumbar Spine"),
    ("kidneys", "location", "Kidneys"),
    ("bilateral", "location", "Bilateral"),
    ("left-sided", "location", "Left"),
    ("left ", "location", "Left"),
    ("right-sided", "location", "Right"),
    ("right ", "location", "Right"),
    ("midline", "location", "Midline"),
    # --- finding_type ---
    ("subdural hematoma", "finding_type", "Subdural Hematoma"),
    ("intracranial hemorrhage", "finding_type", "Intracranial Hemorrhage"),
    ("hemorrhage", "finding_type", "Hemorrhage"),
    ("mass effect", "finding_type", "Mass Effect"),
    ("midline shift", "finding_type", "Midline Shift"),
    ("ventriculomegaly", "finding_type", "Ventriculomegaly"),
    ("hydrocephalus", "finding_type", "Hydrocephalus"),
    ("ischemic infarct", "finding_type", "Ischemic Infarct"),
    ("infarct", "finding_type", "Infarct"),
    ("ischemia", "finding_type", "Ischemia"),
    ("ischemic", "finding_type", "Ischemic"),
    ("edema", "finding_type", "Edema"),
    ("encephalocele", "finding_type", "Encephalocele"),
    ("schwannoma", "finding_type", "Schwannoma"),
    ("acoustic neuroma", "finding_type", "Acoustic Neuroma"),
    ("cortical dysplasia", "finding_type", "Cortical Dysplasia"),
    ("cortical migration abnormality", "finding_type", "Cortical Migration Abnormality"),
    ("tuberous sclerosis", "finding_type", "Tuberous Sclerosis"),
    ("cortical tubers", "finding_type", "Cortical Tubers"),
    ("subependymal nodules", "finding_type", "Subependymal Nodules"),
    ("demyelinating plaque", "finding_type", "Demyelinating Plaque"),
    ("demyelination", "finding_type", "Demyelination"),
    ("vascular compression", "finding_type", "Vascular Compression"),
    ("atrophy", "finding_type", "Atrophy"),
    ("chiari", "finding_type", "Chiari Malformation"),
    ("myelomeningocele", "finding_type", "Myelomeningocele"),
    ("dandy-walker", "finding_type", "Dandy-Walker Variant"),
    ("skeletal dysplasia", "finding_type", "Skeletal Dysplasia"),
    ("micromelia", "finding_type", "Micromelia"),
    ("diaphragmatic hernia", "finding_type", "Diaphragmatic Hernia"),
    ("renal agenesis", "finding_type", "Renal Agenesis"),
    ("oligohydramnios", "finding_type", "Oligohydramnios"),
    ("hypokinesis", "finding_type", "Hypokinesis"),
    ("hypokinesia", "finding_type", "Hypokinesis"),
    ("dyskinesia", "finding_type", "Dyskinesia"),
    ("dyskinesis", "finding_type", "Dyskinesia"),
    ("akinesia", "finding_type", "Akinesia"),
    ("regurgitation", "finding_type", "Valvular Regurgitation"),
    ("stenosis", "finding_type", "Stenosis"),
    ("dilated cardiomyopathy", "finding_type", "Dilated Cardiomyopathy"),
    ("cardiomyopathy", "finding_type", "Cardiomyopathy"),
    ("myocarditis", "finding_type", "Myocarditis"),
    ("late gadolinium enhancement", "finding_type", "Late Gadolinium Enhancement"),
    ("fibrosis", "finding_type", "Fibrosis"),
    ("thrombus", "finding_type", "Thrombus"),
    ("thrombi", "finding_type", "Thrombus"),
    ("aneurysm", "finding_type", "Aneurysm"),
    ("dilation", "finding_type", "Dilation"),
    ("dilatation", "finding_type", "Dilation"),
    ("dilated", "finding_type", "Dilation"),
]

MEASUREMENT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:x\s*\d+(?:\.\d+)?){0,2}\s*(?:cm|mm)\b", re.IGNORECASE
)

SEVERITY_WORDS = ["severe", "moderate", "mild", "marked", "significant", "large", "small", "focal", "diffuse"]

NEGATION_CUES = [
    "no ", "no evidence of", "no signs of", "no significant", "no focal",
    "without", "absence of", "absent", "negative for", "free of",
    "not seen", "not observed", "not present", "not appreciated", "ruled out",
]

UNCERTAINTY_CUES = [
    "suggestive of", "suspicious for", "possible", "cannot exclude",
    "concerning for", "differential includes", "may represent",
    "likely represents", "equivocal", "atypical for", "highly suspicious for",
]

WINDOW_CHARS = 45


def status_for_match(text: str, start: int) -> str:
    window = text[max(0, start - WINDOW_CHARS):start].lower()
    if any(cue in window for cue in NEGATION_CUES):
        return "absent"
    if any(cue in window for cue in UNCERTAINTY_CUES):
        return "uncertain"
    return "present"


def objective_findings_section(diagnosis: str) -> str:
    match = re.search(r"impression\s*:", diagnosis, re.IGNORECASE)
    return diagnosis[: match.start()] if match else diagnosis


def extract_tags(diagnosis: str) -> list[dict]:
    objective = objective_findings_section(diagnosis)
    lower = objective.lower()
    seen = set()
    tags = []

    for keyword, dimension, value in KEYWORDS:
        idx = lower.find(keyword.lower())
        if idx == -1:
            continue
        key = (dimension, value)
        if key in seen:
            continue
        seen.add(key)
        tags.append({"dimension": dimension, "value": value, "status": status_for_match(objective, idx)})

    for match in MEASUREMENT_RE.finditer(objective):
        value = match.group(0).strip()
        key = ("size", value)
        if key in seen:
            continue
        seen.add(key)
        tags.append({"dimension": "size", "value": value, "status": status_for_match(objective, match.start())})

    for word in SEVERITY_WORDS:
        idx = lower.find(word)
        if idx == -1:
            continue
        value = word.capitalize()
        key = ("size", value)
        if key in seen:
            continue
        seen.add(key)
        tags.append({"dimension": "size", "value": value, "status": status_for_match(objective, idx)})

    return tags


def label_node(hospital_node: str) -> None:
    data_stem = NODE_DATA_MAP[hospital_node]
    source_path = REPO_ROOT / "data" / f"{data_stem}.json"
    output_dir = REPO_ROOT / "LLM_output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / f"{data_stem}_labeled.json"

    source_records = json.loads(source_path.read_text())
    labeled = []
    for raw in source_records:
        category = BODY_PART_TO_CATEGORY[raw["BodyPartExamined"]]
        record = {
            **raw,
            "GenericCategory": category,
            "FindingTags": extract_tags(raw["Diagnosis"]),
        }
        LabeledStudyRecord.model_validate(record)  # fail fast if schema drifts
        labeled.append(record)

    output_path.write_text(json.dumps(labeled, indent=2))
    print(f"[{hospital_node}] labeled {len(labeled)} records -> {output_path}", file=sys.stderr)


def main() -> None:
    for hospital_node in NODE_DATA_MAP:
        label_node(hospital_node)


if __name__ == "__main__":
    main()
