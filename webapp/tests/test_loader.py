"""校验器 importlib 加载：确认可用 + 不污染主进程 stdout/stderr（win32 坑）。"""

import sys


def test_hyphenated_loaders_no_std_pollution():
    saved_out, saved_err = sys.stdout, sys.stderr
    from webapp.services import scripts_loader

    vtd = scripts_loader.validate_test_design()
    vq = scripts_loader.validate_questions()
    nq = scripts_loader.normalize_questions()

    # 加载后主进程标准流必须保持原对象（loader 已 detach + 还原）
    assert sys.stdout is saved_out
    assert sys.stderr is saved_err

    assert hasattr(vtd, "Validator") and hasattr(vtd, "Issue")
    assert hasattr(vq, "validate")
    assert hasattr(nq, "normalize_text") and hasattr(nq, "normalize_file")


def test_raw_config_missing_local_file_does_not_import_loader(monkeypatch, tmp_path):
    from webapp import config

    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)

    def fail_loader():
        raise AssertionError("_load_env should not be imported when config.local.yaml is absent")

    monkeypatch.setattr(config, "scripts_loader", fail_loader, raising=False)
    assert config.raw_config() == {}


def test_normal_modules_import():
    from webapp.services import scripts_loader

    assert hasattr(scripts_loader.select_sprint(), "plan")
    assert hasattr(scripts_loader.batch_generate(), "count_stats")
    assert hasattr(scripts_loader.batch_generate(), "run_validator")
    assert hasattr(scripts_loader.jira_fetch(), "get_issue")
    assert hasattr(scripts_loader.load_env(), "parse_config")


def test_validator_runs_on_sample():
    from webapp import config
    from webapp.services import scripts_loader

    p = config.TICKETS_DIR / "wms" / "2026-06-09" / "EAR-240444" / "test-design.json"
    if not p.exists():
        import pytest
        pytest.skip("样本缺失")
    vtd = scripts_loader.validate_test_design()
    issues = vtd.Validator(p).validate()
    assert not [i for i in issues if i.level == "FAIL"], [i.message for i in issues]
