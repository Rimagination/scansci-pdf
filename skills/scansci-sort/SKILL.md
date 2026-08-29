---
name: scansci-sort
description: >
  Use this skill when the user wants to triage a large academic paper list into
  open-access, repository, institutional, and user-selected source queues before
  downloading.
---

# ScanSci Sort

Use this skill for large-list hygiene and route planning. The detailed workflow is
kept in `../../zcode-plugin/skills/scansci-sort/SKILL.md` and remains the canonical
reference for the ZCode command as well.

- Normalize, deduplicate, and validate identifiers before probing sources.
- Keep triage separate from retrieval: classification should not download the
  entire input list.
- Prefer open-access and repository checks before any user-selected secondary
  source probe.
- Preserve `hit`, `miss`, `turnstile`, and `blocked` distinctions instead of
  treating every HTTP 200 response as a full-text hit.
- Produce a report, bucket files, and a clear next route for each bucket.
