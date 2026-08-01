# Priority rules

- `priority` must be an integer from 0 through 10. Booleans and out-of-range values raise `ValueError`.
- An unverified delivery is always `hold`, before routing by urgency or channel.
- A verified delivery with priority 8 or higher is `expedite`.
