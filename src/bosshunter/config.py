"""Configuration loader for BossHunter."""

import os
from pathlib import Path
from typing import Any

import yaml

from bosshunter.city_codes import CITY_CODES as _CITY_CODES

# BOSS直聘城市编码映射
CITY_CODES = _CITY_CODES

SUPPORTED_AI_PROVIDERS = {"anthropic", "openai_compatible"}


DEFAULTS: dict[str, Any] = {
    "profile": {
        "resume_path": "./resume.md",
        "resume_output_dir": "./data/resumes",
        "target_cities": ["北京"],
        "salary_min": 0,
        "salary_max": 0,
        "allow_internship": False,
        "deal_breakers": [],
    },
    "search": {
        "keywords": [],
        "cities": [],  # Empty = fallback to profile.target_cities
        "max_pages": 3,
    },
    "scoring": {
        "threshold": 71,
        "max_candidates": 20,
        "concurrency": 3,
        # 注册资本硬性下限（万）。预筛阶段排除明确低于该值的岗位；缺失/未补全不排除。
        "min_registered_capital_wan": 50,
    },
    "throttle": {
        "daily_limit": 30,
        "interval_min": 60,
        "interval_max": 180,
		"browse_before_greet": True,
		"only_send_when_hr_online": False,
		"browse_duration_min": 15,
        "browse_duration_max": 30,
        "send_windows": ["09:00-16:00"],
        "day_off_probability": 0.05,
    },
    "ai": {
        "provider": "openai_compatible",
        "model": "deepseek-chat",
        "greeting_review_threshold": 7.0,
        "greeting_max_iterations": 2,
    },
	"assistant": {
		"intent_classifier": {
			"mode": "llm",
			"provider": "typesafe",
			"model": "jev-latest",
			"base_url": "https://api.typesafe.ai",
			"api_key_env": "TYPESAFE_API_KEY",
			"timeout_seconds": 3,
		},
		"context": {
			"default_context_window_tokens": 32768,
			"output_reserve_tokens": 4096,
			"safety_margin_ratio": 0.15,
			"recent_history_ratio": 0.35,
			"recent_turn_min": 2,
			"recent_turn_max": 12,
			"summary_max_tokens": 2048,
			"pending_digest_max_tokens": 1024,
			"compaction_batch_tokens": 8192,
			"compaction_batch_turns": 8,
		},
		"memory": {
			"capture_mode": "automatic",
			"min_confidence": 0.85,
			"retrieval_max_items": 8,
			"retrieval_max_tokens": 2048,
		},
	},
    "resume": {
        "template": "modern",
        "theme_color": "#0F766E",
        "show_avatar": True,
        "generate_ats_version": True,
    },
    "monitor": {
        "interval": 30,  # 分钟
        "chat_url": "https://www.zhipin.com/web/geek/chat",
        "max_resume_sends_per_cycle": 5,
        "auto_reply_hr_questions": False,
        "auto_generate_resume": True,  # HR 要简历时是否自动生成定制 PDF
    },
    "follow_up": {
        "enabled": False,
        "interval_hours": 48,
        "skip_weekends": True,
    },
    "dedup": {
        "history_file": "./data/history.jsonl",
    },
    "browser": {
        "runtime": "builtin",
        "proxy_host": "127.0.0.1",
        "proxy_port": 3456,
		"proxy_token": "",
        "chrome_ports": [9222, 9229, 9333],
        "preferred_browser": "auto",
        "executable_path": "",
        "user_data_dir": "./data/browser-profile",
        "auto_start_proxy": True,
        "enable_port_guard": True,
        "site_patterns": True,
    },
    "database": {
        "url": "",
    },
    "runtime": {
        "redis_url": "",
		# 单进程桌面版使用本地锁；多实例部署时显式开启 Redis。
		"redis_required": False,
        "worker_enabled": False,
        "max_concurrency": 4,
    },
    "artifacts": {
        "backend": "local",
        "local_path": "./data/artifacts",
        "bucket": "",
        "endpoint_url": "",
    },
}


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load configuration from YAML file, falling back to defaults."""
    cfg = _deep_copy_dict(DEFAULTS)
    if config_path is None:
        config_path = _default_config_path()
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        _deep_merge(cfg, user_cfg)
    _apply_environment_overrides(cfg)
    _validate_ai_provider(cfg)
    return cfg


def _default_config_path() -> Path:
	"""解析当前运行实例的默认配置文件路径。

	Returns:
		显式挂载的配置路径，或 ``BOSSHUNTER_BASE_DIR/config.yaml``。
	"""
	configured_path = os.getenv("BOSSHUNTER_CONFIG_PATH", "").strip()
	if configured_path:
		return Path(configured_path).expanduser()
	base_dir = os.getenv("BOSSHUNTER_BASE_DIR", "").strip()
	if base_dir:
		return Path(base_dir).expanduser() / "config.yaml"
	return Path("config.yaml")


def _apply_environment_overrides(config: dict[str, Any]) -> None:
	"""应用容器和进程级配置覆盖。

	环境变量是部署边界，优先级高于配置文件。只覆盖明确设置且非空的值，
	避免 Docker 模板中的空变量意外清除用户配置。
	"""
	database_url = os.getenv("BOSSHUNTER_DATABASE_URL", "").strip()
	if database_url:
		config["database"]["url"] = database_url

	browser = config["browser"]
	string_overrides = {
		"proxy_host": "BOSSHUNTER_BROWSER_PROXY_HOST",
		"proxy_token": "BOSSHUNTER_BROWSER_PROXY_TOKEN",
		"runtime": "BOSSHUNTER_BROWSER_RUNTIME",
	}
	for field, environment_name in string_overrides.items():
		value = os.getenv(environment_name, "").strip()
		if value:
			browser[field] = value

	proxy_port = os.getenv("BOSSHUNTER_BROWSER_PROXY_PORT", "").strip()
	if proxy_port:
		try:
			parsed_port = int(proxy_port)
		except ValueError as exc:
			raise ValueError("BOSSHUNTER_BROWSER_PROXY_PORT 必须是整数") from exc
		if not 1 <= parsed_port <= 65535:
			raise ValueError("BOSSHUNTER_BROWSER_PROXY_PORT 必须在 1 到 65535 之间")
		browser["proxy_port"] = parsed_port

	auto_start = os.getenv("BOSSHUNTER_BROWSER_AUTO_START_PROXY", "").strip().lower()
	if auto_start:
		if auto_start not in {"1", "0", "true", "false", "yes", "no"}:
			raise ValueError("BOSSHUNTER_BROWSER_AUTO_START_PROXY 必须是布尔值")
		browser["auto_start_proxy"] = auto_start in {"1", "true", "yes"}


def _validate_ai_provider(config: dict[str, Any]) -> None:
    """Fail fast when the configured AI provider is not supported."""
    provider = config.get("ai", {}).get("provider", "openai_compatible")
    if provider not in SUPPORTED_AI_PROVIDERS:
        raise ValueError("当前版本支持 Anthropic 或 OpenAI 兼容接口。")


def _deep_copy_dict(d: dict) -> dict:
    result = {}
    for k, v in d.items():
        if isinstance(v, dict):
            result[k] = _deep_copy_dict(v)
        elif isinstance(v, list):
            result[k] = v[:]
        else:
            result[k] = v
    return result


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge override into base."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
