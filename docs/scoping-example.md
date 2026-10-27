# Scoping example: from a vague request to tasks

An anonymised example from a production video pipeline I built and run. It shows how I turn an
ambiguous request into work that can be estimated, built and verified.

## The request

> "Jobs sometimes get created twice and the editors see duplicates. Can you fix it?"

## Questions before any code

| Question | Why it matters | Answer found |
|---|---|---|
| Where do jobs enter the system? | The fix belongs at the entry point, not downstream | A webhook from the project-management tool, through an HTTP bridge, triggers the pipeline |
| Is the duplicate a second *request* or a second *row*? | Different causes, different fixes | Second request: the webhook source retries when it does not get a fast response |
| What identifies "the same job" for the business? | That is the idempotency key | The id of the item in the upstream tool, not anything we generate |
| What should a duplicate request return? | Callers must not see an error for a safe replay | The existing job code |
| Can a retry arrive while the first one is still running? | Decides between "check then insert" and a database constraint | Yes, within seconds |

## Tasks

1. Unique constraint on the upstream item id; intake becomes `INSERT … ON CONFLICT DO NOTHING`
   followed by a read-back, so concurrent requests cannot both create a row.
2. The intake response says whether the job was created or replayed.
3. Every later write keyed by a natural key (job, file, shot) so a re-run updates instead of
   duplicating; counters recalculated, not incremented.
4. Test: two concurrent intake calls with the same id produce one job.
5. One-off query to find and merge the duplicates that already existed.

## Done when

- The concurrency test passes.
- A week of production intake shows zero new duplicates (query against the jobs table).
- Editors confirm the duplicates are gone.

The same pattern is what `submit_job` implements in this repository.
