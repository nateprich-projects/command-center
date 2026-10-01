"""Scratch #2063: print the session basetemp at session end."""
def pytest_sessionfinish(session, exitstatus):
    f = getattr(session.config, "_tmp_path_factory", None)
    try:
        print("\nBASETEMP", f.getbasetemp() if f else None)
    except Exception as exc:  # noqa: BLE001
        print("\nBASETEMP unavailable", exc)
