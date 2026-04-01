# nltk Security Upgrade — CVE-2025-14009

**Date:** 2026-04-01
**Affected package:** `nltk`
**Previous version:** 3.9.3
**New version:** 3.9.4

## Summary

Dependabot alert #21 flagged `nltk` for CVE-2025-14009, which describes an unauthenticated remote
shutdown endpoint exposed by `nltk.app.wordnet_app`.

## Impact Assessment

**This project is NOT exploitable via CVE-2025-14009.**

- `nltk` is a **transitive dependency** pulled in by `safety`, not a direct dependency.
- This project never imports or invokes `nltk.app.wordnet_app` (or any other nltk web application
  component). The vulnerable HTTP endpoint is only exposed when `wordnet_app` is explicitly started.
- No upstream fix has been released (fixed_in: null in the GitHub advisory as of this date).

## Action Taken

- Upgraded `nltk` from `3.9.3` to `3.9.4` (latest available on PyPI) as a precautionary measure.
- Updated the pin comment in `requirements.txt` to document the false-positive nature of the alert.

## References

- GitHub Advisory: CVE-2025-14009
- Dependabot Alert: BondIT-ApS/open-xliff-translator#21
