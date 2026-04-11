# Contract fixtures

Canonical JSON output captured from real HamSCI clients that implement
`docs/CLIENT-CONTRACT.md`.  Used by `tests/test_contract_adapter.py` as
a frozen reference so `ContractAdapter` keeps parsing real client
output correctly across refactors.

## Files

- `hf-timestd-inventory.json` — `hf-timestd inventory --json` output
  captured on bee3 at hf-timestd commit `96beda9` (v7.0.0, contract
  v0.2). First full v0.2 reference implementation.
- `hf-timestd-validate.json` — `hf-timestd validate --json` output
  from the same host/commit.

## Refreshing

When a client ships a new contract-affecting version, re-capture:

```
ssh bee3 'hf-timestd inventory --json' > tests/fixtures/hf-timestd-inventory.json
ssh bee3 'hf-timestd validate  --json' > tests/fixtures/hf-timestd-validate.json
```

Commit the new fixtures alongside any adapter changes needed to parse
them.  If a field is removed that the adapter previously consumed, the
adapter must keep tolerating its absence for one contract release
(see CLIENT-CONTRACT.md "Migration and versioning").
