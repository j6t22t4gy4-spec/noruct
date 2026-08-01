# Retry policy

- `attempt` is a non-negative integer and must not be a boolean.
- `base` and `cap` are positive integers and must not be booleans.
- `cap` must be greater than or equal to `base`.
- The delay is `min(cap, base * 2 ** attempt)`.
- Invalid inputs raise `ValueError`.
