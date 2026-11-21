from jarvis.docs.index import is_prose, skip_dir


def test_app_and_game_folders_are_skipped():
    for d in ["Rockstar Games", "Service Worker", "CacheStorage", "ZxcvbnData", ".git", "Logs"]:
        assert skip_dir(d), d
    for d in ["Resumes", "Work", "Invoices 2026", "Temperature reports", "Templates"]:
        assert not skip_dir(d), d


def test_word_lists_are_not_documents():
    assert not is_prose("password\n123456\nqwerty\nmonkey\n")
    assert is_prose("Notes from the Siemens meeting.\nShip the inspection module next sprint.\n")
