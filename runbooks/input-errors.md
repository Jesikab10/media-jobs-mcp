# Input errors

## Timeline references a missing source file

The edit timeline (XML) points to a clip that was never uploaded or was renamed.
Action: this is not retryable. Ask the uploader to provide the missing file or a corrected timeline. Retrying will fail the same way.

## Timecode outside the clip duration

A segment's in/out point is beyond the length of the source clip, usually a timeline exported against a different version of the media.
Action: not retryable. Request a timeline exported from the final media.

## Unknown shot code in the timeline

The timeline uses a shot code that is not registered for the job.
Action: not retryable. Register the shot code or fix the timeline, then resubmit.
