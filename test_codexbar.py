"""Regression tests for the CodexBar widget modules."""

import io
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import ssl
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.request
from unittest import mock

from codexbar import (
    app,
    config,
    credentials,
    diagnostics,
    dpi,
    quota_api,
    runtime,
    taskbar,
    taskbar_accessibility,
    token_usage,
    ui,
    web_dashboard,
)


class JsonResponse(io.StringIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class JsonOpener:
    def __init__(self, payload):
        self.payload = payload

    def open(self, request, timeout=15):
        return JsonResponse(json.dumps(self.payload))


class ErrorOpener:
    def __init__(self, status):
        self.status = status

    def open(self, request, timeout=15):
        raise urllib.error.HTTPError(request.full_url, self.status, "error", None, None)


class ModuleBoundaryTests(unittest.TestCase):
    def test_modules_import_without_cycles(self):
        self.assertTrue(callable(config.load_config))
        self.assertTrue(callable(credentials.resolve_credentials))
        self.assertTrue(callable(diagnostics.log_exception))
        self.assertTrue(callable(quota_api.fetch_quota))
        self.assertTrue(callable(runtime.resource_path))
        self.assertTrue(callable(taskbar.position_taskbar_popup))
        self.assertTrue(callable(token_usage.collect_today_usage))
        self.assertTrue(callable(token_usage.collect_usage_range))
        self.assertTrue(callable(web_dashboard.launch))
        self.assertTrue(hasattr(ui, "QuotaWidget"))
        self.assertTrue(callable(app.main))


class RuntimePackagingTests(unittest.TestCase):
    def test_resource_path_uses_source_root_by_default(self):
        path = runtime.resource_path("codexbar", "web_assets", "dashboard.html")

        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "dashboard.html")

    def test_resource_path_uses_meipass_when_frozen(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(runtime.sys, "frozen", True, create=True):
                with mock.patch.object(runtime.sys, "_MEIPASS", temp, create=True):
                    path = runtime.resource_path("codexbar", "web_assets")

        self.assertEqual(str(path), os.path.join(temp, "codexbar", "web_assets"))

    def test_dashboard_command_uses_dashboard_pyw_in_source(self):
        with mock.patch.object(runtime.sys, "frozen", False, create=True):
            command, cwd = runtime.dashboard_command()

        self.assertEqual(command[0], runtime.sys.executable)
        self.assertTrue(command[1].endswith("codexbar_dashboard.pyw"))
        self.assertEqual(cwd, os.path.dirname(os.path.abspath(__file__)))

    def test_dashboard_command_uses_same_exe_when_frozen(self):
        executable = r"C:\Apps\CodexBar\CodexBar.exe"
        with mock.patch.object(runtime.sys, "frozen", True, create=True):
            with mock.patch.object(runtime.sys, "executable", executable):
                command, cwd = runtime.dashboard_command()

        self.assertEqual(command, [executable, "--dashboard"])
        self.assertEqual(cwd, r"C:\Apps\CodexBar")

    def test_main_dashboard_argument_skips_taskbar_singleton(self):
        with mock.patch.object(app.web_dashboard, "main") as dashboard_main:
            with mock.patch.object(app, "acquire_single_instance") as acquire:
                app.main(["--dashboard"])

        dashboard_main.assert_called_once_with()
        acquire.assert_not_called()


class OpenSourceReleaseContractTests(unittest.TestCase):
    def test_legal_files_metadata_and_private_artifacts_are_declared(self):
        root = Path(__file__).resolve().parent
        required_files = (
            "LICENSE",
            "docs/PRIVACY.md",
            "docs/ARCHITECTURE.md",
            ".github/SECURITY.md",
            ".github/CONTRIBUTING.md",
            ".github/CODE_OF_CONDUCT.md",
            "THIRD_PARTY_NOTICES.md",
        )
        for relative_path in required_files:
            self.assertTrue((root / relative_path).is_file(), relative_path)

        with (root / "pyproject.toml").open("rb") as file:
            project = tomllib.load(file)["project"]
        self.assertRegex(project["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(project["license"], "MIT")
        self.assertEqual(project["readme"], "README.md")
        self.assertIn("CodexBar", project["description"])
        self.assertEqual(project["authors"][0]["name"], "zhuxianghcl-ctrl")
        self.assertIn("Homepage", project["urls"])
        self.assertIn("Issues", project["urls"])
        self.assertIn("LICENSE", project["license-files"])

        from codexbar import __version__

        self.assertEqual(__version__, project["version"])

        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        for pattern in (
            ".env",
            "*.log",
            "*.zip",
            ".coverage",
            ".pytest_cache/",
            ".ruff_cache/",
            ".claude/",
            ".codegraph/",
            "credentials.dat",
            "model_prices.json",
            ".codexbar_cfg.json",
            "*.key",
            "*.p12",
            "*.pfx",
            ".secrets/",
        ):
            self.assertIn(pattern, gitignore)

        gitattributes = (root / ".gitattributes").read_text(encoding="utf-8")
        for rule in (
            "*.py text eol=lf",
            "*.md text eol=lf",
            "*.yml text eol=lf",
            "*.bat text eol=crlf",
            "*.ico binary",
            "*.png binary",
        ):
            self.assertIn(rule, gitattributes)


class ReleaseBuildContractTests(unittest.TestCase):
    def _load_release_audit(self):
        path = Path(__file__).resolve().parent / "scripts" / "audit_release.py"
        spec = importlib.util.spec_from_file_location("codexbar_release_audit", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _load_license_collector(self):
        path = Path(__file__).resolve().parent / "scripts" / "collect_licenses.py"
        spec = importlib.util.spec_from_file_location(
            "codexbar_license_collector", path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _make_minimal_release(root: Path) -> None:
        (root / "CodexBar.exe").write_bytes(b"MZ clean-test-executable")
        for name in (
            "LICENSE",
            "PRIVACY.md",
            "SECURITY.md",
            "THIRD_PARTY_NOTICES.md",
        ):
            (root / name).write_text(f"clean {name}\n", encoding="utf-8")
        licenses = root / "licenses"
        licenses.mkdir()
        (licenses / "dependency-LICENSE.txt").write_text(
            "test dependency license\n", encoding="utf-8"
        )

    def test_version_resource_and_build_pipeline_are_declared(self):
        root = Path(__file__).resolve().parent
        version_path = root / "codexbar" / "version_info.txt"
        self.assertTrue(version_path.is_file())
        version_text = version_path.read_text(encoding="utf-8")
        from codexbar import __version__
        version_tuple = tuple(map(int, __version__.split("."))) + (0,)
        self.assertIn(f"filevers={version_tuple}", version_text)
        self.assertIn(f"prodvers={version_tuple}", version_text)
        self.assertIn(f"StringStruct('FileVersion', '{__version__}')", version_text)
        self.assertIn(f"StringStruct('ProductVersion', '{__version__}')", version_text)
        self.assertIn("ProductName", version_text)
        self.assertIn("CodexBar", version_text)
        self.assertIn("LegalCopyright", version_text)
        self.assertIn("zhuxianghcl-ctrl", version_text)

        spec_text = (root / "CodexBar.spec").read_text(encoding="utf-8")
        self.assertIn("version_info.txt", spec_text)

        build_text = (root / "scripts" / "build_exe.ps1").read_text(
            encoding="utf-8"
        )
        for required in (
            "THIRD_PARTY_NOTICES.md",
            "collect_licenses.py",
            "audit_release.py",
            "CodexBar-Windows-x64.zip",
            "Get-FileHash",
            "GetFullPath",
            "Refusing to remove path outside the repository",
            ".sha256",
        ):
            self.assertIn(required, build_text)

    def test_release_auditor_accepts_minimal_clean_distribution(self):
        audit = self._load_release_audit()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._make_minimal_release(root)

            findings = audit.audit_release(root)

        self.assertEqual(findings, [])

    def test_release_auditor_rejects_runtime_secrets_and_private_paths(self):
        audit = self._load_release_audit()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._make_minimal_release(root)
            (root / "credentials.dat").write_bytes(b"encrypted-but-private")
            (root / "leak.txt").write_text(
                "sk-this-is-a-fake-but-secret-value "
                "C:\\Users\\PrivateName\\project "
                "C:\\Projects\\PrivateWorktree",
                encoding="utf-8",
            )

            findings = audit.audit_release(root)

        kinds = {finding.kind for finding in findings}
        self.assertIn("forbidden-file", kinds)
        self.assertIn("credential-pattern", kinds)
        self.assertIn("private-path", kinds)
        self.assertNotIn("sk-this-is-a-fake-but-secret-value", repr(findings))

    def test_upstream_python_paths_require_identical_external_runtime_reference(self):
        audit = self._load_release_audit()
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            release, runtime = base / "release", base / "python"
            release.mkdir()
            runtime.mkdir()
            self._make_minimal_release(release)
            internal = release / "_internal"
            internal.mkdir()
            content = b"MZ upstream C:\\Users\\UpstreamBuilder\\python\\source"
            (runtime / "python313.dll").write_bytes(content)
            (internal / "python313.dll").write_bytes(content)
            self.assertEqual([f.kind for f in audit.audit_release(release)], ["private-path"])
            self.assertEqual(audit.audit_release(release, runtime_root=runtime), [])
            (internal / "python313.dll").write_bytes(content + b" altered")
            self.assertEqual([f.kind for f in audit.audit_release(release, runtime_root=runtime)], ["private-path"])

    def test_runtime_reference_does_not_exempt_text_or_nested_binaries(self):
        audit = self._load_release_audit()
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            release, runtime = base / "release", base / "python"
            release.mkdir()
            (runtime / "DLLs").mkdir(parents=True)
            self._make_minimal_release(release)
            nested = release / "_internal" / "extra"
            nested.mkdir(parents=True)
            content = b"C:\\Users\\LocalUser\\private"
            for relative in ("config.txt", "extra/python313.dll"):
                target = release / "_internal" / relative
                target.write_bytes(content)
                (runtime / "DLLs" / target.name).write_bytes(content)
            self.assertEqual(len(audit.audit_release(release, runtime_root=runtime)), 2)

    def test_unchanged_runtime_binary_still_rejects_credentials(self):
        audit = self._load_release_audit()
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            release, runtime = base / "release", base / "python"
            release.mkdir()
            (runtime / "DLLs").mkdir(parents=True)
            self._make_minimal_release(release)
            (release / "_internal").mkdir()
            content = b"MZ C:\\Users\\Builder\\code sk-this-is-a-fake-but-secret-value"
            (release / "_internal" / "_ssl.pyd").write_bytes(content)
            (runtime / "DLLs" / "_ssl.pyd").write_bytes(content)
            self.assertEqual([f.kind for f in audit.audit_release(release, runtime_root=runtime)], ["credential-pattern"])

    def test_release_cannot_whitelist_itself_as_the_runtime(self):
        audit = self._load_release_audit()
        with tempfile.TemporaryDirectory() as temp:
            release = Path(temp)
            self._make_minimal_release(release)
            internal = release / "_internal"
            internal.mkdir()
            (internal / "python313.dll").write_bytes(b"MZ C:\\Users\\LocalUser\\private")
            self.assertEqual([f.kind for f in audit.audit_release(release, runtime_root=internal)], ["private-path"])

    def test_source_launcher_syncs_frozen_environment_before_python_check(self):
        root = Path(__file__).resolve().parent
        launcher = (root / "启动 CodexBar.bat").read_text(
            encoding="utf-8-sig"
        ).lower()
        sync_index = launcher.index("uv sync --quiet --frozen")
        python_check_index = launcher.index('if not exist "%pyw%"')
        self.assertLess(sync_index, python_check_index)

    def test_license_collector_ignores_non_license_files_in_license_directories(self):
        collector = self._load_license_collector()

        class FakeDistribution:
            files = (
                Path("package.dist-info/licenses/LICENSE"),
                Path("package.dist-info/licenses/COPYING.third-party"),
                Path("package.dist-info/licenses/_spdx.py"),
                Path("package.dist-info/AUTHORS"),
            )

            @staticmethod
            def locate_file(relative):
                return relative

        with mock.patch.object(Path, "is_file", return_value=True):
            results = collector._license_files(FakeDistribution())

        self.assertEqual(
            [path.name for path in results],
            ["LICENSE", "COPYING.third-party"],
        )


class GitHubAutomationContractTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent

    def test_workflows_pin_actions_and_use_minimum_permissions(self):
        workflow_dir = self.root / ".github" / "workflows"
        ci = (workflow_dir / "ci.yml").read_text(encoding="utf-8")
        release = (workflow_dir / "release.yml").read_text(encoding="utf-8")

        for text in (ci, release):
            self.assertNotIn("pull_request_target", text)
            uses_lines = [line.strip() for line in text.splitlines() if "uses:" in line]
            self.assertTrue(uses_lines)
            for line in uses_lines:
                self.assertRegex(line, r"uses:\s+[^@\s]+@[0-9a-f]{40}\s+#\s+v\d")

        self.assertIn("permissions:\n  contents: read", ci)
        self.assertIn("runs-on: windows-latest", ci)
        self.assertIn("uv lock --check", ci)
        self.assertIn("python -m unittest", ci)
        self.assertIn("python -m unittest discover -q", ci)
        self.assertIn("upload-artifact@", ci)
        self.assertIn("python -m compileall", ci)
        self.assertIn("build_exe.ps1", ci)

        self.assertIn("tags:\n      - \"v*\"", release)
        self.assertIn("workflow_dispatch:", release)
        self.assertIn("RELEASE_TAG: ${{ inputs.tag || github.ref_name }}", release)
        self.assertIn("ref: ${{ inputs.tag || github.ref }}", release)
        self.assertIn("contents: write", release)
        self.assertIn("GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}", release)
        self.assertIn("gh release create", release)
        self.assertIn("python -m unittest discover -q", release)
        self.assertIn("PSNativeCommandUseErrorActionPreference = $true", release)
        self.assertIn("--repo $env:GITHUB_REPOSITORY", release)
        self.assertIn("CodexBar-Windows-x64.zip", release)
        self.assertIn("CodexBar-Windows-x64.zip.sha256", release)

    def test_dependabot_and_contribution_templates_exist(self):
        dependabot = (self.root / ".github" / "dependabot.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('package-ecosystem: "uv"', dependabot)
        self.assertIn('package-ecosystem: "github-actions"', dependabot)
        self.assertIn("interval: \"weekly\"", dependabot)

        required = (
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
        )
        for relative in required:
            self.assertTrue((self.root / relative).is_file(), relative)

        bug_form = (self.root / required[1]).read_text(encoding="utf-8")
        config_form = (self.root / required[3]).read_text(encoding="utf-8")
        self.assertIn("不要粘贴", bug_form)
        self.assertIn("access token", bug_form.lower())
        self.assertIn("security/advisories/new", config_form)


class PublicDocumentationContractTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent

    def test_chinese_and_english_readmes_cover_release_safety(self):
        readme_zh = (self.root / "README.md").read_text(encoding="utf-8")
        readme_en_path = self.root / "README_EN.md"
        self.assertTrue(readme_en_path.is_file())
        readme_en = readme_en_path.read_text(encoding="utf-8")

        for required in (
            "README_EN.md",
            "CodexBar-Windows-x64.zip",
            "SHA-256",
            "SmartScreen",
            "%LOCALAPPDATA%\\CodexBar",
            "非 OpenAI 官方",
            "未公开接口",
            "不包含遥测",
            "卸载",
        ):
            self.assertIn(required, readme_zh)

        for required in (
            "README.md",
            "CodexBar-Windows-x64.zip",
            "SHA-256",
            "SmartScreen",
            r"%LOCALAPPDATA%\CodexBar",
            "not affiliated with OpenAI",
            "undocumented endpoint",
            "does not include telemetry",
            "Uninstall",
        ):
            self.assertIn(required, readme_en)

    def test_public_docs_link_legal_privacy_and_security_policies(self):
        for filename in ("README.md", "README_EN.md"):
            text = (self.root / filename).read_text(encoding="utf-8")
            for policy in (
                "LICENSE",
                "PRIVACY.md",
                "SECURITY.md",
                "CONTRIBUTING.md",
                "THIRD_PARTY_NOTICES.md",
            ):
                self.assertIn(policy, text, f"{filename}: {policy}")

        readme_zh = (self.root / "README.md").read_text(encoding="utf-8")
        for project_file in (
            "diagnostics.py",
            "version_info.txt",
            "audit_release.py",
            "collect_licenses.py",
        ):
            self.assertIn(project_file, readme_zh)

    def test_readme_local_links_resolve_to_tracked_files(self):
        for filename in ("README.md", "README_EN.md"):
            text = (self.root / filename).read_text(encoding="utf-8")
            targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
            local_targets = [
                target.split("#", 1)[0]
                for target in targets
                if target and "://" not in target and not target.startswith("#")
            ]
            for target in local_targets:
                self.assertTrue(
                    (self.root / target).is_file(),
                    f"{filename}: unresolved local link {target}",
                )

    def test_public_text_does_not_publish_personal_contact_or_machine_paths(self):
        public_extensions = {
            ".bat",
            ".json",
            ".md",
            ".ps1",
            ".py",
            ".pyw",
            ".spec",
            ".toml",
            ".yaml",
            ".yml",
        }
        public_files = [
            path
            for path in self.root.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in public_extensions
            and not {".git", ".venv", "build", "dist"}.intersection(path.parts)
        ]
        forbidden = (
            "@" + "gmail.com",
            r"c:\users" + r"\user",
            "c:/users/" + "user",
            r"c:\projects" + r"\little-tool",
            "c:/projects/" + "little-tool",
        )
        for path in public_files:
            text = path.read_text(encoding="utf-8")
            lowered = text.casefold()
            for value in forbidden:
                self.assertNotIn(value.casefold(), lowered, str(path))


class DiagnosticsLoggingTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(ui.pricing, "refresh_prices", return_value={})
        patch.start()
        self.addCleanup(patch.stop)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self.temp.name, "error.log")
        self.patch = mock.patch.object(config, "ERROR_LOG_PATH", self.log_path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def _read_log_line(self):
        with open(self.log_path, encoding="utf-8") as file:
            return json.loads(file.readline())

    def test_log_event_writes_jsonl_without_token_values(self):
        diagnostics.log_event(
            "quota_error",
            stage="quota_fetch",
            message="Authorization: Bearer secret-token access_token=abc",
            refresh_token="secret-refresh",
        )
        line = self._read_log_line()
        self.assertEqual(line["event"], "quota_error")
        self.assertEqual(line["stage"], "quota_fetch")
        text = json.dumps(line)
        self.assertNotIn("secret-token", text)
        self.assertNotIn("secret-refresh", text)
        self.assertNotIn("access_token=abc", text)

    def test_log_exception_records_http_status(self):
        request = urllib.request.Request("https://example.test")
        error = urllib.error.HTTPError(request.full_url, 503, "unavailable", None, None)
        diagnostics.log_exception("quota_fetch", error)
        line = self._read_log_line()
        self.assertEqual(line["event"], "exception")
        self.assertEqual(line["stage"], "quota_fetch")
        self.assertEqual(line["error_type"], "HTTPError")
        self.assertEqual(line["http_status"], 503)

    def test_sanitize_text_redacts_credentials_account_and_user_profile(self):
        profile = r"C:\Users\PrivateName"
        fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwcml2YXRlIn0.signaturevalue"
        source = (
            "Authorization: Bearer bearer-secret "
            "access_token=oauth-access&refresh_token=oauth-refresh "
            '"id_token":"oauth-id" '
            "OPENAI_API_KEY=sk-this-is-a-fake-secret-key "
            f"jwt={fake_jwt} "
            "account_id=acc-private-123 "
            "https://example.test/?api_key=query-private&safe=yes "
            rf"{profile}\Documents\rollout.jsonl"
        )

        with mock.patch.dict(os.environ, {"USERPROFILE": profile}, clear=False):
            sanitized = diagnostics.sanitize_text(source)

        for secret in (
            "bearer-secret",
            "oauth-access",
            "oauth-refresh",
            "oauth-id",
            "sk-this-is-a-fake-secret-key",
            fake_jwt,
            "acc-private-123",
            "query-private",
            "PrivateName",
        ):
            self.assertNotIn(secret, sanitized)
        self.assertIn("%USERPROFILE%", sanitized)
        self.assertEqual(diagnostics.sanitize_text(sanitized), sanitized)

    def test_export_sanitized_diagnostics_re_sanitizes_rotated_logs(self):
        backup_path = self.log_path + ".1"
        destination = os.path.join(self.temp.name, "diagnostics.txt")
        profile = r"C:\Users\PrivateName"
        with open(backup_path, "w", encoding="utf-8") as file:
            file.write(
                rf'{{"message":"Bearer old-secret {profile}\old.log"}}' + "\n"
            )
        with open(self.log_path, "w", encoding="utf-8") as file:
            file.write('{"account_id":"account-private","message":"sk-fake-private-key"}\n')

        with mock.patch.dict(os.environ, {"USERPROFILE": profile}, clear=False):
            result = diagnostics.export_sanitized_diagnostics(destination, "0.1.0")

        self.assertEqual(result, destination)
        exported = Path(destination).read_text(encoding="utf-8")
        self.assertIn("CodexBar sanitized diagnostics", exported)
        self.assertIn("version: 0.1.0", exported)
        self.assertIn("%USERPROFILE%", exported)
        for secret in (
            "old-secret",
            "PrivateName",
            "account-private",
            "sk-fake-private-key",
        ):
            self.assertNotIn(secret, exported)
        self.assertFalse(os.path.exists(destination + ".tmp"))

    def test_widget_exports_diagnostics_only_after_user_selects_destination(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.root = object()
        destination = r"C:\Temp\CodexBar-diagnostics.txt"
        with mock.patch.object(
            ui.filedialog, "asksaveasfilename", return_value=destination
        ) as ask:
            with mock.patch.object(
                ui.diagnostics,
                "export_sanitized_diagnostics",
                return_value=destination,
            ) as export:
                with mock.patch.object(ui.messagebox, "showinfo") as showinfo:
                    ui.QuotaWidget.export_diagnostics(widget)

        self.assertEqual(ask.call_args.kwargs["parent"], widget.root)
        export.assert_called_once_with(destination, ui.__version__)
        showinfo.assert_called_once()

    def test_refresh_worker_logs_unexpected_quota_exception(self):
        class FakeRoot:
            def __init__(self):
                self.calls = []

            def after(self, *args):
                self.calls.append(args)
                return "after-id"

        widget = object.__new__(ui.QuotaWidget)
        widget.root = FakeRoot()
        with mock.patch.object(ui, "fetch_quota", side_effect=RuntimeError("boom")):
            with mock.patch.object(ui.diagnostics, "log_exception") as log_exception:
                ui.QuotaWidget._refresh_worker(widget)
        log_exception.assert_called_once()
        self.assertEqual(log_exception.call_args.args[0], "quota_refresh")
        self.assertEqual(widget.root.calls[0][-2:], ("ERR", None))

    def test_open_error_log_creates_log_before_opening(self):
        widget = object.__new__(ui.QuotaWidget)
        with mock.patch.object(ui.os.path, "exists", return_value=False):
            with mock.patch.object(ui.diagnostics, "log_event") as log_event:
                with mock.patch.object(ui.os, "startfile") as startfile:
                    ui.QuotaWidget.open_error_log(widget)
        log_event.assert_called_once_with("log_created", stage="manual_open")
        startfile.assert_called_once_with(config.ERROR_LOG_PATH)

    def test_open_usage_dashboard_starts_webview_entry_process(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.root = object()
        process = mock.Mock()
        process.poll.return_value = None
        command = [r"C:\Python\pythonw.exe", r"C:\Work\CodexBar\codexbar_dashboard.pyw"]
        cwd = r"C:\Work\CodexBar"
        with mock.patch.object(ui.runtime, "dashboard_command", return_value=(command, cwd)):
            with mock.patch.object(ui.subprocess, "Popen", return_value=process) as popen:
                ui.QuotaWidget.open_usage_dashboard(widget)
        args = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(args, command)
        self.assertEqual(kwargs["cwd"], cwd)
        self.assertTrue(kwargs["close_fds"])
        self.assertIs(widget._usage_dashboard_process, process)

    def test_open_usage_dashboard_uses_frozen_dashboard_command(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.root = object()
        process = mock.Mock()
        process.poll.return_value = None
        command = [r"C:\Apps\CodexBar\CodexBar.exe", "--dashboard"]
        cwd = r"C:\Apps\CodexBar"
        with mock.patch.object(ui.runtime, "dashboard_command", return_value=(command, cwd)):
            with mock.patch.object(ui.subprocess, "Popen", return_value=process) as popen:
                ui.QuotaWidget.open_usage_dashboard(widget)
        self.assertEqual(popen.call_args.args[0], command)
        self.assertEqual(popen.call_args.kwargs["cwd"], cwd)

    def test_pyinstaller_packaging_files_are_present(self):
        root = os.path.dirname(__file__)
        spec_path = os.path.join(root, "CodexBar.spec")
        script_path = os.path.join(root, "scripts", "build_exe.ps1")
        pyproject_path = os.path.join(root, "pyproject.toml")
        launcher_path = os.path.join(root, "启动 CodexBar.bat")
        gitignore_path = os.path.join(root, ".gitignore")

        with open(spec_path, encoding="utf-8") as file:
            spec = file.read()
        self.assertIn("codexbar/web_assets", spec)
        self.assertIn("codexbar/assets", spec)
        self.assertIn("webview.platforms.winforms", spec)
        self.assertIn("codexbar.ico", spec)
        self.assertIn("SPECPATH", spec)
        self.assertIn("upx=False", spec)

        with open(script_path, encoding="utf-8-sig") as file:
            script = file.read()
        self.assertIn("pyinstaller", script)
        self.assertIn("CodexBar.spec", script)
        self.assertIn("dist\\CodexBar\\CodexBar.exe", script)
        self.assertIn("--frozen", script)
        self.assertIn("dashboard.html", script)
        self.assertIn("codexbar.ico", script)

        with open(pyproject_path, encoding="utf-8") as file:
            pyproject = file.read()
        self.assertIn('pyinstaller==6.21.0', pyproject.lower())

        with open(launcher_path, encoding="utf-8-sig") as file:
            launcher = file.read().lower()
        self.assertIn(r".venv\scripts\pythonw.exe", launcher)
        self.assertIn("uv sync --quiet --frozen", launcher)
        self.assertNotIn("miniconda3", launcher)
        self.assertNotIn("uv pip install", launcher)

        with open(gitignore_path, encoding="utf-8") as file:
            gitignore = file.read()
        self.assertIn("build/", gitignore)
        self.assertIn("dist/", gitignore)


class TlsConfigurationTests(unittest.TestCase):
    def test_default_context_keeps_tls_verification_enabled(self):
        context = quota_api._build_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_custom_ca_bundle_is_forwarded_to_ssl_context(self):
        with mock.patch.dict(
            os.environ, {"CODEXBAR_CA_BUNDLE": r"C:\proxy\root-ca.pem"}, clear=False
        ):
            with mock.patch.object(
                quota_api.ssl, "create_default_context", return_value=mock.Mock()
            ) as create_context:
                quota_api._build_ssl_context()
        create_context.assert_called_once_with(cafile=r"C:\proxy\root-ca.pem")

    def test_proxy_keeps_strict_tls_and_adds_proxy_handler(self):
        fake_context = mock.Mock()
        https_handler = object()
        proxy_handler = object()
        with mock.patch.dict(
            os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890"}, clear=False
        ):
            with mock.patch.object(
                quota_api, "_build_ssl_context", return_value=fake_context
            ) as build_context, mock.patch.object(
                quota_api.urllib.request, "HTTPSHandler", return_value=https_handler
            ) as https_ctor, mock.patch.object(
                quota_api.urllib.request, "ProxyHandler", return_value=proxy_handler
            ) as proxy_ctor, mock.patch.object(
                quota_api.urllib.request, "build_opener", return_value=mock.Mock()
            ) as build_opener:
                quota_api._build_opener()

        build_context.assert_called_once_with()
        https_ctor.assert_called_once_with(context=fake_context)
        proxy_ctor.assert_called_once_with(
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
        )
        build_opener.assert_called_once_with(proxy_handler, https_handler)


class ConfigMigrationTests(unittest.TestCase):
    DEFAULT_CFG_PATH = config.CFG_PATH

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self.temp.name, ".codexbar_cfg.json")
        self.legacy_cfg_path = os.path.join(self.temp.name, ".quota_widget_cfg.json")
        self.older_legacy_cfg_path = os.path.join(
            self.temp.name, ".older_quota_widget_cfg.json"
        )
        self.cfg_patch = mock.patch.object(config, "CFG_PATH", self.cfg_path)
        self.legacy_patch = mock.patch.object(
            config, "LEGACY_CFG_PATH", self.legacy_cfg_path
        )
        self.older_legacy_patch = mock.patch.object(
            config, "OLDER_LEGACY_CFG_PATH", self.older_legacy_cfg_path
        )
        self.cfg_patch.start()
        self.legacy_patch.start()
        self.older_legacy_patch.start()

    def tearDown(self):
        self.older_legacy_patch.stop()
        self.legacy_patch.stop()
        self.cfg_patch.stop()
        self.temp.cleanup()

    def test_load_config_migrates_legacy_path(self):
        with open(self.legacy_cfg_path, "w", encoding="utf-8") as file:
            json.dump({"refresh_minutes": 15}, file)
        loaded = config.load_config()
        self.assertEqual(loaded["refresh_minutes"], 15)
        self.assertTrue(os.path.exists(self.cfg_path))

    def test_load_config_can_migrate_oldest_legacy_path(self):
        with open(self.older_legacy_cfg_path, "w", encoding="utf-8") as file:
            json.dump({"refresh_minutes": 23}, file)

        loaded = config.load_config()

        self.assertEqual(loaded["refresh_minutes"], 23)
        self.assertTrue(os.path.exists(self.older_legacy_cfg_path))

    def test_default_config_path_belongs_to_codexbar_localappdata(self):
        self.assertEqual(
            os.path.normcase(self.DEFAULT_CFG_PATH),
            os.path.normcase(os.path.join(config.VAULT_DIR, "config.json")),
        )

    def test_new_config_wins_over_legacy_and_legacy_is_not_deleted(self):
        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump({"refresh_minutes": 7}, file)
        with open(self.legacy_cfg_path, "w", encoding="utf-8") as file:
            json.dump({"refresh_minutes": 19}, file)

        loaded = config.load_config()

        self.assertEqual(loaded["refresh_minutes"], 7)
        self.assertTrue(os.path.exists(self.legacy_cfg_path))

    def test_migration_validates_legacy_values_before_persisting(self):
        with open(self.legacy_cfg_path, "w", encoding="utf-8") as file:
            json.dump({"width": 10, "refresh_minutes": "bad"}, file)

        loaded = config.load_config()

        self.assertEqual(loaded["width"], config.W_MIN)
        self.assertEqual(loaded["refresh_minutes"], config.DEFAULTS["refresh_minutes"])
        with open(self.cfg_path, encoding="utf-8") as file:
            persisted = json.load(file)
        self.assertEqual(persisted, loaded)
        self.assertTrue(os.path.exists(self.legacy_cfg_path))

    def test_damaged_legacy_config_does_not_create_new_file(self):
        with open(self.legacy_cfg_path, "w", encoding="utf-8") as file:
            file.write("{not-json")

        loaded = config.load_config()

        self.assertEqual(loaded, config.DEFAULTS)
        self.assertFalse(os.path.exists(self.cfg_path))

    def test_config_migration_never_touches_auth_json(self):
        auth_path = os.path.join(self.temp.name, "auth.json")
        original = b'{"tokens":{"access_token":"leave-me-alone"}}'
        with open(auth_path, "wb") as file:
            file.write(original)
        with open(self.legacy_cfg_path, "w", encoding="utf-8") as file:
            json.dump({"width": 320}, file)

        with mock.patch.object(config, "AUTH_PATH", auth_path):
            config.load_config()

        with open(auth_path, "rb") as file:
            self.assertEqual(file.read(), original)

    def test_invalid_numeric_values_fall_back_to_defaults(self):
        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump(
                {
                    "width": "wide",
                    "height": None,
                    "font_scale_percent": [],
                    "refresh_minutes": "often",
                },
                file,
            )

        loaded = config.load_config()

        self.assertEqual(loaded["width"], config.DEFAULTS["width"])
        self.assertEqual(loaded["height"], config.DEFAULTS["height"])
        self.assertEqual(
            loaded["font_scale_percent"], config.DEFAULTS["font_scale_percent"]
        )
        self.assertEqual(
            loaded["refresh_minutes"], config.DEFAULTS["refresh_minutes"]
        )

    def test_missing_widget_background_uses_current_card_color(self):
        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump({"width": 300}, file)

        loaded = config.load_config()

        self.assertEqual(loaded["widget_background"], config.CARD)

    def test_widget_background_round_trips_through_config(self):
        updated = {**config.DEFAULTS, "widget_background": "#f0e68c"}

        config.save_config(updated)
        loaded = config.load_config()

        self.assertEqual(loaded["widget_background"], "#f0e68c")

    def test_invalid_widget_background_uses_default(self):
        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump({"widget_background": "not-a-color"}, file)

        loaded = config.load_config()

        self.assertEqual(loaded["widget_background"], config.CARD)

    def test_transparent_key_cannot_be_used_as_widget_background(self):
        for transparent_key in ("#ff00fe", "#FF00FE", "#Ff00Fe"):
            with self.subTest(transparent_key=transparent_key):
                self.assertEqual(
                    config.valid_widget_background(transparent_key),
                    config.CARD,
                )

    def test_failed_atomic_save_keeps_previous_config(self):
        previous = {**config.DEFAULTS, "width": 280}
        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump(previous, file)

        with mock.patch.object(config.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                config.save_config({**config.DEFAULTS, "width": 420})

        with open(self.cfg_path, encoding="utf-8") as file:
            self.assertEqual(json.load(file), previous)

    def test_atomic_save_replaces_config_and_removes_temporary_file(self):
        updated = {**config.DEFAULTS, "width": 320}

        config.save_config(updated)

        with open(self.cfg_path, encoding="utf-8") as file:
            self.assertEqual(json.load(file), updated)
        self.assertFalse(os.path.exists(self.cfg_path + ".tmp"))

    def test_too_narrow_width_is_clamped_to_minimum(self):
        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump({"width": 200, "refresh_minutes": 15}, file)
        loaded = config.load_config()
        self.assertEqual(loaded["width"], 250)

    def test_minimum_width_is_preserved(self):
        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump({"width": 250, "refresh_minutes": 15}, file)
        loaded = config.load_config()
        self.assertEqual(loaded["width"], 250)

    def test_default_width_is_preserved(self):
        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump({"width": 300, "refresh_minutes": 15}, file)
        loaded = config.load_config()
        self.assertEqual(loaded["width"], 300)

    def test_user_width_at_or_above_minimum_is_preserved(self):
        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump({"width": 360, "refresh_minutes": 15}, file)
        loaded = config.load_config()
        self.assertEqual(loaded["width"], 360)

    def test_missing_font_scale_defaults_to_100_percent(self):
        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump({"width": 300, "refresh_minutes": 15}, file)
        loaded = config.load_config()
        self.assertEqual(loaded["font_scale_percent"], 100)

    def test_font_scale_is_clamped_to_supported_range(self):
        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump({"font_scale_percent": 60}, file)
        self.assertEqual(config.load_config()["font_scale_percent"], 80)

        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump({"font_scale_percent": 160}, file)
        self.assertEqual(config.load_config()["font_scale_percent"], 130)

    def test_valid_font_scale_is_preserved(self):
        with open(self.cfg_path, "w", encoding="utf-8") as file:
            json.dump({"font_scale_percent": 115}, file)
        self.assertEqual(config.load_config()["font_scale_percent"], 115)


class LauncherIconTests(unittest.TestCase):
    def test_icon_asset_and_source_launcher_are_present(self):
        root = os.path.dirname(__file__)
        icon_path = os.path.join(root, "codexbar", "assets", "codexbar.ico")
        launcher_path = os.path.join(root, "启动 CodexBar.bat")
        gitignore_path = os.path.join(root, ".gitignore")

        self.assertGreater(os.path.getsize(icon_path), 1024)
        with open(launcher_path, encoding="utf-8-sig") as file:
            launcher = file.read()
        self.assertIn("codexbar.pyw", launcher)
        with open(gitignore_path, encoding="utf-8") as file:
            self.assertIn("*.lnk", file.read())


class CredentialVaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth_path = os.path.join(self.temp.name, "auth.json")
        self.vault_path = os.path.join(self.temp.name, "CodexBar", "credentials.dat")
        self.legacy_vault_path = os.path.join(
            self.temp.name, "CodexQuota", "credentials.dat"
        )
        self.auth_patch = mock.patch.object(credentials, "AUTH_PATH", self.auth_path)
        self.vault_patch = mock.patch.object(credentials, "VAULT_PATH", self.vault_path)
        self.legacy_vault_patch = mock.patch.object(
            credentials, "LEGACY_VAULT_PATH", self.legacy_vault_path
        )
        self.auth_patch.start()
        self.vault_patch.start()
        self.legacy_vault_patch.start()

    def tearDown(self):
        self.legacy_vault_patch.stop()
        self.vault_patch.stop()
        self.auth_patch.stop()
        self.temp.cleanup()

    def write_auth(self, data):
        with open(self.auth_path, "w", encoding="utf-8") as file:
            json.dump(data, file)

    @staticmethod
    def oauth_auth(account="acct-one"):
        return {
            "tokens": {
                "access_token": f"access-{account}",
                "refresh_token": f"refresh-{account}",
                "account_id": account,
                "id_token": f"id-{account}",
            }
        }

    def oauth_credentials(self, account="acct-one"):
        return credentials._credentials_from_auth(self.oauth_auth(account))

    def test_dpapi_round_trip_contains_no_plaintext_tokens(self):
        value = self.oauth_credentials()
        credentials.save_credentials(value)
        with open(self.vault_path, "rb") as file:
            encrypted = file.read()
        self.assertTrue(encrypted.startswith(credentials.VAULT_MAGIC))
        self.assertNotIn(b"access-acct-one", encrypted)
        self.assertNotIn(b"refresh-acct-one", encrypted)
        self.assertEqual(credentials.load_credentials()["account_id"], "acct-one")
        vault = credentials.load_vault()
        self.assertEqual(vault["version"], credentials.VAULT_FORMAT_VERSION)
        self.assertEqual(vault["active_account_id"], "acct-one")

    def test_oauth_capture_survives_switch_to_api_key(self):
        self.write_auth(self.oauth_auth())
        captured = credentials.resolve_credentials()
        self.assertEqual(captured["account_id"], "acct-one")
        self.assertTrue(os.path.isfile(self.vault_path))

        self.write_auth({"OPENAI_API_KEY": "not-used-by-widget"})
        cached = credentials.resolve_credentials()
        self.assertEqual(cached["refresh_token"], "refresh-acct-one")

    def test_new_oauth_account_is_added_and_selected(self):
        self.write_auth(self.oauth_auth("acct-one"))
        credentials.resolve_credentials()
        self.write_auth(self.oauth_auth("acct-two"))
        credentials.resolve_credentials()
        vault = credentials.load_vault()
        self.assertEqual(sorted(vault["accounts"]), ["acct-one", "acct-two"])
        self.assertEqual(vault["active_account_id"], "acct-two")
        self.assertEqual(credentials.load_credentials()["account_id"], "acct-two")

    def test_malformed_auth_falls_back_to_cache(self):
        credentials.save_credentials(self.oauth_credentials())
        with open(self.auth_path, "w", encoding="utf-8") as file:
            file.write("{broken")
        self.assertEqual(credentials.resolve_credentials()["account_id"], "acct-one")

    def test_unreadable_vault_is_not_overwritten_by_current_auth(self):
        self.write_auth(self.oauth_auth())
        os.makedirs(os.path.dirname(self.vault_path), exist_ok=True)
        original = b"unreadable-existing-vault"
        with open(self.vault_path, "wb") as file:
            file.write(original)

        with mock.patch.object(
            credentials,
            "load_vault",
            side_effect=credentials.CredentialVaultError("temporary DPAPI failure"),
        ):
            resolved = credentials.resolve_credentials()

        self.assertEqual(resolved["account_id"], "acct-one")
        with open(self.vault_path, "rb") as file:
            self.assertEqual(file.read(), original)

    def test_refresh_rotation_updates_only_vault(self):
        value = self.oauth_credentials()
        credentials.save_credentials(value)
        self.write_auth(self.oauth_auth())
        response = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "id_token": "new-id",
        }
        with mock.patch.object(
            quota_api, "_build_opener", return_value=JsonOpener(response)
        ):
            updated = quota_api.refresh_credentials(value)
        self.assertEqual(updated["refresh_token"], "new-refresh")
        self.assertEqual(credentials.load_credentials()["access_token"], "new-access")
        with open(self.auth_path, encoding="utf-8") as file:
            self.assertEqual(json.load(file)["tokens"]["access_token"], "access-acct-one")
        self.assertEqual(credentials.resolve_credentials()["access_token"], "new-access")

    def test_refresh_save_preserves_active_account_and_does_not_restore_deleted_account(self):
        account_one = self.oauth_credentials("acct-one")
        credentials.save_credentials(account_one)
        credentials.save_credentials(self.oauth_credentials("acct-two"))
        refreshed_one = dict(account_one, access_token="rotated-access")

        self.assertTrue(credentials.save_refreshed_credentials(refreshed_one))
        vault = credentials.load_vault()
        self.assertEqual(vault["active_account_id"], "acct-two")
        self.assertEqual(vault["accounts"]["acct-one"]["access_token"], "rotated-access")

        self.assertTrue(credentials.delete_account("acct-one"))
        self.assertFalse(credentials.save_refreshed_credentials(refreshed_one))
        self.assertNotIn("acct-one", credentials.load_vault()["accounts"])

    def test_newer_same_account_login_replaces_stale_cache(self):
        stale = self.oauth_credentials()
        stale["last_refresh"] = "2025-01-01T00:00:00Z"
        credentials.save_credentials(stale)
        active = self.oauth_auth()
        active["tokens"]["access_token"] = "new-login-access"
        active["tokens"]["refresh_token"] = "new-login-refresh"
        active["last_refresh"] = "2026-01-01T00:00:00Z"
        self.write_auth(active)
        self.assertEqual(
            credentials.resolve_credentials()["access_token"], "new-login-access"
        )

    def test_fetch_quota_uses_cache_while_api_key_is_active(self):
        credentials.save_credentials(self.oauth_credentials())
        self.write_auth({"OPENAI_API_KEY": "not-used-by-widget"})
        usage = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 25, "reset_at": 123},
                "secondary_window": {"used_percent": 40, "reset_at": 456},
            },
        }
        with mock.patch.object(
            quota_api, "_build_opener", return_value=JsonOpener(usage)
        ):
            result = quota_api.fetch_quota()
        self.assertEqual(result["h_remain"], 75)
        self.assertEqual(result["w_remain"], 60)

    def test_fetch_quota_recognizes_weekly_only_primary_window(self):
        credentials.save_credentials(self.oauth_credentials())
        usage = {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 2,
                    "reset_at": 1784487488,
                    "limit_window_seconds": 604800,
                },
                "secondary_window": None,
            },
        }

        with mock.patch.object(
            quota_api, "_build_opener", return_value=JsonOpener(usage)
        ):
            result = quota_api.fetch_quota()

        self.assertIsNone(result["h_remain"])
        self.assertIsNone(result["h_reset"])
        self.assertEqual(result["w_remain"], 98)
        self.assertEqual(result["w_reset"], 1784487488)

    def test_fetch_quota_classifies_windows_by_duration_not_field_name(self):
        credentials.save_credentials(self.oauth_credentials())
        usage = {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 20,
                    "reset_at": 700,
                    "limit_window_seconds": 604800,
                },
                "secondary_window": {
                    "used_percent": 40,
                    "reset_at": 500,
                    "limit_window_seconds": 18000,
                },
            },
        }

        with mock.patch.object(
            quota_api, "_build_opener", return_value=JsonOpener(usage)
        ):
            result = quota_api.fetch_quota()

        self.assertEqual(result["h_remain"], 60)
        self.assertEqual(result["h_reset"], 500)
        self.assertEqual(result["w_remain"], 80)
        self.assertEqual(result["w_reset"], 700)

    def test_fetch_quota_does_not_hold_credential_lock_during_http(self):
        credentials.save_credentials(self.oauth_credentials())
        self.write_auth({"OPENAI_API_KEY": "not-used-by-widget"})
        request_started = threading.Event()
        release_request = threading.Event()
        lock_acquired = threading.Event()
        usage = {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 25, "reset_at": 123},
                "secondary_window": {"used_percent": 40, "reset_at": 456},
            },
        }

        class BlockingOpener:
            def open(self, request, timeout=15):
                request_started.set()
                release_request.wait(2)
                return JsonResponse(json.dumps(usage))

        fetch_thread = threading.Thread(target=quota_api.fetch_quota)
        with mock.patch.object(quota_api, "_build_opener", return_value=BlockingOpener()):
            fetch_thread.start()
            self.assertTrue(request_started.wait(1))

            def acquire_lock():
                with credentials.CREDENTIAL_LOCK:
                    lock_acquired.set()

            probe = threading.Thread(target=acquire_lock)
            probe.start()
            acquired_while_request_waited = lock_acquired.wait(0.2)
            release_request.set()
            fetch_thread.join(2)
            probe.join(2)

        self.assertTrue(acquired_while_request_waited)
        self.assertFalse(fetch_thread.is_alive())

    def test_set_active_account_controls_api_key_fallback(self):
        credentials.save_credentials(self.oauth_credentials("acct-one"))
        credentials.save_credentials(self.oauth_credentials("acct-two"))
        credentials.set_active_account("acct-one")
        self.write_auth({"OPENAI_API_KEY": "not-used-by-widget"})
        self.assertEqual(credentials.resolve_credentials()["account_id"], "acct-one")

    def test_manual_account_choice_survives_existing_oauth_auth_file(self):
        credentials.save_credentials(self.oauth_credentials("acct-one"))
        credentials.save_credentials(self.oauth_credentials("acct-two"))
        credentials.set_active_account("acct-one")
        self.write_auth(self.oauth_auth("acct-two"))

        resolved = credentials.resolve_credentials()

        self.assertEqual(resolved["account_id"], "acct-one")
        self.assertEqual(credentials.load_vault()["active_account_id"], "acct-one")

    def test_list_accounts_marks_active_account(self):
        credentials.save_credentials(self.oauth_credentials("acct-one"))
        credentials.save_credentials(self.oauth_credentials("acct-two"))
        summaries = credentials.list_accounts()
        self.assertEqual([item["account_id"] for item in summaries], ["acct-one", "acct-two"])
        self.assertEqual([item["active"] for item in summaries], [False, True])

    def test_revoked_refresh_token_preserves_existing_vault(self):
        value = self.oauth_credentials()
        credentials.save_credentials(value)
        with open(self.vault_path, "rb") as file:
            before = file.read()
        with mock.patch.object(
            quota_api, "_build_opener", return_value=ErrorOpener(401)
        ):
            with self.assertRaises(credentials.ReloginRequiredError):
                quota_api.refresh_credentials(value)
        with open(self.vault_path, "rb") as file:
            self.assertEqual(file.read(), before)

    def test_clear_removes_new_and_legacy_cache(self):
        credentials.save_credentials(self.oauth_credentials())
        os.makedirs(os.path.dirname(self.legacy_vault_path), exist_ok=True)
        shutil.copy2(self.vault_path, self.legacy_vault_path)
        self.assertTrue(credentials.clear_credentials())
        self.assertFalse(os.path.exists(self.vault_path))
        self.assertFalse(os.path.exists(self.legacy_vault_path))

    def test_delete_current_account_selects_remaining_account(self):
        credentials.save_credentials(self.oauth_credentials("acct-one"))
        credentials.save_credentials(self.oauth_credentials("acct-two"))
        self.assertTrue(credentials.delete_account())
        vault = credentials.load_vault()
        self.assertEqual(sorted(vault["accounts"]), ["acct-one"])
        self.assertEqual(vault["active_account_id"], "acct-one")

    def test_delete_last_account_removes_legacy_cache_too(self):
        credentials.save_credentials(self.oauth_credentials())
        os.makedirs(os.path.dirname(self.legacy_vault_path), exist_ok=True)
        shutil.copy2(self.vault_path, self.legacy_vault_path)
        self.assertTrue(credentials.delete_account())
        self.assertFalse(os.path.exists(self.vault_path))
        self.assertFalse(os.path.exists(self.legacy_vault_path))

    def test_legacy_vault_is_migrated_to_new_location(self):
        legacy_value = self.oauth_credentials()
        credentials.save_credentials(
            legacy_value,
            path=self.legacy_vault_path,
            entropy=credentials.LEGACY_DPAPI_ENTROPY,
        )
        self.write_auth({"OPENAI_API_KEY": "not-used-by-widget"})

        resolved = credentials.resolve_credentials()

        self.assertEqual(resolved["account_id"], "acct-one")
        self.assertTrue(os.path.exists(self.vault_path))
        self.assertTrue(os.path.exists(self.legacy_vault_path))
        self.assertEqual(credentials.load_vault()["version"], credentials.VAULT_FORMAT_VERSION)

    def test_new_entropy_single_account_vault_is_wrapped_as_multi_account(self):
        credentials.save_credentials(
            self.oauth_credentials(), path=self.vault_path, entropy=credentials.DPAPI_ENTROPY
        )
        self.write_auth({"OPENAI_API_KEY": "not-used-by-widget"})
        self.assertEqual(credentials.resolve_credentials()["account_id"], "acct-one")
        self.assertEqual(credentials.load_vault()["version"], credentials.VAULT_FORMAT_VERSION)

    def test_clear_requires_auth_when_only_api_key_remains(self):
        credentials.save_credentials(self.oauth_credentials())
        self.write_auth({"OPENAI_API_KEY": "not-used-by-widget"})
        self.assertTrue(credentials.clear_credentials())
        with self.assertRaises(credentials.AuthRequiredError):
            credentials.resolve_credentials()


