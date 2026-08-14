# Repository Guidance

## Testing

Prefer positive contract tests that verify required behavior. Do not add tests
solely to assert that retired vocabulary, legacy literals, or removed feature
values are absent unless their presence would cause a concrete runtime failure.
