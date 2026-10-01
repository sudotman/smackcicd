# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from smackcicd import dashboard as dash
from smackcicd.state import State


def mk(path, size=1000):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def world(cfg):
    """Tag v1 built twice into the same folders (as rebuilds do), plus tag v2."""
    st = State(cfg.state_db)
    drop, logs = cfg.drop_root, cfg.log_root
    for tag in ("v1.0.0", "v2.0.0"):
        for plat in ("Android", "Windows"):
            mk(drop / tag / plat / ("%s.zip" % plat), 5000)
            mk(logs / tag / ("%s-uat.log" % plat))
            mk(logs / tag / ("%s-build.log" % plat))
        mk(drop / tag / "manifest.json")
    mk(logs / "smackcicd.log")

    def build(job, tag, plat, number):
        bid = st.start_build(number, job, tag, plat, "Development", "abc")
        st._write("UPDATE builds SET status='success', drop_path=?, log_path=? WHERE id=?",
                  (str(drop / tag / plat), str(logs / tag / ("%s-uat.log" % plat)), bid))
        return bid

    ids = {}
    for job_name, tag, start in (("j1", "v1.0.0", 1), ("j2", "v1.0.0", 3), ("j3", "v2.0.0", 5)):
        job = st._write("INSERT INTO jobs(tag, source, status, enqueued_at)"
                        " VALUES(?, 'manual', 'success', 'x')", (tag,)).lastrowid
        ids[job_name] = [build(job, tag, "Android", start), build(job, tag, "Windows", start + 1)]
    yield cfg, st, ids
    st.close()


def rel(cfg, paths):
    return sorted(Path(p).relative_to(cfg.home).as_posix() for p in paths)


def test_deleting_an_old_attempt_keeps_shared_files(world):
    cfg, st, ids = world
    plan = dash.plan_delete(cfg, st, ids["j1"])
    assert plan["paths"] == [] and plan["sizeBytes"] == 0


def test_deleting_only_the_newest_attempt_keeps_files(world):
    cfg, st, ids = world
    assert dash.plan_delete(cfg, st, [ids["j2"][1]])["paths"] == []


def test_deleting_a_whole_tag_removes_its_folders(world, log):
    cfg, st, ids = world
    plan = dash.plan_delete(cfg, st, ids["j1"] + ids["j2"])
    assert rel(cfg, plan["paths"]) == ["artifacts/v1.0.0", "logs/v1.0.0"]
    assert plan["tagsRemoved"] == ["v1.0.0"]
    dash.delete_builds(cfg, st, log, ids["j1"] + ids["j2"])
    assert not (cfg.drop_root / "v1.0.0").exists()
    assert (cfg.drop_root / "v2.0.0" / "Windows" / "Windows.zip").exists()
    assert (cfg.log_root / "smackcicd.log").exists()
    assert [b["tag"] for b in st.all_builds()] == ["v2.0.0", "v2.0.0"]


def test_deleted_tags_are_not_rebuilt(world, log):
    cfg, st, ids = world
    st.mark_tag_seen("v1.0.0")
    dash.delete_builds(cfg, st, log, ids["j1"] + ids["j2"])
    assert st.has_seen_tag("v1.0.0"), "forgetting the tag would make the poller rebuild it"


def test_unknown_and_running_builds_are_refused(world):
    cfg, st, ids = world
    with pytest.raises(dash.ActionError) as err:
        dash.plan_delete(cfg, st, [999])
    assert err.value.status == 404
    st._write("UPDATE builds SET status='running' WHERE id=?", (ids["j3"][1],))
    with pytest.raises(dash.ActionError) as err:
        dash.plan_delete(cfg, st, [ids["j3"][1]])
    assert err.value.status == 409


def test_paths_outside_the_roots_are_never_touched(world, tmp_path):
    cfg, st, ids = world
    outside = mk(tmp_path / "outside" / "keep.txt").parent
    st._write("UPDATE builds SET drop_path=? WHERE id=?", (str(outside), ids["j3"][0]))
    plan = dash.plan_delete(cfg, st, [ids["j3"][0]])
    assert all(str(outside) not in p for p in plan["paths"])


def test_overview_offers_downloads_only_on_the_newest_build_per_folder(world):
    cfg, st, ids = world
    rows = {b["id"]: b for b in dash.overview(cfg, st)["builds"]}
    assert rows[ids["j1"][0]]["supersededBy"] == 3
    assert rows[ids["j1"][0]]["downloads"] == []


def test_log_tail_and_bad_offsets(world):
    cfg, st, ids = world
    log_file = cfg.log_root / "big.log"
    log_file.write_text("line\n" * 100000)
    tail = dash.read_log(log_file, max(0, log_file.stat().st_size - dash.LOG_CHUNK_LIMIT))
    assert tail["offset"] == tail["size"]
    sent = []
    dash.handle_get(cfg, st, None, "/api/log", {"build": ["live"], "offset": ["undefined"]},
                    lambda *a, **k: sent.append(a[0]))
    assert sent == [404]        # no live build -- and no crash on "undefined"


def test_the_page_ships_with_the_package():
    assert "<title>" in dash.PAGE and "smackcicd" in dash.PAGE
