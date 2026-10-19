"""Installer asset regressions without wxPython, networking or disk operations.

Run: python3 -m unittest discover -s ci_tooling -p test_installer_assets.py -v
The real URL, download/copy, flash-result and retry methods run with temporary
files. wx dialogs, download transport, subprocesses and KDK/Metallib are mocked.
These checks do not validate a packaged app, real USB or macOS installation.
"""

import importlib
import plistlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "_oclp_installer_assets_test"
for name in ("", ".wx_gui", ".support", ".datasets", ".detections"):
    module = types.ModuleType(PACKAGE + name)
    module.__path__ = [str(ROOT / "opencore_legacy_patcher" / name.lstrip(".").replace(".", "/"))]
    sys.modules[module.__name__] = module

for name in (
    "constants", "volume", "wx_gui.gui_entry", "wx_gui.gui_main_menu",
    "wx_gui.gui_build", "wx_gui.gui_support", "detections.device_probe",
    "detections.os_probe", "support.utilities", "support.defaults",
    "support.arguments", "support.reroute_payloads", "support.logging_handler",
    "support.analytics_handler", "support.macos_installer_handler",
    "support.network_handler", "support.kdk_handler", "support.metallib_handler",
    "support.subprocess_wrapper",
):
    sys.modules[PACKAGE + "." + name] = types.ModuleType(PACKAGE + "." + name)
sys.modules[PACKAGE + ".constants"].Constants = object
sys.modules[PACKAGE + ".volume"].generate_copy_arguments = mock.Mock(side_effect=AssertionError("Unexpected native volume access"))

wx = types.ModuleType("wx")
wx.Frame = object
wx.Event = object
wx.YES, wx.NO, wx.YES_NO, wx.ICON_WARNING = 1, 2, 4, 8
with mock.patch.dict(sys.modules, {"wx": wx}):
    application = importlib.import_module(PACKAGE + ".application_entry")
    flash = importlib.import_module(PACKAGE + ".wx_gui.gui_macos_installer_flash")


class InstallerURLTests(unittest.TestCase):
    def configure(self, ref):
        app = application.OpenCoreLegacyPatcher.__new__(application.OpenCoreLegacyPatcher)
        app.constants = SimpleNamespace(
            commit_info=(ref, "", ""),
            repo_link="https://github.com/dortania/OpenCore-Legacy-Patcher",
            installer_pkg_url="https://github.com/dortania/OpenCore-Legacy-Patcher/releases/download/3.0.0/AutoPkg-Assets.pkg",
            installer_pkg_url_nightly="http://nightly.link/dortania/OpenCore-Legacy-Patcher/workflows/build-app-wxpython/main/AutoPkg-Assets.pkg.zip",
        )
        app._configure_installer_pkg_urls()
        return app.constants

    def test_release_tags_select_matching_assets_without_tagged_nightly_paths(self):
        for tag in ("3.0.0-rc.3", "3.0.0", "3.1.0-rc.1"):
            with self.subTest(tag=tag):
                config = self.configure("refs/tags/" + tag)
                self.assertIn(f"/download/{tag}/AutoPkg-Assets.pkg", config.installer_pkg_url)
                self.assertIn("/main/", config.installer_pkg_url_nightly)
                self.assertNotIn("refs/tags", config.installer_pkg_url_nightly)

    def test_nightly_branch_and_source_builds_preserve_existing_selection(self):
        for ref in ("refs/heads/main", "refs/heads/feature/assets", "Running from source", "Built from source"):
            with self.subTest(ref=ref):
                config = self.configure(ref)
                branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else "main"
                self.assertIn(f"/build-app-wxpython/{branch}/", config.installer_pkg_url_nightly)
                self.assertIn("/download/3.0.0/", config.installer_pkg_url)


class InstallerAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.usb = self.root / "USB"
        version = self.usb / "System/Library/CoreServices/SystemVersion.plist"
        version.parent.mkdir(parents=True)
        version.write_bytes(plistlib.dumps({"ProductVersion": "26.7.1", "ProductBuildVersion": "25G241"}))
        self.frame = flash.macOSInstallerFlashFrame.__new__(flash.macOSInstallerFlashFrame)
        self.frame.constants = SimpleNamespace(
            installer_pkg_path=self.root / "AutoPkg-Assets.pkg",
            installer_pkg_zip_path=self.root / "AutoPkg-Assets.pkg.zip",
            installer_pkg_url="https://example.invalid/releases/download/3.0.0-rc.3/AutoPkg-Assets.pkg",
            installer_pkg_url_nightly="https://example.invalid/main/AutoPkg-Assets.pkg.zip",
            payload_path=self.root,
            installer_sh_path=self.root / "installer.sh",
        )
        self.frame.constants.installer_sh_path.write_text("fixture; never executed")
        self.frame.installer_assets_error = ""
        self.frame._chainload_metallib = mock.Mock()
        self.frame._kdk_chainload = mock.Mock()
        self.download_success = True
        self.download_bytes = b"package-fixture"
        self.archive_extracts_package = True

        def download(url, path):
            obj = mock.Mock(download_complete=self.download_success, error_msg="network unavailable")
            obj.download.side_effect = lambda **kwargs: Path(path).write_bytes(self.download_bytes)
            return obj

        def run(args, **kwargs):
            if args[0] == "/bin/cp":
                shutil.copyfile(args[1], Path(args[2]) / Path(args[1]).name)
            elif args[0] == "/usr/bin/ditto":
                if self.archive_extracts_package:
                    self.frame.constants.installer_pkg_path.write_bytes(b"archive-package-fixture")
            else:
                raise AssertionError(f"Unexpected subprocess: {args}")
            return subprocess.CompletedProcess(args, 0)

        patches = (
            mock.patch.object(flash.network_handler, "NetworkUtilities", create=True, return_value=mock.Mock(validate_link=mock.Mock(return_value=True))),
            mock.patch.object(flash.network_handler, "DownloadObject", create=True, side_effect=download),
            mock.patch.object(flash.utilities, "grab_mount_point_from_disk", create=True, return_value=str(self.usb)),
            mock.patch.object(flash.utilities, "disable_sleep_while_running", create=True),
            mock.patch.object(flash.utilities, "enable_sleep_after_running", create=True),
            mock.patch.object(flash.gui_support, "wait_for_thread", create=True, side_effect=lambda thread: thread.join()),
            mock.patch.object(flash, "generate_copy_arguments", create=True, side_effect=lambda src, dst: ["/bin/cp", src, dst]),
            mock.patch.object(flash.subprocess, "run", side_effect=run),
            mock.patch.object(wx, "MessageBox", create=True, return_value=wx.NO),
        )
        self.mocks = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)

    @property
    def destination(self):
        return self.usb / "Library/Packages/AutoPkg-Assets.pkg"

    def test_success_creates_packages_directory_and_copies_verified_assets(self):
        self.frame._prepare_installer_assets("disk-fixture")
        self.assertEqual(self.frame.installer_assets_error, "")
        self.assertEqual(self.destination.read_bytes(), self.download_bytes)
        flash.utilities.grab_mount_point_from_disk.assert_called_with("disk-fixtures2")
        self.frame._kdk_chainload.assert_called_once()

    def test_missing_release_uses_nightly_archive_and_validates_extraction(self):
        flash.network_handler.NetworkUtilities.return_value.validate_link.return_value = False
        self.frame._prepare_installer_assets("disk-fixture")
        self.assertEqual(self.frame.installer_assets_error, "")
        self.assertEqual(self.destination.read_bytes(), b"archive-package-fixture")
        self.assertEqual(flash.network_handler.DownloadObject.call_args.args[0], self.frame.constants.installer_pkg_url_nightly)

    def test_failed_download_does_not_copy_stale_or_partial_package(self):
        self.frame.constants.installer_pkg_path.write_bytes(b"stale-cache")
        self.download_success = False
        with self.assertLogs(level="ERROR"):
            self.frame._prepare_installer_assets("disk-fixture")
        self.assertIn("network unavailable", self.frame.installer_assets_error)
        self.assertFalse(self.destination.exists())
        self.frame._kdk_chainload.assert_not_called()

    def test_empty_download_is_not_accepted(self):
        self.download_bytes = b""
        with self.assertLogs(level="ERROR"):
            self.frame._prepare_installer_assets("disk-fixture")
        self.assertIn("non-empty", self.frame.installer_assets_error)
        self.assertFalse(self.destination.exists())

    def test_exception_without_message_still_blocks_completion(self):
        flash.network_handler.NetworkUtilities.side_effect = RuntimeError()
        with self.assertLogs(level="ERROR"):
            self.frame._prepare_installer_assets("disk-fixture")
        self.assertIn("RuntimeError", self.frame.installer_assets_error)
        self.assertFalse(self.destination.exists())

    def test_archive_without_package_does_not_reuse_stale_cache(self):
        self.frame.constants.installer_pkg_path.write_bytes(b"stale-cache")
        self.archive_extracts_package = False
        flash.network_handler.NetworkUtilities.return_value.validate_link.return_value = False
        with self.assertLogs(level="ERROR"):
            self.frame._prepare_installer_assets("disk-fixture")
        self.assertIn("non-empty", self.frame.installer_assets_error)
        self.assertFalse(self.destination.exists())

    def test_extraction_failure_reaches_completion_error(self):
        flash.network_handler.NetworkUtilities.return_value.validate_link.return_value = False
        flash.subprocess.run.side_effect = subprocess.CalledProcessError(1, "ditto")
        with self.assertLogs(level="ERROR"):
            self.frame._prepare_installer_assets("disk-fixture")
        self.assertTrue(self.frame.installer_assets_error)
        self.assertFalse(self.destination.exists())

    def test_copy_failure_is_reported(self):
        flash.subprocess.run.side_effect = PermissionError("USB read-only")
        with self.assertLogs(level="ERROR"):
            self.frame._prepare_installer_assets("disk-fixture")
        self.assertIn("read-only", self.frame.installer_assets_error)
        self.frame._kdk_chainload.assert_not_called()

    def test_successful_copy_exit_with_wrong_or_missing_bytes_is_rejected(self):
        for corrupt in (None, b"X" * len(self.download_bytes)):
            with self.subTest(corrupt=corrupt):
                self.destination.unlink(missing_ok=True)
                def corrupt_copy(args, **kwargs):
                    if corrupt is not None:
                        self.destination.write_bytes(corrupt)
                    return subprocess.CompletedProcess(args, 0)
                flash.subprocess.run.side_effect = corrupt_copy
                with self.assertLogs(level="ERROR"):
                    self.frame._prepare_installer_assets("disk-fixture")
                self.assertIn("copy verification failed", self.frame.installer_assets_error)
        self.frame._kdk_chainload.assert_not_called()

    def test_missing_installer_version_is_reported(self):
        (self.usb / "System/Library/CoreServices/SystemVersion.plist").unlink()
        with self.assertLogs(level="ERROR"):
            self.frame._prepare_installer_assets("disk-fixture")
        self.assertIn("Could not identify", self.frame.installer_assets_error)

    def test_pre_big_sur_installer_does_not_require_assets(self):
        version = self.usb / "System/Library/CoreServices/SystemVersion.plist"
        version.write_bytes(plistlib.dumps({"ProductVersion": "10.15.7", "ProductBuildVersion": "19H2026"}))
        self.download_success = False
        with self.assertLogs(level="ERROR"):
            self.frame._prepare_installer_assets("disk-fixture")
        self.assertEqual(self.frame.installer_assets_error, "")
        self.assertFalse(self.destination.exists())
        self.frame._kdk_chainload.assert_not_called()

    def test_copy_subprocess_nonzero_exit_is_reported(self):
        flash.subprocess.run.side_effect = subprocess.CalledProcessError(1, "/bin/cp")
        with self.assertLogs(level="ERROR"):
            self.frame._prepare_installer_assets("disk-fixture")
        self.assertTrue(self.frame.installer_assets_error)
        self.frame._kdk_chainload.assert_not_called()

    def test_failed_retry_can_be_cancelled_without_reflashing(self):
        self.frame.installer_assets_error = "previous failure"
        self.download_success = False
        wx.MessageBox.side_effect = [wx.YES, wx.NO]
        with self.assertLogs(level="ERROR"):
            self.assertFalse(self.frame._retry_installer_assets("disk-fixture", mock.Mock(), mock.Mock()))
        self.assertIn("network unavailable", self.frame.installer_assets_error)
        flash.subprocess.run.assert_not_called()

    def test_cancel_retry_does_not_claim_success_or_modify_usb(self):
        self.frame.installer_assets_error = "network unavailable"
        self.assertFalse(self.frame._retry_installer_assets("disk-fixture", mock.Mock(), mock.Mock()))
        flash.network_handler.DownloadObject.assert_not_called()
        flash.subprocess.run.assert_not_called()

    def test_retry_prepares_assets_without_reflashing_installer(self):
        self.frame.installer_assets_error = "previous failure"
        wx.MessageBox.return_value = wx.YES
        animation = mock.Mock()
        self.assertTrue(self.frame._retry_installer_assets("disk-fixture", mock.Mock(), animation))
        self.assertEqual(self.frame.installer_assets_error, "")
        self.assertEqual(self.destination.read_bytes(), self.download_bytes)
        self.assertEqual([call.args[0][0] for call in flash.subprocess.run.call_args_list], ["/bin/cp"])
        animation.start_pulse.assert_called_once()
        animation.stop_pulse.assert_called_once()
        flash.utilities.enable_sleep_after_running.assert_called()

    def test_flash_records_missing_assets_even_when_apple_installer_succeeds(self):
        self.download_success = False
        with mock.patch.object(flash.subprocess_wrapper, "run_as_root", create=True,
                               return_value=SimpleNamespace(stdout="Install media now available at /USB", stderr="")):
            with self.assertLogs(level="ERROR"):
                self.assertTrue(self.frame._flash_installer("disk-fixture"))
        self.assertIn("network unavailable", self.frame.installer_assets_error)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
