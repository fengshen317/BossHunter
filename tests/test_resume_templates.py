import base64
import tempfile
import unittest
from pathlib import Path


class ResumeTemplateTests(unittest.TestCase):
    def test_modern_template_embeds_local_avatar_and_theme(self) -> None:
        from bosshunter.ai.resume_templates import build_resume_html

        with tempfile.TemporaryDirectory() as tmp:
            avatar_path = Path(tmp) / "avatar.png"
            avatar_bytes = b"\x89PNG\r\n\x1a\nportrait"
            avatar_path.write_bytes(avatar_bytes)

            html = build_resume_html(
                "# 张三\n\n## 基本信息\n\nPython 工程师",
                template_name="modern",
                avatar_path=avatar_path,
                show_avatar=True,
                theme_color="#0F766E",
            )

        encoded = base64.b64encode(avatar_bytes).decode("ascii")
        self.assertIn("resume-shell--modern", html)
        self.assertIn("--accent: #0F766E", html)
        self.assertIn(f"data:image/png;base64,{encoded}", html)
        self.assertIn('class="resume-avatar"', html)

    def test_ats_template_never_includes_avatar(self) -> None:
        from bosshunter.ai.resume_templates import build_resume_html

        with tempfile.TemporaryDirectory() as tmp:
            avatar_path = Path(tmp) / "avatar.jpg"
            avatar_path.write_bytes(b"\xff\xd8\xffportrait")

            html = build_resume_html(
                "# 张三\n\n## 工作经历\n\n经历",
                template_name="ats",
                avatar_path=avatar_path,
                show_avatar=True,
            )

        self.assertIn("resume-shell--ats", html)
        self.assertNotIn('<img class="resume-avatar"', html)
        self.assertNotIn("data:image/", html)

    def test_missing_avatar_is_optional(self) -> None:
        from bosshunter.ai.resume_templates import build_resume_html

        html = build_resume_html(
            "# 张三\n\n## 相关技能\n\nPython",
            template_name="executive",
            avatar_path=Path("missing-avatar.png"),
            show_avatar=True,
        )

        self.assertIn("resume-shell--executive", html)
        self.assertNotIn('<img class="resume-avatar"', html)

    def test_unknown_template_falls_back_to_modern(self) -> None:
        from bosshunter.ai.resume_templates import build_resume_html

        html = build_resume_html("# 张三", template_name="unknown")

        self.assertIn("resume-shell--modern", html)


class ResumeConfigTests(unittest.TestCase):
    def test_resume_presentation_defaults_are_loaded(self) -> None:
        from bosshunter.config import load_config

        config = load_config(Path("missing-config.yaml"))

        self.assertEqual(config["resume"]["template"], "modern")
        self.assertEqual(config["resume"]["theme_color"], "#0F766E")
        self.assertTrue(config["resume"]["show_avatar"])
        self.assertTrue(config["resume"]["generate_ats_version"])
