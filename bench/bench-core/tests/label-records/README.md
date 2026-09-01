# Label-face sample records

One realistic `bench-run-record/1` per QA-label face (TEC-352). Used by
`tests/test_qa_label.py`, and kept in the repo so a face can be reviewed
visually after it is changed:

```bash
.venv/bin/python -m bench_core.qa_label \
    bench-core/tests/label-records/magos-apu.json
```

A wildcard dumps every face at once, and `-o` writes the file instead of
relying on a shell redirect (which on the bench station's PowerShell would
produce UTF-16 the printer cannot read):

```bash
.venv/bin/python -m bench_core.qa_label -o all.zpl \
    bench-core/tests/label-records/'*.json'
```

Paste the ZPL into <https://labelary.com/viewer.html> (8 dpmm, 5.9 x 1.97 in)
to see exactly what the ZD421 prints. `docs/qa-labels.md` has the full
workflow.

Every record here is `status: "ok"` with `verified: true`, because that is the
only combination that prints. Records for the gate itself (verified false,
verified null, status error) are built inline in `tests/test_label_printer.py`
— they have no face, so there is nothing to review.
