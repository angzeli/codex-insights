from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


def _load_guard() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "editable_install_guard.py"
    spec = importlib.util.spec_from_file_location("editable_install_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()
EditableInstallError = guard.EditableInstallError
active_environment = guard.active_environment
editable_site_packages = guard.editable_site_packages
find_editable_pth = guard.find_editable_pth
has_hidden_flag = guard.has_hidden_flag
repair_hidden_editable_pth = guard.repair_hidden_editable_pth
validate_environment_destination = guard.validate_environment_destination
verify_install = guard.verify_install


def test_environment_validation_requires_isolation_and_rejects_codex_home(
    tmp_path: Path,
) -> None:
    base = tmp_path / "python"
    environment = tmp_path / "venv-acceptance"
    codex_home = tmp_path / "codex-home"

    assert validate_environment_destination(environment, codex_home=codex_home) == environment
    assert active_environment(
        prefix=environment,
        base_prefix=base,
        codex_home=codex_home,
    ) == environment
    with pytest.raises(EditableInstallError, match="No isolated virtual environment"):
        active_environment(prefix=base, base_prefix=base, codex_home=codex_home)
    with pytest.raises(EditableInstallError, match="outside the Codex home"):
        validate_environment_destination(codex_home / "derived", codex_home=codex_home)


def test_only_codex_editable_artifact_inside_active_site_packages_is_selected(
    tmp_path: Path,
) -> None:
    environment = tmp_path / "venv-acceptance"
    packages = environment / "lib" / "python3.14" / "site-packages"
    packages.mkdir(parents=True)
    expected = packages / "__editable__.codex_insights-0.1.0.pth"
    expected.write_text("/synthetic/src\n", encoding="utf-8")
    (packages / "unrelated.pth").write_text("/unrelated\n", encoding="utf-8")
    outside = tmp_path / "outside" / "__editable__.codex_insights-0.1.0.pth"
    outside.parent.mkdir()
    outside.write_text("/outside\n", encoding="utf-8")

    directories = editable_site_packages(environment, (packages, outside.parent))

    assert directories == (packages,)
    assert find_editable_pth(environment, directories) == expected


def test_macos_repair_targets_only_verified_hidden_editable_file(tmp_path: Path) -> None:
    environment = tmp_path / "venv-acceptance"
    packages = environment / "lib" / "python3.14" / "site-packages"
    packages.mkdir(parents=True)
    target = packages / "__editable__.codex_insights-0.1.0.pth"
    target.write_text("/synthetic/src\n", encoding="utf-8")
    calls: list[list[str]] = []
    flag_values = iter((0x8000, 0, 0))

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    repaired = repair_hidden_editable_pth(
        target,
        environment,
        (packages,),
        platform_name="darwin",
        read_flags=lambda _: next(flag_values),
        run=fake_run,
        sleep=lambda _: None,
    )

    assert repaired is True
    assert has_hidden_flag(0x8000, hidden_flag=0x8000)
    assert calls == [["chflags", "nohidden", str(target)]]
    with pytest.raises(EditableInstallError, match="outside the active environment"):
        repair_hidden_editable_pth(
            tmp_path / "__editable__.codex_insights-0.1.0.pth",
            environment,
            (packages,),
            platform_name="darwin",
            read_flags=lambda _: 0x8000,
            run=fake_run,
            sleep=lambda _: None,
        )


def test_macos_reapplied_hidden_flag_reports_non_dot_recreation(tmp_path: Path) -> None:
    environment = tmp_path / ".venv-acceptance"
    packages = environment / "lib" / "python3.14" / "site-packages"
    packages.mkdir(parents=True)
    target = packages / "__editable__.codex_insights-0.1.0.pth"
    target.write_text("/synthetic/src\n", encoding="utf-8")
    flag_values = iter((0x8000, 0, 0x8000))

    with pytest.raises(EditableInstallError, match="non-dot name"):
        repair_hidden_editable_pth(
            target,
            environment,
            (packages,),
            platform_name="darwin",
            read_flags=lambda _: next(flag_values),
            run=lambda command, **_: subprocess.CompletedProcess(command, 0, "", ""),
            sleep=lambda _: None,
        )


def test_failed_import_reports_skipped_hidden_pth(tmp_path: Path) -> None:
    environment = tmp_path / "venv-acceptance"
    packages = environment / "lib" / "python3.14" / "site-packages"
    packages.mkdir(parents=True)
    target = packages / "__editable__.codex_insights-0.1.0.pth"
    target.write_text("/synthetic/src\n", encoding="utf-8")
    python = environment / "bin" / "python"
    command = environment / "bin" / "codex-insights"
    python.parent.mkdir(parents=True)
    python.write_text("synthetic", encoding="utf-8")
    command.write_text("synthetic", encoding="utf-8")

    def fake_run(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            arguments,
            1,
            "",
            f"Skipping hidden .pth file: {target}\nModuleNotFoundError",
        )

    with pytest.raises(EditableInstallError, match="Python skipped the hidden editable .pth"):
        verify_install(environment, target, run=fake_run)
