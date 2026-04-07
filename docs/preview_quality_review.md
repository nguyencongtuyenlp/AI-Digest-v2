# AI-Digest-v2 Preview Quality Review

This doc is a lightweight manual check for preview runs. It is meant to be used in 3-5 minutes by PM, operator, or engineer after a preview batch finishes.

## Metrics Set

| Area | What to check | Score |
| --- | --- | --- |
| Main brief quality | Does the final main brief feel worth reading? Are the selected items strong enough? Is there filler? | 1-5 |
| Source quality | Did the main brief include at least one official or strong-source item when available? Is the batch overly dependent on proxy or regional recap sources? | 1-5 |
| Selection sharpness | Are weak Society & Culture items excluded? Are GitHub/tooling items only present when truly high-impact? Is the brief compact and selective? | 1-5 |
| Writing quality | Is the wording factual and readable? Any marketing tone or opinion leakage? Any repetition or awkward stitching? | 1-5 |

## Scoring Rubric

Use a simple 1-5 score for each metric.

| Score | Meaning |
| --- | --- |
| 5 | Strong, clean, clearly above baseline |
| 4 | Good, minor issues only |
| 3 | Acceptable, but needs attention |
| 2 | Weak, clearly needs fixes |
| 1 | Poor, should not ship without change |

Suggested interpretation:

- `4-5` = healthy preview
- `3` = borderline, review before publishing
- `1-2` = red zone, fix before trusting the batch

## Reusable Review Template

Copy this block into a note after each preview run:

```text
Preview date:
Run mode:
Reviewer:

1. Main brief quality: _ / 5
   Notes:

2. Source quality: _ / 5
   Notes:

3. Selection sharpness: _ / 5
   Notes:

4. Writing quality: _ / 5
   Notes:

Overall judgment:
- Healthy / Borderline / Red zone

Action needed:
- None
- Minor tuning
- Selection fix
- Writing fix
- Re-run preview
```

## How To Use

1. Open the preview output and read only the main brief plus a few supporting items.
2. Score each metric quickly without debating edge cases for too long.
3. If one area looks weak, write one sentence explaining the issue and the likely cause.
4. Use the overall judgment to decide whether the next step is publish, tune, or rerun.

## Red Flag Patterns

- The main brief feels padded, generic, or not worth reading.
- No official or strong-source item appears even though one was available.
- The batch leans too hard on proxy, recap, or regional filler sources.
- Weak Society & Culture items survive into the main brief.
- GitHub or tooling items show up without real ecosystem or adoption impact.
- The writing sounds promotional, opinionated, repetitive, or stitched together.

## Why This Doc Lives Here

Put this in `docs/` so the review language sits next to the architecture and process docs, but stays separate from code and run output. That keeps it easy to find for PM, leadership, and operators without making it look like a code-level spec.
