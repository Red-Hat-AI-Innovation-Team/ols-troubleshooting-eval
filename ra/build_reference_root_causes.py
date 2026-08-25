"""Extract reference root causes from existing runs for each test seed.
Writes to processed/test/reference_root_causes.json
"""
import json, os, csv

base = "/home/lab/rawhad/ols-troubleshooting-eval/ra/sdg/v1"

with open(f"{base}/processed/test/metadata.csv") as f:
    rows = list(csv.DictReader(f))

references: dict[str, list[str]] = {}

for row in rows:
    sc = row["original_scenario_idx"]
    sd = row["original_seed_idx"]
    fn = row["filename"]
    runs_dir = f"{base}/{sc}/runs"
    
    answers = []
    for rf in sorted(os.listdir(runs_dir)):
        if not rf.startswith(f"seed_{sd}_run_"):
            continue
        with open(f"{runs_dir}/{rf}") as f:
            data = json.load(f)
        conv = data["conversation"]
        
        # Get the last assistant message with content
        for msg in reversed(conv):
            if msg["role"] == "assistant" and msg.get("content"):
                answers.append(msg["content"])
                break
    
    references[fn] = answers
    print(f"{fn}: {len(answers)} reference answers extracted")

out_path = f"{base}/processed/test/reference_root_causes.json"
with open(out_path, "w") as f:
    json.dump(references, f, indent=2)
print(f"\nWrote {out_path}")
