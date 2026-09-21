#!/usr/bin/env python3
"""只读检查 BossHunter Web 服务是否健康。"""

from __future__ import annotations

import json
from urllib.request import urlopen


def main() -> int:
	"""访问本机健康端点并返回 Docker 健康检查退出码。"""
	try:
		with urlopen("http://127.0.0.1:8686/api/health", timeout=3) as response:  # noqa: S310 - 固定本地地址
			payload = json.load(response)
		return 0 if response.status == 200 and payload.get("status") == "ok" else 1
	except Exception:
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
