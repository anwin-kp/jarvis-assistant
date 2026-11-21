"""Memory quality: single-value facts are updated (not duplicated), nothing is silently lost, sources are kept."""

from jarvis.memory.store import Memory, slot_of


def test_only_truly_single_value_facts_are_slots():
    assert slot_of("[identity] Name is Anwin.") == "name"
    assert slot_of("[identity] Full name is Anwin KP; goes by Anwin.") == "name"
    assert slot_of("[places] Lives in Panaji, Goa.") == "home"
    assert slot_of("[dates] Birthday is 4 May.") == "birthday"
    # rich facts (from the real profile) are never slots: replacing them would wipe correct detail
    for f in ["[places] From Kerala, India; currently working in Goa on the Siemens project.",
              "[work] Works at Gadgeon Smart Systems since Aug 2022.", "[identity] Speaks English and Malayalam."]:
        assert slot_of(f) is None, f


def test_a_corrected_name_replaces_the_wrong_one_and_keeps_history(tmp_path):
    m = Memory(tmp_path / "m.db")
    assert m.learn("[identity] Name is Nin.")  # misheard, as in the real self-check
    assert m.learn("[identity] Name is Anwin.")
    names = [f["fact"] for f in m.list_facts() if slot_of(f["fact"]) == "name"]
    assert names == ["[identity] Name is Anwin."]  # one name, not two
    (h,) = m.fact_history()
    assert h["old"] == "[identity] Name is Nin." and h["new"] == "[identity] Name is Anwin."
    assert h["reason"] == "new name"


def test_forget_and_replace_are_recorded_and_sources_kept(tmp_path):
    m = Memory(tmp_path / "m.db")
    m.learn("[food] Loves biryani.", "taught")
    m.learn("[work] Builds Flutter apps.", "document")
    fid = m.list_facts()[0]["id"]
    m.replace_fact(fid, "[food] Loves Malabar biryani.")
    m.forget(fid)
    assert [h["reason"] for h in m.fact_history()] == ["forgotten", "updated"]
    assert m.db.execute("SELECT source FROM facts").fetchall() == [("document",)]
    assert "fact_history" in m.export()


def test_old_databases_get_the_new_column(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, ts TEXT, fact TEXT UNIQUE)")
    con.execute("INSERT INTO facts(fact) VALUES ('[music] Likes AR Rahman.')")
    con.commit()
    con.close()
    m = Memory(db)
    assert m.list_facts()[0]["fact"] == "[music] Likes AR Rahman." and m.learn("[identity] Name is Anwin.")
