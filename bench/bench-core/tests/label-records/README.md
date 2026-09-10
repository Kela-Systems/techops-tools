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

Paste the ZPL into <https://labelary.com/viewer.html> (8 dpmm, 2.28 x 1.14 in)
to see exactly what the ZD421 prints. It appears a quarter turn round, because
the design's 15 cm side runs along the feed — the printhead is only 104 mm wide,
so it cannot be otherwise. `docs/qa-labels.md` shows how to get the render in
reading orientation, and explains why.

`planet.json` dumps TWO formats, not one: the PoE switch earns a port map
beside its QA label (`qa_label.extra_contents`), and the review loop shows both.

Every record here is `status: "ok"` with `verified: true`, because that is the
only combination that prints. Records for the gate itself (verified false,
verified null, status error) are built inline in `tests/test_label_printer.py`
— they have no face, so there is nothing to review.
