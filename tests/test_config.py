import pytest

from cleantube.config import load_config


def test_defaults_when_file_missing(tmp_path):
    config = load_config(tmp_path / "nope.toml")
    assert config.backfill_count == 3
    assert config.poll_interval_seconds == 3600
    assert config.max_download_attempts == 3


def test_overrides(tmp_path):
    path = tmp_path / "cleantube.toml"
    path.write_text('backfill_count = 5\npoll_interval_seconds = 60\n')
    config = load_config(path)
    assert config.backfill_count == 5
    assert config.poll_interval_seconds == 60


def test_unknown_key_warns_but_loads(tmp_path, caplog):
    path = tmp_path / "cleantube.toml"
    path.write_text('backfil_count = 5\n')  # typo
    with caplog.at_level("WARNING", logger="cleantube"):
        config = load_config(path)
    assert config.backfill_count == 3  # typo'd key ignored, default kept
    assert any("config_unknown_key" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "toml_line",
    [
        "backfill_count = -1",
        "poll_interval_seconds = 0",
        "post_download_cooldown_seconds = -5",
        "max_download_attempts = 0",
    ],
)
def test_invalid_values_rejected(tmp_path, toml_line):
    path = tmp_path / "cleantube.toml"
    path.write_text(toml_line + "\n")
    with pytest.raises(ValueError):
        load_config(path)
