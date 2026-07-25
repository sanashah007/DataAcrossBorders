"""
Reads every *_data_labeled.json file under data/ and writes a human-readable
summary of the GenericCategory values and FindingTags actually used across
all hospital nodes, to data/tag_categories_summary.txt.

Run after labeling has completed for one or more nodes:
    python scripts/summarize_categories.py
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "data"
OUTPUT_PATH = DATA_DIR / "tag_categories_summary.txt"

LABELED_FILES = {
    "BCH": DATA_DIR / "bch_data_labeled.json",
    "MGH": DATA_DIR / "mgh_data_labeled.json",
    "BWH": DATA_DIR / "bwh_data_labeled.json",
}


def main() -> None:
    category_counts = Counter()
    # tags[category][dimension][value] = {"present": n, "absent": n, "uncertain": n}
    tags = defaultdict(lambda: defaultdict(lambda: defaultdict(Counter)))
    nodes_found = []

    for node, path in LABELED_FILES.items():
        if not path.exists():
            continue
        nodes_found.append(node)
        records = json.loads(path.read_text())
        for rec in records:
            category = rec["GenericCategory"]
            category_counts[category] += 1
            for tag in rec["FindingTags"]:
                tags[category][tag["dimension"]][tag["value"]][tag["status"]] += 1

    if not nodes_found:
        print("No *_data_labeled.json files found under data/ -- run label_studies.py first.", file=sys.stderr)
        sys.exit(1)

    lines = []
    lines.append(f"Tag categories summary -- generated from: {', '.join(nodes_found)}")
    lines.append("")
    lines.append("=== Generic Categories ===")
    for category, count in category_counts.most_common():
        lines.append(f"  {category}: {count}")
    lines.append("")
    lines.append("=== Finding Tags by Category ===")
    for category in sorted(tags):
        lines.append(f"[{category}]")
        for dimension in sorted(tags[category]):
            lines.append(f"  {dimension}:")
            for value, statuses in sorted(tags[category][dimension].items()):
                status_str = ", ".join(f"{s}: {n}" for s, n in sorted(statuses.items()))
                lines.append(f"    {value} ({status_str})")
        lines.append("")

    OUTPUT_PATH.write_text("\n".join(lines))
    print(f"Wrote {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
