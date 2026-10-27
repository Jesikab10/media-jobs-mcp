# Transient errors

## HTTP 429 from the encoding service

The encoder runs with concurrency 1 per instance. A 429 means every instance is busy, not that the job is broken.
Action: the job is safe to retry once capacity frees up. Do not retry in a tight loop; respect Retry-After.
Escalate only if the same job hits 429 for more than one hour.

## HTTP 502 or 503 from a processing service

Usually an instance being recycled or a cold start. The request may not have reached the worker.
Action: retry the job. If three consecutive retries fail with 503, check the service health dashboard before retrying again.

## Timeout while waiting for an encode

A timeout does not mean the encode failed: the worker may still be running and will write its output.
Action: do not retry immediately. Check whether the output exists first; retrying blindly can duplicate heavy work.
