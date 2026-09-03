from pathlib import Path

from workcall_transcriber.paths import RuntimePaths


def test_runtime_paths_create_the_complete_safe_layout(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")

    paths.ensure_exists()

    assert paths.inbox.is_dir()
    assert paths.archive.is_dir()
    assert paths.temp.is_dir()
    assert paths.logs.is_dir()
    assert paths.state.is_dir()
    assert paths.database == paths.state / "workcalls.db"
