"""Canonical state-name synonym categories shared by the GitHub and Azure DevOps issue backends.

Both backends' `issue update --state` accept the same human-friendly synonyms for the three
states every issue tracker has some notion of, even though the two backends resolve them
differently:

- GitHub issues only have open/closed (+ a `state_reason` of 'completed' or 'not planned') — no
  per-process-template state names — so `gh_issue.py` maps a synonym straight onto a
  `gh issue close/reopen` call.
- Azure DevOps state names (and which ones are terminal) are defined per work item type per
  process template (e.g. Agile/CMMI use 'Closed' for their Completed-category state, Scrum/Basic
  use 'Done'), so `ado_issue.py` instead uses these same synonyms to find which of the work item
  type's *actual* state names matches the requested category — a request for 'Closed' against a
  Basic-process type must still resolve to its actual state name, 'Done' (and vice versa).
"""

from __future__ import annotations

DONE_STATE_NAMES = ("closed", "done", "completed")
REMOVED_STATE_NAMES = ("removed", "not planned", "not_planned", "wontfix", "won't fix")
OPEN_STATE_NAMES = ("open", "reopened", "reopen")
