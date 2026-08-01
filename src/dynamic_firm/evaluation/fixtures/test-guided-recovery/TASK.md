# Test-guided recovery

`within_window` uses inclusive bounds. Run validation after each change and use
one bounded recovery edit if the first candidate does not satisfy every edge.
Preserve the public tests and change only `window.py`.