class ResetFormattingTests(unittest.TestCase):
    @staticmethod
    def local_timestamp(year, month, day, hour, minute):
        return quota_api.time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))

    def test_hours_and_minutes(self):
        reset = self.local_timestamp(2026, 7, 1, 3, 23)
        now = reset - (4 * 3600 + 40 * 60)
        with mock.patch.object(quota_api.time, "time", return_value=now):
            self.assertEqual(
                quota_api.format_reset_time(reset), "4h 40min (07/01 03:23)"
            )

    def test_days_hours_and_minutes(self):
        reset = self.local_timestamp(2026, 7, 7, 16, 8)
        now = reset - (6 * 86400 + 17 * 3600 + 24 * 60)
        with mock.patch.object(quota_api.time, "time", return_value=now):
            self.assertEqual(
                quota_api.format_reset_time(reset), "6d 17h 24min (07/07 16:08)"
            )

    def test_minutes_only(self):
        reset = self.local_timestamp(2026, 7, 1, 3, 23)
        with mock.patch.object(quota_api.time, "time", return_value=reset - 38 * 60):
            self.assertEqual(quota_api.format_reset_time(reset), "38min (07/01 03:23)")

    def test_expired_and_missing(self):
        reset = self.local_timestamp(2026, 7, 1, 3, 23)
        with mock.patch.object(quota_api.time, "time", return_value=reset + 1):
            self.assertEqual(quota_api.format_reset_time(reset), "已重置 (07/01 03:23)")
        self.assertEqual(quota_api.format_reset_time(None), "")


class RefreshCoordinationTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(ui.pricing, "refresh_prices", return_value={})
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def make_widget():
        widget = object.__new__(ui.QuotaWidget)
        widget.root = mock.Mock()
        widget.settings = dict(config.DEFAULTS)
        widget._refresh_after_id = None
        widget._refresh_in_progress = False
        widget._refresh_pending = False
        widget._refresh_generation = 0
        widget._closed = False
        widget._status_code = None
        widget._redraw = mock.Mock()
        return widget

    def test_refresh_requests_are_coalesced_while_worker_runs(self):
        widget = self.make_widget()

        with mock.patch.object(ui.threading, "Thread") as thread_class:
            ui.QuotaWidget.refresh_async(widget)
            ui.QuotaWidget.refresh_async(widget)

        self.assertEqual(thread_class.call_count, 1)
        self.assertTrue(widget._refresh_in_progress)
        self.assertTrue(widget._refresh_pending)
        self.assertEqual(widget._refresh_generation, 2)

    def test_stale_result_starts_pending_refresh_without_updating_ui(self):
        widget = self.make_widget()
        widget._refresh_generation = 2
        widget._refresh_in_progress = True
        widget._refresh_pending = True
        widget._apply = mock.Mock()
        widget._apply_error = mock.Mock()
        widget._start_refresh_worker = mock.Mock()

        ui.QuotaWidget._finish_refresh(
            widget,
            generation=1,
            data={"plan": "plus"},
            usage=None,
            error_code=None,
        )

        widget._apply.assert_not_called()
        widget._apply_error.assert_not_called()
        widget._start_refresh_worker.assert_called_once_with(2)

    def test_close_cancels_timer_and_blocks_future_refreshes(self):
        widget = self.make_widget()
        widget._refresh_after_id = "timer-1"

        ui.QuotaWidget.close(widget)
        ui.QuotaWidget.refresh_async(widget)

        self.assertTrue(widget._closed)
        widget.root.after_cancel.assert_called_once_with("timer-1")
        widget.root.destroy.assert_called_once()

    def test_clear_all_accounts_invalidates_inflight_result(self):
        widget = self.make_widget()
        widget._refresh_generation = 1
        widget._refresh_in_progress = True
        widget.data = {}
        widget._empty_data = mock.Mock(return_value={})
        widget._apply_error = mock.Mock()

        with mock.patch.object(ui.messagebox, "askyesno", return_value=True), mock.patch.object(
            ui, "clear_credentials"
        ):
            ui.QuotaWidget._clear_all_accounts(widget)

        self.assertEqual(widget._refresh_generation, 2)
        self.assertFalse(widget._refresh_pending)
        widget._apply_error.assert_called_once_with("AUTH")

    def test_incomplete_token_scan_is_marked_as_approximate_on_taskbar(self):
        widget = self.make_widget()
        widget._schedule_next = mock.Mock()
        widget._position_at_taskbar = mock.Mock()
        usage = token_usage.empty_usage()
        usage.update(total_tokens=1_200_000, cost_usd=1.23, incomplete=True)
        quota = {
            "plan": "plus",
            "h_remain": 75.0,
            "w_remain": 60.0,
            "h_reset": 123.0,
            "w_reset": 456.0,
        }

        ui.QuotaWidget._apply(widget, quota, usage)

        self.assertEqual(widget.data["usage"]["tokens"], "~1.2M")
        self.assertEqual(widget.data["usage"]["cost"], "~$1.23")


class ContextMenuStateTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(taskbar, "primary_scale", return_value=1.0)
        patch.start()
        self.addCleanup(patch.stop)

    def test_scale_uses_configured_height_when_actual_taskbar_height_shrinks(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.W = 300
        widget.H = 36
        widget.desired_H = 40
        widget.settings = {"font_scale_percent": 100}

        self.assertEqual(ui.QuotaWidget._scale(widget), 1.0)

    def test_scale_applies_user_font_scale_percent(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.W = 300
        widget.H = 40
        widget.desired_H = 40
        widget.settings = {"font_scale_percent": 120}

        self.assertEqual(ui.QuotaWidget._scale(widget), 1.2)

    def test_preview_font_scale_rebuilds_main_widget_without_saving(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.settings = {"font_scale_percent": 100}
        widget._build_ui = mock.Mock()

        ui.QuotaWidget._preview_font_scale(widget, 115)

        self.assertEqual(widget.settings["font_scale_percent"], 115)
        widget._build_ui.assert_called_once()

    def test_preview_width_applies_to_main_widget_immediately(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.settings = dict(config.DEFAULTS, width=300)
        widget.W = 300
        widget.H = 40
        widget.desired_H = 40
        widget.root = mock.Mock()
        widget._build_ui = mock.Mock()
        widget._position_at_taskbar = mock.Mock()

        ui.QuotaWidget._preview_width(widget, 360)

        self.assertEqual(widget.settings["width"], 360)
        self.assertEqual(widget.W, 360)
        widget.root.geometry.assert_called_once_with("360x40")
        widget._build_ui.assert_called_once()
        widget._position_at_taskbar.assert_called_once()

    def test_preview_color_applies_to_main_widget_immediately(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.settings = dict(config.DEFAULTS)
        widget._redraw = mock.Mock()

        ui.QuotaWidget._preview_color(widget, "color_good", "#123456")

        self.assertEqual(widget.settings["color_good"], "#123456")
        self.assertEqual(ui.COLORS["good"], "#123456")
        widget._redraw.assert_called_once()

    def test_preview_widget_background_applies_without_saving(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.settings = dict(config.DEFAULTS)
        widget._redraw = mock.Mock()

        ui.QuotaWidget._preview_widget_background(widget, "#abcdef")

        self.assertEqual(widget.settings["widget_background"], "#abcdef")
        widget._redraw.assert_called_once()

    def test_redraw_uses_configured_widget_background(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.canvas = mock.Mock()
        widget.settings = dict(config.DEFAULTS, widget_background="#abcdef")
        widget.W = 300
        widget.H = 40
        widget._status_code = "AUTH"
        widget.f_status = object()

        ui.QuotaWidget._redraw(widget)

        self.assertEqual(widget.canvas.create_polygon.call_args.kwargs["fill"], "#abcdef")

    def test_contrast_foreground_is_dark_on_light_background(self):
        self.assertEqual(ui.contrast_foreground("#ffffff"), config.BG)

    def test_contrast_foreground_is_light_on_dark_background(self):
        self.assertEqual(ui.contrast_foreground("#10243a"), config.FG)

    def test_settings_layout_keeps_color_controls_above_action_buttons(self):
        layout = ui.settings_layout_metrics()

        self.assertGreaterEqual(
            layout["action_divider_y"] - layout["color_controls_bottom_y"], 24
        )
        self.assertGreaterEqual(layout["close_font_size"], 13)

    def test_restore_settings_snapshot_rebuilds_and_repositions(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.settings = dict(
            config.DEFAULTS,
            width=360,
            height=40,
            font_scale_percent=120,
            widget_background="#ffffff",
        )
        widget.W = 360
        widget.H = 40
        widget.desired_H = 40
        widget.root = mock.Mock()
        widget._build_ui = mock.Mock()
        widget._position_at_taskbar = mock.Mock()

        ui.QuotaWidget._restore_settings_snapshot(
            widget,
            dict(config.DEFAULTS, width=300, height=40, font_scale_percent=100),
        )

        self.assertEqual(widget.settings["font_scale_percent"], 100)
        self.assertEqual(widget.settings["widget_background"], config.CARD)
        self.assertEqual(widget.W, 300)
        self.assertEqual(widget.desired_H, 40)
        widget.root.geometry.assert_called_once_with("300x40")
        widget._build_ui.assert_called_once()
        widget._position_at_taskbar.assert_called_once()

    def test_persist_settings_reports_write_failure_without_closing_editor(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.settings = dict(config.DEFAULTS)
        parent = object()
        error = OSError("disk full")

        with mock.patch.object(config, "save_config", side_effect=error), mock.patch.object(
            ui.diagnostics, "log_exception"
        ) as log_exception, mock.patch.object(ui.messagebox, "showerror") as showerror:
            saved = ui.QuotaWidget._persist_settings(widget, parent)

        self.assertFalse(saved)
        log_exception.assert_called_once_with("config_save", error)
        showerror.assert_called_once()
        self.assertIs(showerror.call_args.kwargs["parent"], parent)

    def test_persist_settings_returns_true_after_successful_write(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.settings = dict(config.DEFAULTS)

        with mock.patch.object(config, "save_config") as save_config:
            saved = ui.QuotaWidget._persist_settings(widget, object())

        self.assertTrue(saved)
        save_config.assert_called_once_with(widget.settings)

    def test_positioning_keeps_requesting_configured_height_after_taskbar_shrinks(self):
        widget = object.__new__(ui.QuotaWidget)
        widget._context_menu_open = False
        widget.root = mock.Mock()
        widget.root.winfo_id.return_value = 1234
        widget.W = 300
        widget.H = 40
        widget.desired_H = 40
        widget._build_ui = mock.Mock()

        with mock.patch.object(
            ui.taskbar,
            "position_taskbar_popup",
            side_effect=[(300, 36), (300, 40)],
        ) as position:
            ui.QuotaWidget._position_at_taskbar(widget)
            ui.QuotaWidget._position_at_taskbar(widget)

        self.assertEqual(position.call_args_list[0].args[:3], (1234, 300, 40))
        self.assertEqual(position.call_args_list[1].args[:3], (1234, 300, 40))
        self.assertEqual(widget.H, 40)

    def test_positioning_is_skipped_while_context_menu_is_open(self):
        widget = object.__new__(ui.QuotaWidget)
        widget._context_menu_open = True
        widget.root = mock.Mock()
        widget.W = 300
        widget.H = 40
        with mock.patch.object(ui.taskbar, "position_taskbar_popup") as position:
            ui.QuotaWidget._position_at_taskbar(widget)
        position.assert_not_called()

    def test_positioning_restores_configured_width_after_screen_limit(self):
        widget = object.__new__(ui.QuotaWidget)
        widget._context_menu_open = False
        widget.root = mock.Mock()
        widget.root.winfo_id.return_value = 1234
        widget.settings = {"width": 600}
        widget.W, widget.H, widget.desired_H = 300, 40, 40
        widget._build_ui = mock.Mock()
        with mock.patch.object(ui.taskbar, "position_taskbar_popup", return_value=(600, 40)) as position:
            ui.QuotaWidget._position_at_taskbar(widget)
        self.assertEqual(position.call_args.args[:3], (1234, 600, 40))
        self.assertEqual(widget.W, 600)

    def test_close_context_menu_restores_positioning(self):
        widget = object.__new__(ui.QuotaWidget)
        widget._context_menu_open = True
        widget.root = mock.Mock()
        widget.W = 300
        widget.H = 40
        with mock.patch.object(
            ui.taskbar, "position_taskbar_popup", return_value=(300, 40)
        ) as position:
            ui.QuotaWidget._close_context_menu(widget)
            ui.QuotaWidget._position_at_taskbar(widget)
        self.assertFalse(widget._context_menu_open)
        position.assert_called_once()

    def test_menu_command_wrapper_closes_menu_before_running_callback(self):
        widget = object.__new__(ui.QuotaWidget)
        widget._context_menu_open = True
        events = []

        def callback():
            events.append(widget._context_menu_open)

        wrapped = ui.QuotaWidget._menu_command(widget, callback)
        wrapped()

        self.assertEqual(events, [False])


@unittest.skipUnless(taskbar._IS_WINDOWS, "Windows taskbar selection only")
class TaskbarSelectionTests(unittest.TestCase):
    @staticmethod
    def rect(left, top, right, bottom):
        value = taskbar.RECT()
        value.left = left
        value.top = top
        value.right = right
        value.bottom = bottom
        return value

    def setUp(self):
        for target, name, value in (
            (taskbar, "_work_area", (0, 0, 2560, 1392)),
            (taskbar, "_native_obstacles", []),
            (taskbar._USER32, "IsWindowVisible", True),
            (taskbar._CONTROL_PROBE, "get", ((0, 1392, 600, 1440),)),
            (taskbar, "_configure_taskbar_child", 400),
        ):
            patch = mock.patch.object(target, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        coordinate_patch = mock.patch.object(taskbar, "_client_coordinates", side_effect=lambda _, x, y: (x, y))
        coordinate_patch.start()
        self.addCleanup(coordinate_patch.stop)
        self.last_good_patch = mock.patch.object(taskbar, "_LAST_GOOD_TASKBAR", None)
        self.log_state_patch = mock.patch.object(taskbar, "_LAST_TASKBAR_LOG_STATE", None)
        self.log_event_patch = mock.patch.object(taskbar.diagnostics, "log_event")
        self.last_good_patch.start()
        self.log_state_patch.start()
        self.log_event_patch.start()

    def tearDown(self):
        self.log_event_patch.stop()
        self.log_state_patch.stop()
        self.last_good_patch.stop()

    def test_primary_taskbar_prefers_main_taskbar_with_valid_tray(self):
        main_rect = self.rect(0, 1392, 2560, 1440)
        tray_rect = self.rect(2052, 1392, 2560, 1440)
        secondary_rect = self.rect(-2560, 1392, 0, 1440)

        with mock.patch.object(
            taskbar,
            "find_taskbars",
            return_value=[(100, main_rect), (200, secondary_rect)],
        ), mock.patch.object(
            taskbar,
            "_find_child_window",
            side_effect=lambda hwnd, _class: 300 if hwnd == 100 else None,
        ), mock.patch.object(
            taskbar, "_window_rect", side_effect=lambda hwnd: tray_rect if hwnd == 300 else None
        ):
            selected = taskbar.primary_taskbar()

        self.assertEqual(selected.hwnd, 100)
        self.assertEqual(selected.tray_left, 2052)

    def test_primary_taskbar_uses_last_good_tray_when_tray_temporarily_disappears(self):
        cached = taskbar.TaskbarInfo(
            hwnd=100,
            left=0,
            top=1392,
            right=2560,
            bottom=1440,
            tray_left=2052,
        )
        taskbar._LAST_GOOD_TASKBAR = cached
        main_rect = self.rect(0, 1392, 2560, 1440)
        secondary_rect = self.rect(-2560, 1392, 0, 1440)

        with mock.patch.object(
            taskbar,
            "find_taskbars",
            return_value=[(100, main_rect), (200, secondary_rect)],
        ), mock.patch.object(taskbar, "_find_child_window", return_value=None):
            selected = taskbar.primary_taskbar()

        self.assertIs(selected, cached)

    def test_secondary_tray_never_replaces_cached_primary_tray(self):
        cached = taskbar.TaskbarInfo(
            hwnd=100,
            left=0,
            top=1392,
            right=2560,
            bottom=1440,
            tray_left=2052,
        )
        taskbar._LAST_GOOD_TASKBAR = cached
        main_rect = self.rect(0, 1392, 2560, 1440)
        secondary_rect = self.rect(-2560, 1392, 0, 1440)
        secondary_tray_rect = self.rect(-500, 1392, 0, 1440)

        with mock.patch.object(
            taskbar,
            "find_taskbars",
            return_value=[(100, main_rect), (200, secondary_rect)],
        ), mock.patch.object(
            taskbar,
            "_find_child_window",
            side_effect=lambda hwnd, _class: 300 if hwnd == 200 else None,
        ), mock.patch.object(
            taskbar,
            "_window_rect",
            side_effect=lambda hwnd: secondary_tray_rect if hwnd == 300 else None,
        ):
            selected = taskbar.primary_taskbar()

        self.assertIs(selected, cached)

    def test_missing_primary_discards_cached_geometry(self):
        cached = taskbar.TaskbarInfo(
            hwnd=100,
            left=0,
            top=1392,
            right=2560,
            bottom=1440,
            tray_left=2052,
        )
        taskbar._LAST_GOOD_TASKBAR = cached
        with mock.patch.object(
            taskbar,
            "find_taskbars",
            return_value=[],
        ), mock.patch.object(taskbar, "_find_child_window", return_value=None):
            selected = taskbar.primary_taskbar()

        self.assertIsNone(selected)
        self.assertIsNone(taskbar._LAST_GOOD_TASKBAR)

    def test_resized_taskbar_does_not_reuse_old_tray_coordinates(self):
        taskbar._LAST_GOOD_TASKBAR = taskbar.TaskbarInfo(100, 0, 1392, 2560, 1440, 2052)
        with mock.patch.object(
            taskbar, "find_taskbars", return_value=[(100, self.rect(0, 720, 1280, 768))]
        ), mock.patch.object(taskbar, "_find_child_window", return_value=None):
            selected = taskbar.primary_taskbar()
        self.assertEqual((selected.right, selected.tray_left), (1280, 1280))

    def test_primary_taskbar_falls_back_to_shell_tray_when_no_cache_exists(self):
        main_rect = self.rect(0, 1392, 2560, 1440)
        secondary_rect = self.rect(-2560, 1392, 0, 1440)

        with mock.patch.object(
            taskbar,
            "find_taskbars",
            return_value=[(100, main_rect), (200, secondary_rect)],
        ), mock.patch.object(taskbar, "_find_child_window", return_value=None):
            selected = taskbar.primary_taskbar()

        self.assertEqual(selected.hwnd, 100)
        self.assertEqual(selected.tray_left, 2560)

    def test_repositioning_does_not_steal_z_order_from_system_flyouts(self):
        info = taskbar.TaskbarInfo(
            hwnd=100,
            left=0,
            top=1392,
            right=2560,
            bottom=1440,
            tray_left=2052,
        )

        with mock.patch.object(taskbar, "primary_taskbar", return_value=info), mock.patch.object(
            taskbar, "_configure_popup", return_value=(400, False)
        ), mock.patch.object(
            taskbar, "_is_above_in_z_order", return_value=True
        ), mock.patch.object(taskbar._USER32, "SetWindowPos") as set_window_pos:
            taskbar._place_popup(300, info, (1748, 1396, 2048, 1436))

        flags = set_window_pos.call_args.args[-1]
        self.assertTrue(flags & 0x0004, "SWP_NOZORDER must be set")

    def test_first_owner_binding_raises_widget_above_taskbar(self):
        info = taskbar.TaskbarInfo(
            hwnd=100,
            left=0,
            top=1392,
            right=2560,
            bottom=1440,
            tray_left=2052,
        )

        with mock.patch.object(taskbar, "primary_taskbar", return_value=info), mock.patch.object(
            taskbar, "_configure_popup", return_value=(400, True)
        ), mock.patch.object(
            taskbar, "_is_above_in_z_order", return_value=None
        ), mock.patch.object(taskbar._USER32, "SetWindowPos") as set_window_pos:
            taskbar._place_popup(300, info, (1748, 1396, 2048, 1436))

        flags = set_window_pos.call_args.args[-1]
        self.assertFalse(flags & 0x0004, "first binding must establish z-order")

    def test_repositioning_recovers_when_taskbar_is_above_widget(self):
        info = taskbar.TaskbarInfo(
            hwnd=100,
            left=0,
            top=1392,
            right=2560,
            bottom=1440,
            tray_left=2052,
        )

        with mock.patch.object(taskbar, "primary_taskbar", return_value=info), mock.patch.object(
            taskbar, "_configure_popup", return_value=(400, False)
        ), mock.patch.object(
            taskbar, "_is_above_in_z_order", return_value=False
        ), mock.patch.object(
            taskbar._USER32, "SetWindowPos"
        ) as set_window_pos, mock.patch.object(
            taskbar.diagnostics, "log_event"
        ) as log_event:
            taskbar._place_popup(300, info, (1748, 1396, 2048, 1436))

        self.assertEqual(
            set_window_pos.call_args.args[1].value,
            taskbar.wintypes.HWND(taskbar.HWND_TOPMOST).value,
        )
        flags = set_window_pos.call_args.args[-1]
        self.assertFalse(flags & taskbar.SWP_NOZORDER)
        log_event.assert_called_once_with(
            "taskbar_z_order_recovered", hwnd=400, taskbar_hwnd=100
        )

    def test_z_order_comparison_stops_at_first_matching_window(self):
        order = {500: 400, 400: 100, 100: 0}
        with mock.patch.object(taskbar._USER32, "GetTopWindow", return_value=500), mock.patch.object(
            taskbar._USER32,
            "GetWindow",
            side_effect=lambda hwnd, _command: order[int(hwnd)],
        ):
            self.assertTrue(taskbar._is_above_in_z_order(400, 100))
            self.assertFalse(taskbar._is_above_in_z_order(100, 400))

    def test_search_and_clock_cannot_hide_widget_through_explorer_owner(self):
        with mock.patch.object(taskbar, "_top_level_hwnd", return_value=400), mock.patch.object(
            taskbar, "_GET_WINDOW_LONG_PTR", side_effect=[taskbar.WS_EX_TOPMOST, 100]
        ), mock.patch.object(taskbar, "_SET_WINDOW_LONG_PTR") as set_style:
            self.assertEqual(taskbar._configure_popup(300), (400, True))
        self.assertIn(mock.call(400, taskbar.GWLP_HWNDPARENT, 0), set_style.call_args_list)
        style = set_style.call_args_list[0].args[2]
        self.assertTrue(style & taskbar.WS_EX_NOACTIVATE)
        self.assertTrue(style & taskbar.WS_EX_TOOLWINDOW)

    def test_hidden_window_is_shown_above_taskbar_again_without_activation(self):
        info = taskbar.TaskbarInfo(100, 0, 1392, 2560, 1440, 2052)
        with mock.patch.object(taskbar, "_configure_popup", return_value=(400, False)), mock.patch.object(
            taskbar, "_is_above_in_z_order", return_value=True
        ), mock.patch.object(taskbar._USER32, "IsWindowVisible", return_value=False), mock.patch.object(
            taskbar._USER32, "SetWindowPos"
        ) as place:
            taskbar._place_popup(300, info, (900, 1396, 1044, 1436))
        flags = place.call_args.args[-1]
        self.assertTrue(flags & taskbar.SWP_SHOWWINDOW)
        self.assertTrue(flags & taskbar.SWP_NOACTIVATE)
        self.assertFalse(flags & taskbar.SWP_NOZORDER)

    def test_crowded_taskbar_uses_compact_width_and_avoids_traffic_monitor(self):
        info = taskbar.TaskbarInfo(100, 0, 1392, 2560, 1440, 2052)
        with mock.patch.object(taskbar, "primary_taskbar", return_value=info), mock.patch.object(
            taskbar._CONTROL_PROBE, "get", return_value=((0, 1392, 1620, 1440),)
        ), mock.patch.object(taskbar, "_native_obstacles", return_value=[(1800, 1396, 2052, 1436)]), mock.patch.object(
            taskbar, "_configure_popup", return_value=(400, False)
        ), mock.patch.object(taskbar, "_is_above_in_z_order", return_value=True), mock.patch.object(
            taskbar._USER32, "SetWindowPos"
        ) as place:
            self.assertEqual(taskbar.position_taskbar_popup(300, 300, 40, compact_width=96), (96, 40))
        self.assertEqual(place.call_args.args[2:6], (1700, 1396, 96, 40))

    def test_popup_avoids_native_traffic_monitor(self):
        info = taskbar.TaskbarInfo(100, 0, 1392, 2560, 1440, 2052)
        with mock.patch.object(taskbar, "primary_taskbar", return_value=info), mock.patch.object(
            taskbar, "_native_obstacles", return_value=[(1800, 1396, 2052, 1436)]
        ), mock.patch.object(taskbar, "_configure_popup", return_value=(400, False)), mock.patch.object(
            taskbar, "_is_above_in_z_order", return_value=True
        ), mock.patch.object(taskbar._USER32, "SetWindowPos") as position:
            self.assertEqual(taskbar.position_taskbar_popup(300, 300, 40), (300, 40))
        self.assertEqual(position.call_args.args[2:6], (1496, 1396, 300, 40))

    def test_incomplete_accessibility_tree_does_not_cover_unknown_controls(self):
        info = taskbar.TaskbarInfo(100, 0, 1392, 2560, 1440, 2052)
        for controls in (None, (), ((2052, 1392, 2560, 1440),), ((500, 100, 800, 200),)):
            with self.subTest(controls=controls), mock.patch.object(
                taskbar, "primary_taskbar", return_value=info
            ), mock.patch.object(taskbar._CONTROL_PROBE, "get", return_value=controls), mock.patch.object(
                taskbar, "_configure_popup", return_value=(400, False)
            ), mock.patch.object(taskbar, "_is_above_in_z_order", return_value=True), mock.patch.object(
                taskbar._USER32, "SetWindowPos"
            ) as position:
                taskbar.position_taskbar_popup(300, 300, 40)
            position.assert_not_called()


@unittest.skipUnless(taskbar._IS_WINDOWS, "Win32 obstacle enumeration only")
class TaskbarNativeObstacleTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(taskbar, "_NATIVE_OBSTACLE_WINDOWS", {})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(taskbar, "_NATIVE_OBSTACLE_KEY", None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_shell_promotion_does_not_turn_a_live_monitor_into_free_space(self):
        info = taskbar.TaskbarInfo(100, 0, 720, 1280, 768, 1000)
        rect = TaskbarSelectionTests.rect(800, 724, 1000, 764)
        pid = 123
        def process_id(hwnd, target):
            target._obj.value = pid
        with mock.patch.object(taskbar, "_child_windows", return_value=[]), mock.patch.object(
            taskbar._USER32, "EnumWindows", side_effect=lambda cb, lp: cb(200, lp)
        ) as enumerate_windows, mock.patch.object(taskbar._USER32, "IsWindowVisible", return_value=True) as visible, mock.patch.object(
            taskbar._USER32, "GetWindowThreadProcessId", side_effect=process_id
        ), mock.patch.object(taskbar, "_window_class", return_value="TrafficMonitorWindow"), mock.patch.object(
            taskbar, "_window_rect", return_value=rect
        ):
            self.assertEqual(taskbar._native_obstacles(info), [(800, 724, 1000, 764)])
            enumerate_windows.side_effect = None  # Start/Search now omit the window.
            rect.left = 750
            self.assertEqual(taskbar._native_obstacles(info), [(750, 724, 1000, 764)])
            pid = 456  # HWND was reused; old identity must not remain an obstacle.
            self.assertEqual(taskbar._native_obstacles(info), [])
            pid = 123
            enumerate_windows.side_effect = lambda cb, lp: cb(200, lp)
            self.assertTrue(taskbar._native_obstacles(info))
            enumerate_windows.side_effect = None
            visible.return_value = False
            self.assertTrue(taskbar._native_obstacles(info))  # Shell temporarily hides owned popups.
            enumerate_windows.side_effect = lambda cb, lp: cb(100, lp)
            self.assertEqual(taskbar._native_obstacles(info), [])

    def test_explorer_replacement_invalidates_known_monitor_handles(self):
        taskbar._NATIVE_OBSTACLE_KEY = (100, (0, 720, 1280, 768))
        taskbar._NATIVE_OBSTACLE_WINDOWS[200] = (123, "TrafficMonitorWindow")
        info = taskbar.TaskbarInfo(300, 0, 720, 1280, 768, 1000)
        with mock.patch.object(taskbar._USER32, "EnumWindows"), mock.patch.object(
            taskbar, "_child_windows", return_value=[]
        ), mock.patch.object(taskbar, "_window_rect") as rectangle:
            self.assertEqual(taskbar._native_obstacles(info), [])
            rectangle.assert_not_called()

    def test_custom_children_and_floating_monitors_count_but_own_window_does_not(self):
        info = taskbar.TaskbarInfo(100, 0, 720, 1280, 768, 1000)
        rects = {
            1: (100, 720, 1000, 768),  # empty task-list container
            2: (800, 724, 1000, 764),  # Traffic Monitor custom-drawn child
            3: (400, 724, 700, 764),   # our owned popup
            4: (600, 724, 790, 764),   # other top-level taskbar monitor
            5: (0, 0, 1280, 768),     # maximized application
            6: (710, 724, 790, 764),   # hidden monitor
        }
        def process_id(hwnd, target):
            target._obj.value = os.getpid() if hwnd == 3 else 123
        with mock.patch.object(taskbar, "_child_windows", return_value=[1, 2, 6]), mock.patch.object(
            taskbar._USER32, "EnumWindows", side_effect=lambda cb, lp: [cb(h, lp) for h in (100, 3, 4, 5)]
        ), mock.patch.object(taskbar._USER32, "IsWindowVisible", side_effect=lambda h: h != 6), mock.patch.object(
            taskbar._USER32, "GetWindowThreadProcessId", side_effect=process_id
        ), mock.patch.object(taskbar, "_window_class", side_effect=lambda h: "MSTaskListWClass" if h == 1 else "#32770"), mock.patch.object(
            taskbar, "_window_rect", side_effect=lambda h: TaskbarSelectionTests.rect(*rects[h])
        ):
            self.assertEqual(taskbar._native_obstacles(info), [rects[2], rects[4]])


class ShellIntegrationTests(unittest.TestCase):
    def test_taskbar_children_remain_discoverable_when_enumeration_omits_them(self):
        children = {100: [200, 300], 200: [400], 300: [], 400: []}
        def find(parent, previous, *_):
            siblings = children[parent]
            index = siblings.index(previous) + 1 if previous else 0
            return siblings[index] if index < len(siblings) else 0
        with mock.patch.object(taskbar._USER32, "FindWindowExW", side_effect=find), mock.patch.object(
            taskbar._USER32, "EnumChildWindows"
        ) as enumerate_children:
            self.assertEqual(taskbar._child_windows(100), [200, 300, 400])
            enumerate_children.assert_not_called()

    def test_primary_taskbar_lookup_survives_empty_desktop_window_enumeration(self):
        rect = TaskbarSelectionTests.rect(0, 720, 1280, 768)
        with mock.patch.object(taskbar._USER32, "FindWindowW", return_value=100) as find, mock.patch.object(
            taskbar._USER32, "EnumWindows"
        ) as enumerate_windows, mock.patch.object(taskbar, "_window_rect", return_value=rect):
            self.assertEqual(taskbar.find_taskbars(), [(100, rect)])
            find.assert_called_once_with("Shell_TrayWnd", None)
            enumerate_windows.assert_not_called()

    def test_missing_shell_does_not_reuse_destroyed_window(self):
        with mock.patch.object(taskbar._USER32, "FindWindowW", return_value=0):
            self.assertEqual(taskbar.find_taskbars(), [])

    def test_embedded_bar_uses_taskbar_client_coordinates_without_activation(self):
        info = taskbar.TaskbarInfo(100, -1920, 1040, 0, 1080, -400)
        def convert(_parent, point):
            point._obj.x += 1920
            point._obj.y -= 1040
            return True
        with mock.patch.object(taskbar, "_configure_taskbar_child", return_value=400), mock.patch.object(
            taskbar._USER32, "ScreenToClient", side_effect=convert
        ), mock.patch.object(taskbar._USER32, "SetWindowPos", return_value=True) as place:
            self.assertEqual(taskbar._place_taskbar_child(300, info, (-704, 1044, -404, 1076)), (300, 32))
            self.assertEqual(place.call_args.args[2:6], (1216, 4, 300, 32))
            self.assertEqual(place.call_args.args[1].value, None)  # HWND_TOP among siblings
            self.assertTrue(place.call_args.args[-1] & taskbar.SWP_NOACTIVATE)
            self.assertTrue(place.call_args.args[-1] & taskbar.SWP_SHOWWINDOW)

    def test_embedding_uses_child_parent_not_explorer_popup_owner(self):
        with mock.patch.object(taskbar, "_top_level_hwnd", return_value=400), mock.patch.object(
            taskbar, "_GET_WINDOW_LONG_PTR", side_effect=[taskbar.WS_POPUP, taskbar.WS_EX_TOOLWINDOW]
        ), mock.patch.object(taskbar, "_SET_WINDOW_LONG_PTR") as style, mock.patch.object(
            taskbar._USER32, "GetParent", side_effect=[0, 100]
        ), mock.patch.object(taskbar._USER32, "SetParent") as parent:
            self.assertEqual(taskbar._configure_taskbar_child(300, 100), 400)
            parent.assert_called_once_with(400, 100)
            self.assertIn(mock.call(400, taskbar.GWLP_HWNDPARENT, 0), style.call_args_list)
            self.assertIn(mock.call(400, taskbar.GWL_STYLE, taskbar.WS_CHILD), style.call_args_list)

    def test_failed_embedding_never_places_bar_over_desktop_windows(self):
        info = taskbar.TaskbarInfo(100, 0, 720, 1280, 768, 1000)
        with mock.patch.object(taskbar, "_configure_taskbar_child", return_value=None), mock.patch.object(
            taskbar, "_client_coordinates", return_value=(0, 4)
        ), mock.patch.object(taskbar._USER32, "SetWindowPos") as place:
            self.assertIsNone(taskbar._place_taskbar_child(300, info, (0, 724, 300, 764)))
            place.assert_not_called()

    def test_withdrawn_widget_establishes_topmost_band_before_shell_attachment(self):
        operations = []
        with mock.patch.object(taskbar, "_top_level_hwnd", return_value=400), mock.patch.object(
            taskbar, "_GET_WINDOW_LONG_PTR", side_effect=[taskbar.WS_POPUP, 0]
        ), mock.patch.object(taskbar, "_SET_WINDOW_LONG_PTR"), mock.patch.object(
            taskbar._USER32, "GetParent", side_effect=[0, 100]
        ), mock.patch.object(taskbar._USER32, "SetWindowPos", side_effect=lambda *args: operations.append(("band", args)) or True), mock.patch.object(
            taskbar._USER32, "SetParent", side_effect=lambda *args: operations.append(("parent", args))
        ):
            self.assertEqual(taskbar._configure_taskbar_child(300, 100), 400)
        self.assertEqual([item[0] for item in operations], ["band", "parent"])
        self.assertEqual(operations[0][1][1].value, taskbar.wintypes.HWND(taskbar.HWND_TOPMOST).value)
        self.assertTrue(operations[0][1][-1] & taskbar.SWP_NOACTIVATE)

    def test_repositioning_existing_child_does_not_clear_its_shell_parent(self):
        with mock.patch.object(taskbar, "_top_level_hwnd", return_value=400), mock.patch.object(
            taskbar, "_GET_WINDOW_LONG_PTR", side_effect=[taskbar.WS_CHILD, taskbar.WS_EX_NOACTIVATE]
        ), mock.patch.object(taskbar, "_SET_WINDOW_LONG_PTR") as style, mock.patch.object(
            taskbar._USER32, "GetParent", return_value=100
        ), mock.patch.object(taskbar._USER32, "SetParent") as parent, mock.patch.object(
            taskbar._USER32, "SetWindowPos"
        ) as position:
            self.assertEqual(taskbar._configure_taskbar_child(300, 100), 400)
            parent.assert_not_called()
            position.assert_not_called()
            self.assertNotIn(mock.call(400, taskbar.GWLP_HWNDPARENT, 0), style.call_args_list)

    def test_destroyed_native_widget_requests_rebuild_without_reusing_tk_path(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.root = mock.Mock()
        widget.root.winfo_id.return_value = 300
        widget._closed = False
        widget._position_at_taskbar = mock.Mock()
        with mock.patch.object(taskbar, "window_exists", return_value=False):
            widget._tick()
        self.assertTrue(widget._restart_requested)
        widget.root.quit.assert_called_once()
        widget._position_at_taskbar.assert_not_called()
        widget.root.after.assert_not_called()

    def test_explorer_recovery_keeps_single_instance_mutex_until_final_exit(self):
        first, second = mock.Mock(), mock.Mock()
        first._restart_requested = True
        second._restart_requested = False
        with mock.patch.object(app, "acquire_single_instance", return_value=123) as acquire, mock.patch.object(
            app, "release_single_instance"
        ) as release, mock.patch.object(app.dpi, "enable_high_dpi"), mock.patch.object(
            app.tk, "Tk", side_effect=[first_root := mock.Mock(), second_root := mock.Mock()]
        ), mock.patch.object(app, "QuotaWidget", side_effect=[first, second]):
            app.main([])
            acquire.assert_called_once()
            release.assert_called_once_with(123)
        first.close.assert_called_once()
        second.close.assert_not_called()
        first_root.mainloop.assert_called_once()
        second_root.mainloop.assert_called_once()

    def test_recovery_cancels_periodic_and_menu_callbacks_before_destroying_tk(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.root = mock.Mock()
        widget.root.tk.splitlist.return_value = ("poll-tray", "tick", "menu-fallback")
        widget._closed = False
        widget._refresh_generation = 0
        widget._refresh_after_id = None
        widget._cancel_hover_timer = mock.Mock()
        widget._hide_hover = mock.Mock()
        widget.close()
        self.assertEqual(widget.root.after_cancel.call_args_list, [mock.call("poll-tray"), mock.call("tick"), mock.call("menu-fallback")])
        widget.root.destroy.assert_called_once()


class TaskbarLayoutTests(unittest.TestCase):
    def setUp(self):
        self.info = taskbar.TaskbarInfo(100, 0, 720, 1280, 768, 1000)
        self.work = (0, 0, 1280, 720)

    def place(self, occupied, width=300):
        return taskbar.taskbar_bounds(self.info, width, 40, occupied)

    def test_full_widget_fits_to_right_of_applications(self):
        self.assertEqual(self.place([(0, 720, 600, 768)]), (696, 724, 996, 764))

    def test_small_screen_collapses_and_restores_full_width_when_space_recovers(self):
        crowded = self.place([(0, 720, 800, 768)])
        self.assertIsNone(crowded)
        self.assertEqual(self.place([(0, 720, 800, 768)], width=96), (900, 724, 996, 764))
        self.assertEqual(self.place([(0, 720, 600, 768)])[1], 724)

    def test_traffic_monitor_near_tray_is_avoided(self):
        self.assertEqual(
            self.place([(0, 720, 300, 768), (800, 724, 1000, 764)]),
            (496, 724, 796, 764),
        )

    def test_centered_apps_can_use_gap_on_left(self):
        self.assertEqual(
            self.place([(0, 720, 100, 768), (450, 720, 1000, 768)]),
            (146, 724, 446, 764),
        )

    def test_fragmented_space_is_not_added_together(self):
        self.assertIsNone(self.place([(250, 720, 350, 768), (600, 720, 700, 768)]))

    def test_overlapping_obstacles_cannot_create_false_gap(self):
        self.assertIsNone(self.place([(0, 720, 850, 768), (200, 720, 500, 768)]))

    def test_unknown_layout_is_not_treated_as_empty(self):
        self.assertIsNone(self.place(None))

    def test_popup_above_taskbar_does_not_count_as_taskbar_obstacle(self):
        self.assertEqual(self.place([(0, 0, 1280, 720)])[1], 724)

    def test_high_width_setting_is_clamped_to_screen_in_floating_mode(self):
        self.assertEqual(taskbar.popup_bounds(self.info, 1500, 40, None, self.work), (4, 676, 1276, 716))

    def test_negative_monitor_coordinates_stay_on_same_monitor(self):
        info = taskbar.TaskbarInfo(100, -1280, 720, 0, 768, -200)
        result = taskbar.popup_bounds(info, 300, 40, None, (-1280, 0, 0, 720))
        self.assertEqual(result, (-504, 676, -204, 716))

    def test_top_taskbar_floats_below(self):
        info = taskbar.TaskbarInfo(100, 0, 0, 1280, 48, 1000)
        self.assertEqual(taskbar.popup_bounds(info, 300, 40, None, (0, 48, 1280, 768)), (696, 52, 996, 92))

    def test_vertical_taskbar_floats_on_desktop_side(self):
        info = taskbar.TaskbarInfo(100, 0, 0, 48, 768, 0)
        self.assertEqual(taskbar.popup_bounds(info, 300, 40, [], (48, 0, 1280, 768)), (52, 724, 352, 764))

    def test_dpi_mapping_rounds_outwards_with_negative_origin(self):
        self.assertEqual(taskbar_accessibility.map_bounds(
            (-1601, 1086, -1200, 1140), (-1920, 1080, 0, 1152), (-1280, 720, 0, 768)
        ), (-1068, 724, -800, 760))


class CompactWidgetTests(unittest.TestCase):
    def widget(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.settings = dict(config.DEFAULTS)
        widget._dpi_scale = 1.5
        widget.W, widget.H, widget.desired_H = 144, 60, 60
        widget._collapsed = True
        widget._closed = False
        widget._context_menu_open = False
        widget._status_code = None
        widget._hover_window = None
        widget._hover_visible = False
        widget._hover_show_id = widget._hover_hide_id = None
        widget._tray_entry = None
        widget._tray_mode = False
        widget.root = mock.Mock()
        widget.root.winfo_id.return_value = 123
        widget.root.state.return_value = "normal"
        widget.canvas = mock.Mock()
        widget.f_row_label = widget.f_row_pct = widget.f_row_reset = widget.f_row_stat = None
        widget.data = {"rows": {"h": {"remain": 100}, "w": {"remain": 76}},
                       "usage": {"tokens": "6.8M", "cost": "$1.23"}}
        return widget

    def test_compact_draws_only_two_labels_and_quota_percentages_at_full_font_size(self):
        widget = self.widget()
        widget._redraw()
        calls = widget.canvas.create_text.call_args_list
        self.assertEqual([call.kwargs["text"] for call in calls], ["5h", "100%", "每周", "76%"])
        self.assertEqual(calls[1].kwargs["anchor"], "e")
        self.assertEqual(widget._scale(), 1.0)

    def test_compact_and_full_views_follow_available_width_without_changing_preferences(self):
        widget = self.widget()
        widget._build_ui = mock.Mock()
        with mock.patch.object(taskbar, "primary_scale", return_value=1.5), mock.patch.object(
            taskbar, "position_taskbar_popup", side_effect=[(144, 60), (450, 60)]
        ) as place:
            widget._position_at_taskbar()
            self.assertTrue(widget._collapsed)
            widget._position_at_taskbar()
        self.assertFalse(widget._collapsed)
        self.assertEqual(widget.W, 450)
        self.assertEqual([call.kwargs["compact_width"] for call in place.call_args_list], [144, 144])
        self.assertEqual(widget.settings, config.DEFAULTS)

    def test_no_safe_gap_uses_tray_and_restores_bar_when_space_returns(self):
        widget = self.widget()
        widget._build_ui = mock.Mock()
        with mock.patch.object(taskbar, "primary_scale", return_value=1.5), mock.patch.object(
            taskbar, "position_taskbar_popup", side_effect=[None, (144, 60)]
        ), mock.patch.object(ui.tray, "TrayEntry") as entry:
            widget._position_at_taskbar()
            self.assertTrue(widget._tray_mode)
            widget.root.withdraw.assert_called_once()
            entry.return_value.show.assert_called_once_with("CodexBar 5h 100% / 每周 76%")
            widget._position_at_taskbar()
        self.assertFalse(widget._tray_mode)
        entry.return_value.hide.assert_called_once()

    def test_hover_waits_then_opens_and_leaving_cancels_pending_open(self):
        widget = self.widget()
        widget.root.after.return_value = "timer"
        widget._enter_bar()
        widget.root.after.assert_called_once_with(150, widget._show_hover)
        widget._leave_bar()
        widget.root.after_cancel.assert_called_with("timer")
        self.assertEqual(widget.root.after.call_args.args[0], 250)

    def test_moving_into_details_keeps_popup_and_leaving_both_hides_it(self):
        widget = self.widget()
        widget._hover_visible = True
        widget._hover_window = mock.Mock()
        widget._hover_window.winfo_id.return_value = 456
        widget.root.winfo_pointerxy.return_value = (900, 680)
        with mock.patch.object(taskbar, "window_bounds", side_effect=[
            (900, 724, 996, 764), (696, 676, 996, 716),
            (900, 724, 996, 764), (696, 676, 996, 716),
        ]):
            widget._check_hover_leave()
            self.assertTrue(widget._hover_visible)
            widget.root.winfo_pointerxy.return_value = (300, 300)
            widget._check_hover_leave()
        self.assertFalse(widget._hover_visible)
        widget._hover_window.withdraw.assert_called_once()

    def test_detail_window_uses_full_configured_width_without_resizing_compact_bar(self):
        widget = self.widget()
        widget._hover_window = mock.Mock()
        widget._hover_window.winfo_id.return_value = 456
        widget._hover_canvas = mock.Mock()
        with mock.patch.object(taskbar, "window_bounds", return_value=(900, 724, 1044, 784)), mock.patch.object(
            taskbar, "position_hover_popup", return_value=(450, 60)
        ) as place:
            self.assertTrue(widget._position_hover())
        self.assertEqual(place.call_args.args, (456, (900, 724, 1044, 784), 450, 60))
        self.assertEqual((widget.W, widget.H), (144, 60))

    def test_native_tray_notifications_are_queued_for_the_tk_thread(self):
        if not taskbar._IS_WINDOWS:
            self.skipTest("Win32 tray messages only")
        entry = object.__new__(ui.tray.TrayEntry)
        entry.events, entry.active, entry._restart = [], True, 0xCAFE
        for code in (0x406, 0x407, 0x400, 0x7B, 0x203):
            entry._message(123, 0x8001, 0, (1 << 16) | code)
        self.assertEqual(entry.events, ["hover", "leave", "click", "menu", "settings"])


class TaskbarProbeTests(unittest.TestCase):
    def test_clock_menu_tree_keeps_known_slots_until_real_controls_return(self):
        probe = taskbar_accessibility.TaskbarProbe()
        key = (100, (0, 720, 1280, 768))
        controls = ((0, 720, 600, 768),)
        with mock.patch.object(taskbar_accessibility, "read_controls", return_value=controls):
            probe._update(key, 10)
        with mock.patch.object(taskbar_accessibility, "read_controls", side_effect=taskbar_accessibility.TaskbarMenuOpen):
            probe._update(key, 15)
        with mock.patch.object(taskbar_accessibility.time, "monotonic", return_value=15.5):
            self.assertEqual(probe.get(*key), controls)
        updated = ((0, 720, 700, 768),)
        with mock.patch.object(taskbar_accessibility, "read_controls", return_value=updated):
            probe._update(key, 16)
        self.assertEqual(probe._result, updated)

    def test_menu_does_not_create_a_slot_without_known_matching_taskbar_geometry(self):
        probe = taskbar_accessibility.TaskbarProbe()
        key = (100, (0, 720, 1280, 768))
        with mock.patch.object(taskbar_accessibility, "read_controls", side_effect=taskbar_accessibility.TaskbarMenuOpen):
            probe._update(key, 10)
            self.assertIsNone(probe._result)
            probe._result = ((0, 720, 600, 768),)
            probe._update((200, key[1]), 11)
            self.assertIsNone(probe._result)

    def test_only_off_taskbar_menu_items_are_classified_as_a_menu_tree(self):
        bounds = (0, 720, 1280, 768)
        menu = [(1000, 600, 1250, 710)]
        self.assertTrue(taskbar_accessibility._menu_only_snapshot(menu, bounds, True))
        self.assertFalse(taskbar_accessibility._menu_only_snapshot(menu, bounds, False))
        self.assertFalse(taskbar_accessibility._menu_only_snapshot([*menu, (0, 720, 600, 768)], bounds, True))

    def test_first_read_does_not_block_and_busy_probe_does_not_spawn_more_threads(self):
        probe = taskbar_accessibility.TaskbarProbe()
        with mock.patch.object(taskbar_accessibility.threading, "Thread") as thread:
            self.assertIsNone(probe.get(100, (0, 720, 1280, 768)))
            self.assertIsNone(probe.get(100, (0, 720, 1280, 768)))
            thread.assert_called_once()

    def test_stale_or_different_taskbar_snapshot_is_never_used(self):
        probe = taskbar_accessibility.TaskbarProbe()
        bounds = (0, 720, 1280, 768)
        with mock.patch.object(taskbar_accessibility, "read_controls", return_value=((0, 720, 600, 768),)):
            probe._update((100, bounds), 10)
        with mock.patch.object(taskbar_accessibility.time, "monotonic", return_value=10.5), mock.patch.object(
            taskbar_accessibility.threading, "Thread"
        ):
            self.assertIsNotNone(probe.get(100, bounds))
            self.assertIsNone(probe.get(200, bounds))
            self.assertIsNone(probe.get(100, (0, 720, 1400, 768)))
        with mock.patch.object(taskbar_accessibility.time, "monotonic", return_value=13):
            self.assertIsNone(probe.get(100, bounds))

    def test_failed_probe_discards_previous_result_and_can_retry(self):
        probe = taskbar_accessibility.TaskbarProbe()
        probe._result = ((0, 720, 600, 768),)
        with mock.patch.object(taskbar_accessibility, "read_controls", side_effect=OSError("Explorer restarting")), mock.patch.object(
            taskbar_accessibility.diagnostics, "log_exception"
        ):
            probe._update((100, (0, 720, 1280, 768)), 10)
        self.assertIsNone(probe._result)
        self.assertFalse(probe._busy)


class DpiRenderingTests(unittest.TestCase):
    def test_native_dpi_is_enabled_before_creating_tk(self):
        calls = []
        with mock.patch.object(app, "acquire_single_instance", return_value=123), mock.patch.object(
            app, "release_single_instance"
        ), mock.patch.object(app.dpi, "enable_high_dpi", side_effect=lambda: calls.append("dpi")), mock.patch.object(
            app.tk, "Tk", side_effect=lambda: calls.append("tk") or mock.Mock()
        ), mock.patch.object(app, "QuotaWidget"):
            app.main([])
        self.assertEqual(calls, ["dpi", "tk"])

    def test_common_display_scales_use_device_pixel_fonts_and_preserve_size(self):
        for factor, width, height, text_pixels in (
            (1, 300, 40, 11), (1.25, 375, 50, 13),
            (1.5, 450, 60, 16), (2, 600, 80, 21),
        ):
            with self.subTest(factor=factor):
                widget = object.__new__(ui.QuotaWidget)
                widget.settings = dict(config.DEFAULTS)
                widget._dpi_scale = factor
                widget.W, widget.H, widget.desired_H = width, height, height
                widget.canvas = mock.Mock()
                widget._redraw = mock.Mock()
                with mock.patch.object(ui.tkfont, "Font") as font:
                    widget._build_ui()
                self.assertEqual(widget._scale(), 1.0)
                self.assertEqual(font.call_args_list[0].kwargs["size"], -text_pixels)
                self.assertEqual(widget._px(300), width)

    def test_primary_monitor_dpi_change_rebuilds_without_changing_saved_settings(self):
        widget = object.__new__(ui.QuotaWidget)
        widget._context_menu_open = False
        widget.settings = dict(config.DEFAULTS)
        widget.root = mock.Mock()
        widget.root.winfo_id.return_value = 123
        widget.W, widget.H, widget.desired_H = 300, 40, 40
        widget._dpi_scale = 1.0
        widget._build_ui = mock.Mock()
        with mock.patch.object(taskbar, "primary_scale", side_effect=[1.5, 2, 1]), mock.patch.object(
            taskbar, "position_taskbar_popup", side_effect=[(450, 60), (600, 80), (300, 40)]
        ) as place:
            for _ in range(3):
                widget._position_at_taskbar()
        self.assertEqual([call.args for call in place.call_args_list], [(123, 450, 60), (123, 600, 80), (123, 300, 40)])
        self.assertEqual([call.kwargs["margin"] for call in place.call_args_list], [6, 8, 4])
        self.assertEqual(widget._build_ui.call_count, 3)
        self.assertEqual(widget.settings, config.DEFAULTS)
        self.assertEqual((widget.W, widget.H), (300, 40))

    def test_high_dpi_text_columns_scale_with_the_font(self):
        widget = object.__new__(ui.QuotaWidget)
        widget.settings = dict(config.DEFAULTS)
        widget._dpi_scale = 2.0
        widget.W, widget.H = 600, 80
        widget._status_code = None
        widget.data = {}
        widget.canvas = mock.Mock()
        widget.f_row_label = widget.f_row_pct = widget.f_row_reset = widget.f_row_stat = None
        widget._redraw()
        self.assertEqual([call.args[0] for call in widget.canvas.create_text.call_args_list[:4]], [18, 86, 164, 580])

    def test_dialog_embedded_control_width_scales_with_canvas_coordinates(self):
        canvas = mock.Mock()
        canvas.find_all.return_value = (1, 2)
        canvas.type.side_effect = ("text", "window")
        dimensions = {"width": "292", "height": "0"}
        canvas.itemcget.side_effect = lambda _item, key: dimensions[key]
        canvas.scale.side_effect = lambda *args: dimensions.update(width="438")
        ui.scale_canvas_layout(canvas, 1.5)
        canvas.scale.assert_called_once_with("all", 0, 0, 1.5, 1.5)
        canvas.itemconfigure.assert_called_once_with(2, width=438)


class TokenUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = self.temp.name
        for name, filename in (("OFFICIAL_PRICES_PATH", "official_model_prices.json"), ("MODEL_PRICES_PATH", "model_prices.json")):
            patch = mock.patch.object(config, name, os.path.join(self.home, filename))
            patch.start()
            self.addCleanup(patch.stop)
        self.rollout = os.path.join(self.home, "rollout.jsonl")
        self.db = os.path.join(self.home, "state_5.sqlite")
        db = sqlite3.connect(self.db)
        db.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT, model TEXT, updated_at_ms INTEGER, "
            "title TEXT, first_user_message TEXT, preview TEXT, cwd TEXT)"
        )
        db.execute(
            "INSERT INTO threads "
            "(id, rollout_path, model, updated_at_ms, title, first_user_message, preview, cwd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "t1",
                self.rollout,
                "gpt-5.4-mini",
                200,
                "CodexBar WebView 改造",
                "first message",
                "preview",
                r"C:\Work\CodexBar",
            ),
        )
        db.execute(
            "INSERT INTO threads "
            "(id, rollout_path, model, updated_at_ms, title, first_user_message, preview, cwd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("t2", self.rollout, "gpt-5.4-mini", 100, "", "", "", ""),
        )
        db.commit()
        db.close()
        with open(self.rollout, "w", encoding="utf-8") as file:
            file.write('{"type":"turn_context","payload":{"turn_id":"turn-1"}}\n')
            file.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-02T00:50:00Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "please change the UI"},
                    }
                )
                + "\n"
            )
            file.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-02T01:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 1000,
                                    "cached_input_tokens": 400,
                                    "output_tokens": 100,
                                    "reasoning_output_tokens": 25,
                                    "total_tokens": 1100,
                                },
                                "total_token_usage": {
                                    "input_tokens": 1000,
                                    "cached_input_tokens": 400,
                                    "output_tokens": 100,
                                    "reasoning_output_tokens": 25,
                                    "total_tokens": 1100,
                                },
                                "model_context_window": 128000,
                            },
                        },
                    }
                )
                + "\n"
            )
            file.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-01T12:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 999,
                                    "output_tokens": 999,
                                    "total_tokens": 1998,
                                }
                            },
                        },
                    }
                )
                + "\n"
            )

    def tearDown(self):
        token_usage.clear_usage_cache()
        self.temp.cleanup()

    def _append_token_event(
        self,
        timestamp: str,
        total_tokens: int,
        input_tokens: int = 1000,
        cached_input_tokens: int = 0,
        output_tokens: int = 100,
        reasoning_output_tokens: int = 0,
        rollout_path: str | None = None,
        cumulative_usage: dict[str, int] | None = None,
        model_context_window: int = 128000,
    ) -> None:
        with open(rollout_path or self.rollout, "a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "timestamp": timestamp,
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": input_tokens,
                                    "cached_input_tokens": cached_input_tokens,
                                    "output_tokens": output_tokens,
                                    "reasoning_output_tokens": reasoning_output_tokens,
                                    "total_tokens": total_tokens,
                                },
                                **(
                                    {"total_token_usage": cumulative_usage}
                                    if cumulative_usage is not None
                                    else {}
                                ),
                                "model_context_window": model_context_window,
                            },
                        },
                    }
                )
                + "\n"
            )

    def _append_turn_context(self, model: str) -> None:
        with open(self.rollout, "a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-02T00:00:00Z",
                        "type": "turn_context",
                        "payload": {"turn_id": f"turn-{model}", "model": model},
                    }
                )
                + "\n"
            )

    def _replace_rollout(self) -> None:
        with open(self.rollout, "w", encoding="utf-8"):
            pass

    def test_duplicate_cumulative_token_event_is_counted_once(self):
        self._replace_rollout()
        self._append_turn_context("gpt-5.4-mini")
        cumulative = {
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "output_tokens": 10,
            "reasoning_output_tokens": 2,
            "total_tokens": 110,
        }
        for timestamp in ("2026-07-02T01:00:00Z", "2026-07-02T01:01:00Z"):
            self._append_token_event(
                timestamp,
                110,
                input_tokens=100,
                cached_input_tokens=20,
                output_tokens=10,
                reasoning_output_tokens=2,
                cumulative_usage=cumulative,
            )

        result = token_usage.collect_today_usage(
            codex_home=self.home,
            today=dt.date(2026, 7, 2),
            use_cache=False,
        )

        self.assertEqual(result["events"], 1)
        self.assertEqual(result["total_tokens"], 110)

    def test_replayed_parent_history_is_attributed_to_its_original_day_once(self):
        self._replace_rollout()
        copied_rollout = os.path.join(self.home, "copied-child-rollout.jsonl")
        Path(copied_rollout).touch()
        db = sqlite3.connect(self.db)
        db.execute(
            "INSERT INTO threads "
            "(id, rollout_path, model, updated_at_ms, title, first_user_message, preview, cwd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("child", copied_rollout, "gpt-5.4-mini", 300, "child", "", "", ""),
        )
        db.commit()
        db.close()
        cumulative = {
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 10,
            "reasoning_output_tokens": 2,
            "total_tokens": 110,
        }
        self._append_token_event(
            "2026-07-01T01:00:00Z",
            110,
            input_tokens=100,
            cached_input_tokens=80,
            output_tokens=10,
            reasoning_output_tokens=2,
            cumulative_usage=cumulative,
        )
        self._append_token_event(
            "2026-07-02T01:00:00Z",
            110,
            input_tokens=100,
            cached_input_tokens=80,
            output_tokens=10,
            reasoning_output_tokens=2,
            rollout_path=copied_rollout,
            cumulative_usage=cumulative,
        )
        with open(copied_rollout, "r+", encoding="utf-8") as file:
            copied_event = file.read()
            file.seek(0)
            file.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-02T00:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "child",
                            "session_id": "parent",
                            "forked_from_id": "parent",
                        },
                    }
                )
                + "\n"
                + copied_event
                + json.dumps(
                    {
                        "timestamp": "2026-07-02T01:01:00Z",
                        "type": "event_msg",
                        "payload": {"type": "thread_settings_applied"},
                    }
                )
                + "\n"
            )
            file.truncate()

        original_day = token_usage.collect_today_usage(
            codex_home=self.home,
            today=dt.date(2026, 7, 1),
            use_cache=False,
        )
        replay_day = token_usage.collect_today_usage(
            codex_home=self.home,
            today=dt.date(2026, 7, 2),
            use_cache=False,
        )
        daily_range = token_usage.collect_usage_days(
            days=2,
            codex_home=self.home,
            anchor_date=dt.date(2026, 7, 2),
            use_cache=False,
        )
        replay_threads = token_usage.collect_thread_usage_for_day(
            dt.date(2026, 7, 2),
            codex_home=self.home,
        )

        self.assertEqual(original_day["total_tokens"], 110)
        self.assertEqual(original_day["events"], 1)
        self.assertEqual(replay_day["total_tokens"], 0)
        self.assertEqual(replay_day["events"], 0)
        self.assertEqual(daily_range["total_tokens"], 110)
        self.assertEqual(
            [bucket["total_tokens"] for bucket in daily_range["buckets"]],
            [110, 0],
        )
        self.assertEqual(replay_threads, [])

    def test_child_history_before_handoff_only_establishes_cumulative_baseline(self):
        self._replace_rollout()
        with open(self.rollout, "a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-02T00:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "child-thread",
                            "session_id": "parent-thread",
                            "source": {"subagent": {"name": "worker"}},
                        },
                    }
                )
                + "\n"
            )
        self._append_token_event(
            "2026-07-02T01:00:00Z",
            110,
            input_tokens=100,
            cached_input_tokens=80,
            output_tokens=10,
            cumulative_usage={
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 10,
                "reasoning_output_tokens": 0,
                "total_tokens": 110,
            },
        )
        with open(self.rollout, "a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-02T01:01:00Z",
                        "type": "event_msg",
                        "payload": {"type": "thread_settings_applied"},
                    }
                )
                + "\n"
            )
        self._append_token_event(
            "2026-07-02T01:02:00Z",
            55,
            input_tokens=50,
            cached_input_tokens=40,
            output_tokens=5,
            cumulative_usage={
                "input_tokens": 150,
                "cached_input_tokens": 120,
                "output_tokens": 15,
                "reasoning_output_tokens": 0,
                "total_tokens": 165,
            },
        )

        result = token_usage.collect_today_usage(
            codex_home=self.home,
            today=dt.date(2026, 7, 2),
            use_cache=False,
        )

        self.assertEqual(result["events"], 1)
        self.assertEqual(result["input_tokens"], 50)
        self.assertEqual(result["cached_input_tokens"], 40)
        self.assertEqual(result["output_tokens"], 5)
        self.assertEqual(result["total_tokens"], 55)

    def test_cumulative_snapshot_jump_counts_the_full_delta(self):
        self._replace_rollout()
        self._append_token_event(
            "2026-07-02T01:00:00Z",
            110,
            input_tokens=100,
            output_tokens=10,
            cumulative_usage={
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "output_tokens": 10,
                "reasoning_output_tokens": 2,
                "total_tokens": 110,
            },
        )
        self._append_token_event(
            "2026-07-02T02:00:00Z",
            55,
            input_tokens=50,
            output_tokens=5,
            cumulative_usage={
                "input_tokens": 300,
                "cached_input_tokens": 120,
                "output_tokens": 30,
                "reasoning_output_tokens": 7,
                "total_tokens": 330,
            },
        )

        result = token_usage.collect_today_usage(
            codex_home=self.home,
            today=dt.date(2026, 7, 2),
            use_cache=False,
        )

        self.assertEqual(result["events"], 2)
        self.assertEqual(result["input_tokens"], 300)
        self.assertEqual(result["cached_input_tokens"], 120)
        self.assertEqual(result["output_tokens"], 30)
        self.assertEqual(result["reasoning_output_tokens"], 7)
        self.assertEqual(result["total_tokens"], 330)

    def test_total_usage_is_counted_when_last_usage_is_missing(self):
        self._replace_rollout()
        with open(self.rollout, "a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-02T01:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "model": "model-from-token-event",
                                "total_token_usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 10,
                                    "reasoning_output_tokens": 2,
                                    "total_tokens": 110,
                                },
                            },
                        },
                    }
                )
                + "\n"
            )

        events = token_usage._read_rollout_events(
            Path(self.rollout),
            dt.date(2026, 7, 2),
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["model"], "model-from-token-event")
        self.assertEqual(events[0]["usage"]["total_tokens"], 110)

    def test_cumulative_reset_does_not_recount_the_new_baseline(self):
        self._replace_rollout()
        self._append_turn_context("gpt-5.4-mini")
        cumulative_values = (
            ("2026-07-02T01:00:00Z", 100, 10, 110),
            ("2026-07-02T02:00:00Z", 200, 20, 220),
            ("2026-07-02T03:00:00Z", 40, 10, 50),
            ("2026-07-02T03:01:00Z", 40, 10, 50),
        )
        for timestamp, cumulative_input, cumulative_output, cumulative_total in cumulative_values:
            self._append_token_event(
                timestamp,
                110 if cumulative_total != 50 else 50,
                input_tokens=100 if cumulative_total != 50 else 40,
                output_tokens=10,
                cumulative_usage={
                    "input_tokens": cumulative_input,
                    "cached_input_tokens": 0,
                    "output_tokens": cumulative_output,
                    "reasoning_output_tokens": 0,
                    "total_tokens": cumulative_total,
                },
            )

        result = token_usage.collect_today_usage(
            codex_home=self.home,
            today=dt.date(2026, 7, 2),
            use_cache=False,
        )

        self.assertEqual(result["events"], 2)
        self.assertEqual(result["total_tokens"], 220)

    def test_enriched_duplicate_snapshot_does_not_add_reasoning_twice(self):
        self._replace_rollout()
        self._append_turn_context("gpt-5.4-mini")
        first_cumulative = {
            "input_tokens": 100,
            "cached_input_tokens": 0,
            "output_tokens": 10,
            "total_tokens": 110,
        }
        enriched_cumulative = dict(first_cumulative, reasoning_output_tokens=5)
        for timestamp, cumulative in (
            ("2026-07-02T01:00:00Z", first_cumulative),
            ("2026-07-02T01:01:00Z", enriched_cumulative),
        ):
            self._append_token_event(
                timestamp,
                110,
                input_tokens=100,
                output_tokens=10,
                reasoning_output_tokens=5,
                cumulative_usage=cumulative,
            )

        result = token_usage.collect_today_usage(
            codex_home=self.home,
            today=dt.date(2026, 7, 2),
            use_cache=False,
        )

        self.assertEqual(result["events"], 1)
        self.assertEqual(result["reasoning_output_tokens"], 0)

    def test_cumulative_jump_uses_the_active_model_for_the_full_delta(self):
        self._replace_rollout()
        self._append_turn_context("model-a")
        self._append_token_event(
            "2026-07-02T01:00:00Z",
            110,
            input_tokens=100,
            output_tokens=10,
            cumulative_usage={
                "input_tokens": 100,
                "cached_input_tokens": 0,
                "output_tokens": 10,
                "reasoning_output_tokens": 0,
                "total_tokens": 110,
            },
        )
        self._append_turn_context("model-b")
        self._append_token_event(
            "2026-07-02T02:00:00Z",
            110,
            input_tokens=100,
            output_tokens=10,
            cumulative_usage={
                "input_tokens": 300_000,
                "cached_input_tokens": 0,
                "output_tokens": 10,
                "reasoning_output_tokens": 0,
                "total_tokens": 300_010,
            },
        )

        events = token_usage._read_rollout_events(
            Path(self.rollout),
            dt.date(2026, 7, 2),
        )

        self.assertEqual(events[1]["model"], "model-b")
        self.assertEqual(events[1]["usage"]["input_tokens"], 299_900)
        self.assertEqual(events[1]["usage"]["total_tokens"], 299_900)

    def test_events_use_the_model_active_at_each_turn(self):
        self._replace_rollout()
        self._append_turn_context("model-a")
        self._append_token_event(
            "2026-07-02T01:00:00Z",
            110,
            input_tokens=100,
            output_tokens=10,
            cumulative_usage={
                "input_tokens": 100,
                "cached_input_tokens": 0,
                "output_tokens": 10,
                "reasoning_output_tokens": 0,
                "total_tokens": 110,
            },
        )
        self._append_turn_context("model-b")
        self._append_token_event(
            "2026-07-02T02:00:00Z",
            110,
            input_tokens=100,
            output_tokens=10,
            cumulative_usage={
                "input_tokens": 200,
                "cached_input_tokens": 0,
                "output_tokens": 20,
                "reasoning_output_tokens": 0,
                "total_tokens": 220,
            },
        )
        prices = {
            "model-a": {"input": 1.0, "cached_input": 0.1, "output": 1.0},
            "model-b": {"input": 2.0, "cached_input": 0.2, "output": 3.0},
        }

        with mock.patch.object(token_usage, "load_model_prices", return_value=prices):
            result = token_usage.collect_today_usage(
                codex_home=self.home,
                today=dt.date(2026, 7, 2),
                use_cache=False,
            )

        self.assertFalse(result["has_unknown_prices"])
        self.assertAlmostEqual(result["cost_usd"], 0.00034, places=8)

    def test_collect_today_usage_reads_sqlite_rollout_once(self):
        result = token_usage.collect_today_usage(
            codex_home=self.home,
            today=dt.date(2026, 7, 2),
        )
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["threads"], 1)
        self.assertEqual(result["input_tokens"], 1000)
        self.assertEqual(result["cached_input_tokens"], 400)
        self.assertEqual(result["output_tokens"], 100)
        self.assertEqual(result["reasoning_output_tokens"], 25)
        self.assertEqual(result["total_tokens"], 1100)
        self.assertFalse(result["has_unknown_prices"])
        self.assertAlmostEqual(result["cost_usd"], 0.00093, places=8)
        self.assertAlmostEqual(result["input_cost_usd"], 0.00045, places=8)
        self.assertAlmostEqual(result["cached_input_cost_usd"], 0.00003, places=8)
        self.assertAlmostEqual(result["output_cost_usd"], 0.00045, places=8)

    def test_dashboard_collectors_physically_read_each_rollout_once(self):
        real_open = open
        rollout_reads = 0

        def counting_open(path, *args, **kwargs):
            nonlocal rollout_reads
            if os.fspath(path) == self.rollout:
                rollout_reads += 1
            return real_open(path, *args, **kwargs)

        token_usage.clear_usage_cache()
        with mock.patch("builtins.open", side_effect=counting_open):
            token_usage.collect_usage_days(
                days=30,
                codex_home=self.home,
                anchor_date=dt.date(2026, 7, 2),
                use_cache=False,
            )
            token_usage.collect_thread_usage_for_day(
                dt.date(2026, 7, 2),
                codex_home=self.home,
                use_cache=False,
            )
            token_usage.collect_usage_range(
                "week",
                codex_home=self.home,
                anchor_date=dt.date(2026, 7, 2),
                use_cache=False,
            )

        self.assertEqual(rollout_reads, 1)

    def test_malformed_token_line_is_skipped_without_aborting_rollout(self):
        self._replace_rollout()
        with open(self.rollout, "a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-02T00:30:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": "not-a-number",
                                    "output_tokens": 10,
                                }
                            },
                        },
                    }
                )
                + "\n"
            )
        self._append_token_event(
            "2026-07-02T01:00:00Z",
            110,
            input_tokens=100,
            output_tokens=10,
        )

        result = token_usage.collect_today_usage(
            codex_home=self.home,
            today=dt.date(2026, 7, 2),
            use_cache=False,
        )

        self.assertEqual(result["events"], 1)
        self.assertEqual(result["total_tokens"], 110)

    def test_unreadable_candidate_database_marks_result_incomplete(self):
        sqlite_dir = os.path.join(self.home, "sqlite")
        os.makedirs(sqlite_dir, exist_ok=True)
        with open(os.path.join(sqlite_dir, "broken.db"), "wb") as file:
            file.write(b"not a sqlite database")

        result = token_usage.collect_today_usage(
            codex_home=self.home,
            today=dt.date(2026, 7, 2),
            use_cache=False,
        )

        self.assertTrue(result["incomplete"])
        self.assertTrue(result["warnings"])

    def test_rollout_cache_reloads_when_file_changes(self):
        token_usage.clear_usage_cache()
        first = token_usage.collect_today_usage(
            codex_home=self.home,
            today=dt.date(2026, 7, 2),
            use_cache=False,
        )
        self._append_token_event(
            "2026-07-02T05:00:00Z",
            500,
            input_tokens=400,
            output_tokens=100,
        )

        second = token_usage.collect_today_usage(
            codex_home=self.home,
            today=dt.date(2026, 7, 2),
            use_cache=False,
        )

        self.assertEqual(first["total_tokens"], 1100)
        self.assertEqual(second["total_tokens"], 1600)

    def test_rollout_cache_evicts_old_files_at_configured_limit(self):
        token_usage.clear_usage_cache()
        paths = [os.path.join(self.home, f"cache-{index}.jsonl") for index in range(3)]
        for index, path in enumerate(paths):
            self._append_token_event(
                f"2026-07-02T0{index + 1}:00:00Z",
                110,
                input_tokens=100,
                output_tokens=10,
                rollout_path=path,
            )

        with mock.patch.object(
            token_usage,
            "MAX_ROLLOUT_CACHE_FILES",
            2,
            create=True,
        ):
            for path in paths:
                token_usage._read_rollout_activity(Path(path))

        self.assertEqual(len(token_usage._ROLLOUT_CACHE), 2)

    def test_estimate_event_cost_breakdown_splits_token_categories(self):
        breakdown, unknown = token_usage.estimate_event_cost_breakdown_usd(
            "gpt-5.4-mini",
            {
                "input_tokens": 1000,
                "cached_input_tokens": 400,
                "output_tokens": 100,
            },
            model_context_window=128000,
        )
        self.assertFalse(unknown)
        self.assertIsNotNone(breakdown)
        self.assertAlmostEqual(breakdown["input_cost_usd"], 0.00045, places=8)
        self.assertAlmostEqual(breakdown["cached_input_cost_usd"], 0.00003, places=8)
        self.assertAlmostEqual(breakdown["output_cost_usd"], 0.00045, places=8)
        self.assertAlmostEqual(breakdown["cost_usd"], 0.00093, places=8)

    def test_gpt_5_6_default_prices_match_published_table(self):
        prices = token_usage.load_model_prices()

        self.assertEqual(
            prices["gpt-5.6-sol"],
            {"input": 4.0, "cached_input": 0.4, "output": 20.0, "long_input": 8.0, "long_cached_input": 0.8, "long_output": 30.0},
        )
        self.assertEqual(
            prices["gpt-5.6-terra"],
            {"input": 2.0, "cached_input": 0.2, "output": 12.0, "long_input": 4.0, "long_cached_input": 0.4, "long_output": 18.0},
        )
        self.assertEqual(
            prices["gpt-5.6-luna"],
            {"input": 0.2, "cached_input": 0.02, "output": 1.2, "long_input": 0.4, "long_cached_input": 0.04, "long_output": 1.8},
        )
        cost, unknown = token_usage.estimate_event_cost_usd(
            "gpt-5.6-sol",
            {
                "input_tokens": 1_000_000,
                "cached_input_tokens": 800_000,
                "output_tokens": 100_000,
            },
            prices=prices,
        )
        self.assertFalse(unknown)
        self.assertAlmostEqual(cost, 5.24, places=6)

    def test_price_uses_short_context_for_small_request_on_large_model(self):
        cost, unknown = token_usage.estimate_event_cost_usd(
            "gpt-5.4",
            {
                "input_tokens": 18_096,
                "cached_input_tokens": 4_992,
                "output_tokens": 882,
            },
            model_context_window=1_050_000,
        )
        self.assertFalse(unknown)
        self.assertAlmostEqual(cost, 0.047238, places=6)

    def test_price_uses_long_context_when_actual_input_exceeds_threshold(self):
        cost, unknown = token_usage.estimate_event_cost_usd(
            "gpt-5.4",
            {
                "input_tokens": 300_000,
                "cached_input_tokens": 0,
                "output_tokens": 100_000,
            },
            model_context_window=1_050_000,
        )
        self.assertFalse(unknown)
        self.assertAlmostEqual(cost, 3.75, places=6)

    def test_unknown_model_counts_tokens_but_marks_cost_unknown(self):
        cost, unknown = token_usage.estimate_event_cost_usd(
            "unknown-model",
            {"input_tokens": 1000, "output_tokens": 100},
            model_context_window=0,
        )
        self.assertTrue(unknown)
        self.assertIsNone(cost)

    def test_format_usage_for_taskbar(self):
        self.assertEqual(token_usage.format_token_millions(116_348_966), "116.3M")
        self.assertEqual(token_usage.format_token_millions(0), "--")
        self.assertEqual(token_usage.format_cost_usd(31.424), "$31.42")
        self.assertEqual(token_usage.format_cost_usd(31.424, partial=True), "~$31.42")
        self.assertEqual(token_usage.format_cost_usd(0.004), "<$0.01")
        self.assertEqual(token_usage.format_cost_usd(None), "$--")

    def test_partial_cost_survives_unknown_events_in_either_order(self):
        breakdown = {
            "cost_usd": 1.25,
            "input_cost_usd": 0.50,
            "cached_input_cost_usd": 0.25,
            "output_cost_usd": 0.50,
        }
        for unknown_first in (False, True):
            with self.subTest(unknown_first=unknown_first):
                totals = token_usage.empty_usage()
                operations = (
                    [(None, True), (breakdown, False)]
                    if unknown_first
                    else [(breakdown, False), (None, True)]
                )
                for item, unknown in operations:
                    token_usage._add_cost_breakdown(totals, item, unknown)

                self.assertTrue(totals["has_unknown_prices"])
                self.assertEqual(totals["priced_events"], 1)
                self.assertEqual(totals["cost_usd"], 1.25)

    def test_all_unknown_events_keep_cost_unavailable(self):
        totals = token_usage.empty_usage()
        token_usage._add_cost_breakdown(totals, None, True)

        self.assertTrue(totals["has_unknown_prices"])
        self.assertEqual(totals["priced_events"], 0)
        self.assertIsNone(totals["cost_usd"])

    def test_model_price_override_changes_only_named_model(self):
        path = os.path.join(self.home, "model_prices.json")
        token_usage.save_model_price_overrides(
            {
                "gpt-5.5": {
                    "input": 1.0,
                    "cached_input": 0.1,
                    "output": 2.0,
                }
            },
            path=path,
        )
        prices = token_usage.load_model_prices(path=path)
        self.assertEqual(prices["gpt-5.5"]["input"], 1.0)
        self.assertEqual(prices["gpt-5.5"]["cached_input"], 0.1)
        self.assertEqual(prices["gpt-5.4"]["input"], 2.5)

    def test_clear_model_price_overrides_removes_file(self):
        path = os.path.join(self.home, "model_prices.json")
        token_usage.save_model_price_overrides(
            {
                "gpt-5.5": {
                    "input": 1.0,
                    "cached_input": 0.1,
                    "output": 2.0,
                }
            },
            path=path,
        )
        self.assertTrue(os.path.exists(path))
        token_usage.clear_model_price_overrides(path=path)
        self.assertFalse(os.path.exists(path))

    def test_incomplete_price_override_is_ignored(self):
        path = os.path.join(self.home, "model_prices.json")
        token_usage.save_model_price_overrides(
            {"gpt-5.5": {"input": 1.0, "cached_input": 0.1}},
            path=path,
        )
        prices = token_usage.load_model_prices(path=path)
        self.assertEqual(prices["gpt-5.5"]["input"], 5.0)

    def test_parse_price_override_text_for_settings_ui(self):
        self.assertIsNone(token_usage.parse_price_override_text(""))
        self.assertIsNone(token_usage.parse_price_override_text("   "))
        self.assertEqual(token_usage.parse_price_override_text("0.25"), 0.25)
        with self.assertRaises(ValueError):
            token_usage.parse_price_override_text("-1")
        with self.assertRaises(ValueError):
            token_usage.parse_price_override_text("abc")

    def test_complete_default_price_override_fills_missing_required_prices(self):
        completed = token_usage.complete_price_override(
            "gpt-5.5",
            {"output": 20.0},
        )
        self.assertEqual(completed["input"], 5.0)
        self.assertEqual(completed["cached_input"], 0.5)
        self.assertEqual(completed["output"], 20.0)

    def test_unknown_model_price_override_requires_base_prices(self):
        with self.assertRaises(ValueError):
            token_usage.complete_price_override("custom-model", {"output": 20.0})

    def test_collect_week_usage_returns_daily_buckets_and_summary(self):
        result = token_usage.collect_usage_range(
            "week",
            codex_home=self.home,
            anchor_date=dt.date(2026, 7, 7),
        )
        self.assertEqual(result["start_date"], dt.date(2026, 7, 1))
        self.assertEqual(result["end_date"], dt.date(2026, 7, 7))
        self.assertEqual(len(result["buckets"]), 7)
        self.assertEqual(result["buckets"][0]["label"], "07/01")
        self.assertEqual(result["buckets"][0]["total_tokens"], 1998)
        self.assertEqual(result["buckets"][1]["label"], "07/02")
        self.assertEqual(result["buckets"][1]["total_tokens"], 1100)
        self.assertEqual(result["total_tokens"], 3098)
        self.assertEqual(result["peak_label"], "07/01")
        self.assertFalse(result["has_unknown_prices"])

    def test_collect_thread_usage_for_day_groups_by_conversation(self):
        second_rollout = os.path.join(self.home, "second.jsonl")
        db = sqlite3.connect(self.db)
        db.execute(
            "INSERT INTO threads "
            "(id, rollout_path, model, updated_at_ms, title, first_user_message, preview, cwd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "t3",
                second_rollout,
                "gpt-5.4-mini",
                300,
                "",
                "Install PaperTodo and check status",
                "PaperTodo preview",
                r"C:\Projects\paper-todo",
            ),
        )
        db.commit()
        db.close()
        with open(second_rollout, "w", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-02T02:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "install papertodo",
                        },
                    }
                )
                + "\n"
            )
            file.write(
                json.dumps(
                    {
                        "timestamp": "2026-07-02T02:01:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "show me details",
                        },
                    }
                )
                + "\n"
            )
        self._append_token_event(
            "2026-07-02T02:02:00Z",
            total_tokens=5000,
            input_tokens=4000,
            cached_input_tokens=1000,
            output_tokens=1000,
            rollout_path=second_rollout,
        )

        result = token_usage.collect_thread_usage_for_day(
            dt.date(2026, 7, 2),
            codex_home=self.home,
        )

        self.assertEqual([item["title"] for item in result], [
            "Install PaperTodo and check status",
            "CodexBar WebView 改造",
        ])
        self.assertEqual(result[0]["message_count"], 2)
        self.assertEqual(result[0]["cwd_label"], "paper-todo")
        self.assertEqual(result[0]["total_tokens"], 5000)
        self.assertEqual(result[1]["message_count"], 1)
        self.assertEqual(result[1]["input_tokens"], 1000)

    def test_collect_thread_usage_prefers_catalog_display_title(self):
        sqlite_dir = os.path.join(self.home, "sqlite")
        os.makedirs(sqlite_dir, exist_ok=True)
        catalog = os.path.join(sqlite_dir, "codex-dev.db")
        db = sqlite3.connect(catalog)
        db.execute(
            "CREATE TABLE local_thread_catalog ("
            "host_id TEXT, thread_id TEXT, display_title TEXT, source_updated_at TEXT, "
            "cwd TEXT, source_kind TEXT, source_detail TEXT, model_provider TEXT, "
            "git_branch TEXT, observation_sequence INTEGER, missing_candidate INTEGER)"
        )
        db.execute(
            "INSERT INTO local_thread_catalog "
            "(host_id, thread_id, display_title, source_updated_at, cwd, source_kind, "
            "source_detail, model_provider, git_branch, observation_sequence, missing_candidate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "local",
                "t1",
                "用户重命名后的 CodexBar 对话",
                "2026-07-02T08:00:00Z",
                r"C:\Work\CodexBar",
                "local",
                "",
                "openai",
                "main",
                10,
                0,
            ),
        )
        db.commit()
        db.close()

        result = token_usage.collect_thread_usage_for_day(
            dt.date(2026, 7, 2),
            codex_home=self.home,
        )

        self.assertEqual(result[0]["title"], "用户重命名后的 CodexBar 对话")

    def test_collect_thread_usage_reports_rollout_progress(self):
        reports = []

        token_usage.collect_thread_usage_for_day(
            dt.date(2026, 7, 2),
            codex_home=self.home,
            progress_callback=reports.append,
        )

        self.assertGreaterEqual(len(reports), 2)
        self.assertEqual(reports[0]["processed"], 0)
        self.assertEqual(reports[-1]["processed"], reports[-1]["total"])
        self.assertIn("扫描 rollout", reports[-1]["detail"])

    def test_collect_month_usage_uses_current_month_daily_buckets(self):
        result = token_usage.collect_usage_range(
            "month",
            codex_home=self.home,
            anchor_date=dt.date(2026, 7, 7),
        )
        self.assertEqual(result["start_date"], dt.date(2026, 7, 1))
        self.assertEqual(result["end_date"], dt.date(2026, 7, 31))
        self.assertEqual(len(result["buckets"]), 31)
        self.assertEqual(result["total_tokens"], 3098)
        self.assertEqual(result["buckets"][30]["label"], "07/31")

    def test_collect_year_usage_groups_by_month(self):
        self._append_token_event(
            "2026-02-03T03:00:00Z",
            total_tokens=5000,
            input_tokens=4000,
            cached_input_tokens=1000,
            output_tokens=1000,
        )
        result = token_usage.collect_usage_range(
            "year",
            codex_home=self.home,
            anchor_date=dt.date(2026, 7, 7),
        )
        self.assertEqual(result["start_date"], dt.date(2026, 1, 1))
        self.assertEqual(result["end_date"], dt.date(2026, 12, 31))
        self.assertEqual(len(result["buckets"]), 12)
        self.assertEqual(result["buckets"][1]["label"], "2026/02")
        self.assertEqual(result["buckets"][1]["total_tokens"], 5000)
        self.assertEqual(result["total_tokens"], 8098)
        self.assertEqual(result["peak_label"], "2026/02")

    def test_collect_range_marks_unknown_price_but_keeps_tokens(self):
        unknown_rollout = os.path.join(self.home, "unknown.jsonl")
        db = sqlite3.connect(self.db)
        db.execute(
            "INSERT INTO threads (id, rollout_path, model, updated_at_ms) VALUES (?, ?, ?, ?)",
            ("t3", unknown_rollout, "unknown-model", 300),
        )
        db.commit()
        db.close()
        self._append_token_event(
            "2026-07-02T04:00:00Z",
            total_tokens=700,
            input_tokens=600,
            output_tokens=100,
            rollout_path=unknown_rollout,
        )
        result = token_usage.collect_usage_range(
            "month",
            codex_home=self.home,
            anchor_date=dt.date(2026, 7, 7),
        )
        self.assertEqual(result["total_tokens"], 3798)
        self.assertTrue(result["has_unknown_prices"])
        self.assertEqual(result["priced_events"], 2)
        self.assertAlmostEqual(result["cost_usd"], 0.00617475, places=8)

    def test_collect_usage_days_returns_recent_daily_buckets(self):
        result = token_usage.collect_usage_days(
            days=3,
            codex_home=self.home,
            anchor_date=dt.date(2026, 7, 3),
        )
        self.assertEqual(result["start_date"], dt.date(2026, 7, 1))
        self.assertEqual(result["end_date"], dt.date(2026, 7, 3))
        self.assertEqual([bucket["label"] for bucket in result["buckets"]], ["07/01", "07/02", "07/03"])
        self.assertEqual(result["buckets"][0]["total_tokens"], 1998)
        self.assertEqual(result["buckets"][1]["total_tokens"], 1100)
        self.assertEqual(result["total_tokens"], 3098)


class UsageDashboardFormattingTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(web_dashboard.pricing, "refresh_prices", return_value={})
        patch.start()
        self.addCleanup(patch.stop)

    def test_format_usage_summary_contains_range_totals_and_peak(self):
        data = {
            "period": "week",
            "start_date": dt.date(2026, 7, 1),
            "end_date": dt.date(2026, 7, 7),
            "total_tokens": 3098,
            "cost_usd": 0.0123,
            "average_daily_cost_usd": 0.00175,
            "peak_label": "07/01",
            "peak_tokens": 1998,
            "cached_input_ratio": 0.25,
            "output_ratio": 0.1,
            "has_unknown_prices": False,
        }
        summary = web_dashboard.format_usage_summary(data)
        self.assertIn("2026-07-01 - 2026-07-07", summary)
        self.assertIn("3,098 tokens", summary)
        self.assertIn("$0.01", summary)
        self.assertIn("峰值: 07/01", summary)

    def test_build_receipt_splits_tokens_costs_and_percentages(self):
        data = {
            "label": "07/04",
            "start_date": dt.date(2026, 7, 4),
            "input_tokens": 1000,
            "cached_input_tokens": 400,
            "output_tokens": 100,
            "reasoning_output_tokens": 25,
            "total_tokens": 1100,
            "cost_usd": 0.00093,
            "input_cost_usd": 0.00045,
            "cached_input_cost_usd": 0.00003,
            "output_cost_usd": 0.00045,
            "events": 1,
            "threads": 1,
            "incomplete": True,
            "warnings": ["broken.db: DatabaseError"],
        }
        receipt = web_dashboard.build_receipt(data)
        self.assertEqual(receipt["date"], "2026-07-04")
        self.assertEqual(receipt["total_tokens_text"], "1.1K")
        self.assertEqual(receipt["cost_text"], "<$0.01")
        categories = receipt["categories"]
        self.assertEqual(categories[0]["label"], "输入")
        self.assertEqual(categories[0]["tokens"], 600)
        self.assertAlmostEqual(categories[0]["percent"], 54.545, places=2)
        self.assertEqual(categories[1]["label"], "缓存")
        self.assertEqual(categories[1]["tokens"], 400)
        self.assertEqual(categories[2]["label"], "输出")
        self.assertEqual(categories[2]["tokens"], 75)
        self.assertEqual(categories[3]["label"], "推理")
        self.assertEqual(categories[3]["tokens"], 25)
        self.assertEqual(sum(item["tokens"] for item in categories), 1100)
        self.assertAlmostEqual(sum(item["percent"] for item in categories), 100.0)
        self.assertAlmostEqual(categories[2]["cost"], 0.0003375, places=8)
        self.assertAlmostEqual(categories[3]["cost"], 0.0001125, places=8)
        self.assertTrue(receipt["incomplete"])
        self.assertEqual(receipt["warnings"], ["broken.db: DatabaseError"])

    def test_dashboard_api_returns_recent_day_list(self):
        class FakeTokenUsage:
            @staticmethod
            def collect_usage_days(days=30, use_cache=True):
                return {
                    "buckets": [
                        {
                            "label": "07/03",
                            "start_date": dt.date(2026, 7, 3),
                            "total_tokens": 4500,
                            "cost_usd": 0.12,
                            "input_tokens": 4000,
                            "cached_input_tokens": 1000,
                            "output_tokens": 500,
                            "reasoning_output_tokens": 0,
                            "input_cost_usd": 0.02,
                            "cached_input_cost_usd": 0.01,
                            "output_cost_usd": 0.09,
                            "events": 4,
                            "threads": 2,
                        },
                        {
                            "label": "07/04",
                            "start_date": dt.date(2026, 7, 4),
                            "total_tokens": 9000,
                            "cost_usd": 0.21,
                            "input_tokens": 7000,
                            "cached_input_tokens": 2000,
                            "output_tokens": 2000,
                            "reasoning_output_tokens": 200,
                            "input_cost_usd": 0.04,
                            "cached_input_cost_usd": 0.01,
                            "output_cost_usd": 0.16,
                            "events": 8,
                            "threads": 3,
                        },
                    ]
                }

        api = web_dashboard.DashboardApi(token_usage_module=FakeTokenUsage)
        days = api.get_days(limit=2)
        self.assertEqual([day["date"] for day in days], ["2026-07-04", "2026-07-03"])
        self.assertEqual(days[0]["total_tokens_text"], "9.0K")
        self.assertEqual(days[0]["cost_text"], "$0.21")

    def test_dashboard_initial_state_selects_today(self):
        today = dt.date.today()
        old_day = today - dt.timedelta(days=1)

        class FakeTokenUsage:
            @staticmethod
            def collect_usage_days(days=30, anchor_date=None, use_cache=True):
                if days == 1:
                    target = anchor_date or today
                    return {"buckets": [{"label": target.strftime("%m/%d"), "start_date": target}]}
                return {
                    "buckets": [
                        {"label": old_day.strftime("%m/%d"), "start_date": old_day},
                    ]
                }

            @staticmethod
            def collect_thread_usage_for_day(day, use_cache=False):
                return []

            @staticmethod
            def collect_usage_range(period):
                return {
                    "period": period,
                    "start_date": today - dt.timedelta(days=6),
                    "end_date": today,
                    "buckets": [],
                }

        api = web_dashboard.DashboardApi(token_usage_module=FakeTokenUsage)
        state = api.get_initial_state()

        self.assertEqual(state["selected"]["date"], today.isoformat())

    def test_dashboard_load_job_reports_running_then_done(self):
        release = threading.Event()
        entered = threading.Event()

        class SlowTokenUsage:
            @staticmethod
            def collect_usage_days(days=30, anchor_date=None, use_cache=True):
                entered.set()
                release.wait(timeout=2)
                target = anchor_date or dt.date.today()
                return {"buckets": [{"label": target.strftime("%m/%d"), "start_date": target}]}

            @staticmethod
            def collect_thread_usage_for_day(day, use_cache=False, progress_callback=None):
                if progress_callback:
                    progress_callback({"processed": 1, "total": 1, "detail": "扫描 rollout 1/1"})
                return []

            @staticmethod
            def collect_usage_range(period):
                today = dt.date.today()
                return {
                    "period": period,
                    "start_date": today - dt.timedelta(days=6),
                    "end_date": today,
                    "buckets": [],
                }

        api = web_dashboard.DashboardApi(token_usage_module=SlowTokenUsage)
        job_id = api.start_initial_load()["job_id"]
        self.assertTrue(entered.wait(timeout=1))
        self.assertEqual(api.start_initial_load()["job_id"], job_id)
        running = api.get_load_status(job_id)
        self.assertEqual(running["state"], "running")
        self.assertGreater(running["percent"], 0)

        release.set()
        for _ in range(40):
            status = api.get_load_status(job_id)
            if status["state"] == "done":
                break
            time.sleep(0.05)

        self.assertEqual(status["state"], "done")
        self.assertEqual(status["percent"], 100)
        self.assertEqual(status["result"], api.get_initial_state())

    def test_dashboard_load_job_sanitizes_failed_error(self):
        class FailingTokenUsage:
            @staticmethod
            def collect_usage_days(days=30, anchor_date=None, use_cache=True):
                raise RuntimeError(
                    "Authorization: Bearer secret access_token=abc refresh_token=def"
                )

        api = web_dashboard.DashboardApi(token_usage_module=FailingTokenUsage)
        job_id = api.start_initial_load()["job_id"]
        for _ in range(40):
            status = api.get_load_status(job_id)
            if status["state"] == "failed":
                break
            time.sleep(0.05)

        self.assertEqual(status["state"], "failed")
        self.assertNotIn("secret", status["error"])
        self.assertNotIn("abc", status["error"])
        self.assertNotIn("def", status["error"])

    def test_dashboard_discards_old_terminal_jobs(self):
        api = web_dashboard.DashboardApi()
        for index in range(12):
            api._set_job_status(
                f"job-{index}",
                state="done",
                percent=100,
                result={"index": index},
            )

        self.assertEqual(len(api._jobs), 8)
        self.assertNotIn("job-0", api._jobs)
        self.assertIn("job-11", api._jobs)

    def test_dashboard_assets_include_loading_overlay_and_polling(self):
        asset_dir = os.path.join(os.path.dirname(__file__), "codexbar", "web_assets")
        with open(os.path.join(asset_dir, "dashboard.html"), encoding="utf-8") as file:
            html = file.read()
        with open(os.path.join(asset_dir, "dashboard.js"), encoding="utf-8") as file:
            script = file.read()
        with open(os.path.join(asset_dir, "dashboard.css"), encoding="utf-8") as file:
            css = file.read()

        self.assertIn('id="loadingOverlay"', html)
        self.assertIn("start_initial_load", script)
        self.assertIn("get_load_status", script)
        self.assertIn("dayRequestGeneration", script)
        self.assertIn("periodRequestGeneration", script)
        self.assertIn("requestGeneration !== dayRequestGeneration", script)
        self.assertIn("requestGeneration !== periodRequestGeneration", script)
        self.assertIn("数据可能不完整", script)
        self.assertIn("loading-progress", css)
        self.assertIn("token-tape", css)

    def test_dashboard_assets_enforce_local_content_and_avoid_html_templates(self):
        asset_dir = Path(__file__).resolve().parent / "codexbar" / "web_assets"
        html = (asset_dir / "dashboard.html").read_text(encoding="utf-8")
        script = (asset_dir / "dashboard.js").read_text(encoding="utf-8")

        self.assertIn("Content-Security-Policy", html)
        for directive in (
            "default-src 'self'",
            "object-src 'none'",
            "frame-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            "connect-src 'none'",
        ):
            self.assertIn(directive, html)
        self.assertNotRegex(script, r"\.innerHTML\s*=\s*`")
        self.assertIn("textContent", script)

    def test_dashboard_url_guard_accepts_only_the_bundled_file(self):
        expected = "file:///C:/Apps/CodexBar/dashboard.html"

        self.assertTrue(web_dashboard.is_dashboard_url(expected, expected))
        self.assertTrue(
            web_dashboard.is_dashboard_url(expected + "?day=2026-07-13#chart", expected)
        )
        self.assertFalse(
            web_dashboard.is_dashboard_url("https://example.test/dashboard.html", expected)
        )
        self.assertFalse(
            web_dashboard.is_dashboard_url(
                "file:///C:/Apps/CodexBar/other.html", expected
            )
        )

    def test_dashboard_navigation_guard_closes_untrusted_page(self):
        window = mock.Mock()
        expected = "file:///C:/Apps/CodexBar/dashboard.html"
        window.get_current_url.return_value = "https://example.test/"

        allowed = web_dashboard.guard_dashboard_navigation(window, expected)

        self.assertFalse(allowed)
        window.destroy.assert_called_once_with()

    def test_dashboard_launch_uses_private_browser_storage(self):
        class FakeLoadedEvent:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        fake_window = mock.Mock()
        fake_window.events.loaded = FakeLoadedEvent()
        fake_webview = mock.Mock()
        fake_webview.create_window.return_value = fake_window

        with mock.patch.dict("sys.modules", {"webview": fake_webview}):
            web_dashboard.launch()

        self.assertTrue(fake_window.events.loaded.handlers)
        fake_webview.start.assert_called_once_with(private_mode=True)

    def test_dashboard_layout_uses_responsive_line_chart(self):
        fake_webview = mock.Mock()
        fake_webview.create_window.return_value = mock.MagicMock()
        with mock.patch.dict("sys.modules", {"webview": fake_webview}):
            web_dashboard.launch()

        kwargs = fake_webview.create_window.call_args.kwargs
        self.assertEqual(kwargs["width"], 1720)
        self.assertEqual(kwargs["height"], 980)
        self.assertEqual(kwargs["min_size"], (1600, 720))

        asset_dir = os.path.join(os.path.dirname(__file__), "codexbar", "web_assets")
        css_path = os.path.join(
            asset_dir,
            "dashboard.css",
        )
        with open(css_path, encoding="utf-8") as file:
            css = file.read()
        with open(os.path.join(asset_dir, "dashboard.js"), encoding="utf-8") as file:
            script = file.read()

        self.assertIn("overflow-x: hidden;", css)
        self.assertIn("html {\n  height: 100%;\n  overflow: hidden;", css)
        self.assertIn("height: 100%;\n  padding: 22px 0;\n  overflow-x: hidden;\n  overflow-y: auto;", css)
        self.assertIn("padding: 22px 0;", css)
        self.assertIn("margin: 0 auto;", css)
        self.assertIn("grid-template-columns: 62px minmax(62px, 1fr) 68px;", css)
        self.assertIn("scrollbar-gutter: stable;", css)
        self.assertIn("padding-right: 2px;", css)
        self.assertIn("grid-template-columns: 280px minmax(390px, 0.8fr) minmax(800px, 1.2fr);", css)
        self.assertIn("overflow-y: auto;", css)
        self.assertIn("overflow-x: hidden;", css)
        self.assertIn("scrollbar-width: thin;", css)
        self.assertIn(".trend-svg", css)
        self.assertIn(".chart-line", css)
        self.assertIn(".chart-tooltip", css)
        self.assertIn("@media (max-height: 1000px)", css)
        self.assertIn("height: 176px;", css)
        self.assertNotIn(".bar-wrap", css)
        self.assertIn("chart.className = `chart ${range.period || state.period}`;", script)
        self.assertIn("axisLabelIndexes(buckets.length, period === \"month\" ? 7 : 12)", script)
        self.assertIn("const linePath = points.map", script)
        self.assertIn("Math.round(chart.clientHeight || 208)", script)
        self.assertIn("showChartTooltip", script)

    def test_dashboard_day_includes_conversation_rows(self):
        class FakeTokenUsage:
            @staticmethod
            def collect_usage_days(days=30, anchor_date=None, use_cache=True):
                return {
                    "buckets": [
                        {
                            "label": "07/04",
                            "start_date": dt.date(2026, 7, 4),
                            "total_tokens": 10_000,
                            "cost_usd": 0.31,
                            "input_tokens": 8000,
                            "cached_input_tokens": 3000,
                            "output_tokens": 2000,
                            "reasoning_output_tokens": 0,
                            "input_cost_usd": 0.05,
                            "cached_input_cost_usd": 0.01,
                            "output_cost_usd": 0.25,
                            "events": 5,
                            "threads": 1,
                        }
                    ]
                }

            @staticmethod
            def collect_thread_usage_for_day(day, use_cache=False):
                return [
                    {
                        "thread_id": "t1",
                        "title": "CodexBar WebView 改造和右侧对话排行设计",
                        "full_title": "CodexBar WebView 改造和右侧对话排行设计",
                        "model": "gpt-5.4-mini",
                        "cwd": r"C:\Work\CodexBar",
                        "cwd_label": "CodexBar",
                        "updated_at_ms": 123,
                        "message_count": 3,
                        "input_tokens": 8000,
                        "cached_input_tokens": 3000,
                        "output_tokens": 2000,
                        "reasoning_output_tokens": 0,
                        "total_tokens": 10_000,
                        "cost_usd": 0.31,
                        "input_cost_usd": 0.05,
                        "cached_input_cost_usd": 0.01,
                        "output_cost_usd": 0.25,
                        "has_unknown_prices": False,
                        "events": 5,
                        "threads": 1,
                    }
                ]

        api = web_dashboard.DashboardApi(token_usage_module=FakeTokenUsage)
        receipt = api.get_day("2026-07-04")

        self.assertEqual(len(receipt["conversations"]), 1)
        row = receipt["conversations"][0]
        self.assertEqual(row["title"], "CodexBar WebView 改造和右侧对话排行设计")
        self.assertEqual(row["tokens_text"], "10.0K")
        self.assertEqual(row["cost_text"], "$0.31")
        self.assertEqual(row["ico_text"], "50/30/20")
        self.assertEqual(row["message_text"], "3")
        self.assertEqual(row["cwd_label"], "CodexBar")


@unittest.skipUnless(taskbar._IS_WINDOWS, "Windows named mutex only")
class SingleInstanceTests(unittest.TestCase):
    def test_mutex_rejects_duplicate_and_releases_cleanly(self):
        name = rf"Local\CodexBar.Test.{os.getpid()}.{id(self)}"
        first = taskbar.acquire_single_instance(name)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(taskbar.acquire_single_instance(name))
        finally:
            taskbar.release_single_instance(first)

        after_release = taskbar.acquire_single_instance(name)
        self.assertIsNotNone(after_release)
        taskbar.release_single_instance(after_release)


if __name__ == "__main__":
    unittest.main()
