import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class BrowserLauncherTests(unittest.TestCase):
    def test_configured_browser_path_has_priority(self) -> None:
        from bosshunter.browser.launcher import find_browser_executable

        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "msedge.exe"
            executable.write_bytes(b"exe")

            result = find_browser_executable("chrome", executable)

        self.assertEqual(result, executable)

    @patch("bosshunter.browser.launcher.shutil.which")
    def test_auto_discovery_accepts_edge_when_chrome_is_missing(self, which: Mock) -> None:
        from bosshunter.browser.launcher import find_browser_executable

        which.side_effect = lambda name: "C:/Edge/msedge.exe" if name in {"msedge", "msedge.exe"} else None

        result = find_browser_executable("auto")

        self.assertEqual(result, Path("C:/Edge/msedge.exe"))

    def test_build_command_uses_dedicated_profile_and_opens_only_dashboard(self) -> None:
        from bosshunter.browser.launcher import build_browser_command

        command = build_browser_command(
            Path("C:/Chrome/chrome.exe"),
            debug_port=9222,
            user_data_dir=Path("D:/BossHunter/data/browser-profile"),
            dashboard_url="http://127.0.0.1:8686",
        )

        self.assertIn("--remote-debugging-port=9222", command)
        self.assertIn("--user-data-dir=D:\\BossHunter\\data\\browser-profile", command)
        self.assertIn("http://127.0.0.1:8686", command)
        self.assertNotIn("https://www.zhipin.com/", command)

    @patch("bosshunter.browser.launcher.subprocess.Popen")
    def test_launch_browser_starts_visible_process(self, popen: Mock) -> None:
        from bosshunter.browser.launcher import launch_browser

        popen.return_value = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            result = launch_browser(
                executable=Path("C:/Chrome/chrome.exe"),
                debug_port=9222,
                user_data_dir=Path(tmp) / "browser-profile",
                dashboard_url="http://127.0.0.1:8686",
            )

        self.assertIs(result, popen.return_value)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], str(Path("C:/Chrome/chrome.exe")))
        self.assertNotIn("creationflags", popen.call_args.kwargs)
