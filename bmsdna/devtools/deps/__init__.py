"""`bdt deps`: which locked dependencies a security advisory or supply chain incident touches (bmsuisse/devtools#113).

- `lockfiles`   -- read `uv.lock` (PyPI) and `bun.lock` (npm) into one shape
- `versions`    -- version parsing, comparison and range matching per ecosystem
- `osv`         -- OSV / CISA KEV / registry timestamp client (the only network code)
- `advisories`  -- turns OSV records into a per-package status; pure logic, no I/O
"""
