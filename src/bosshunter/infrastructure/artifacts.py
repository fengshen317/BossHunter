"""Agent 制品存储抽象及本地/S3 实现。"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, TypedDict


class ArtifactReference(TypedDict):
	"""数据库和 Agent 状态中保存的轻量制品引用。"""

	id: str
	storage_key: str
	content_type: str
	size: int
	checksum: str


class ArtifactStore(Protocol):
	"""可替换的二进制制品存储协议。"""

	def put(self, key: str, content: bytes, content_type: str | None = None) -> ArtifactReference:
		"""保存内容并返回引用。"""

	def get(self, key: str) -> bytes:
		"""读取内容。"""

	def exists(self, key: str) -> bool:
		"""判断制品是否存在。"""


class LocalArtifactStore:
	"""限制在指定根目录内的本地制品存储。"""

	def __init__(self, root: Path) -> None:
		"""创建制品根目录。"""
		self.root = root.resolve()
		self.root.mkdir(parents=True, exist_ok=True)

	def _path(self, key: str) -> Path:
		"""解析安全路径，阻止绝对路径和目录穿越。"""
		posix = PurePosixPath(key.replace("\\", "/"))
		if posix.is_absolute() or ".." in posix.parts or not posix.parts:
			raise ValueError("非法制品路径")
		path = self.root.joinpath(*posix.parts).resolve()
		if self.root not in path.parents:
			raise ValueError("制品路径越界")
		return path

	def put(self, key: str, content: bytes, content_type: str | None = None) -> ArtifactReference:
		"""原子覆盖单个制品并返回校验信息。"""
		path = self._path(key)
		path.parent.mkdir(parents=True, exist_ok=True)
		temporary = path.with_suffix(f"{path.suffix}.tmp")
		temporary.write_bytes(content)
		temporary.replace(path)
		checksum = hashlib.sha256(content).hexdigest()
		return {
			"id": checksum,
			"storage_key": key,
			"content_type": content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
			"size": len(content),
			"checksum": checksum,
		}

	def get(self, key: str) -> bytes:
		"""读取受根目录约束的制品。"""
		return self._path(key).read_bytes()

	def exists(self, key: str) -> bool:
		"""判断制品是否存在。"""
		return self._path(key).is_file()


class S3ArtifactStore:
	"""兼容 AWS S3、MinIO、OSS S3 网关的制品存储。"""

	def __init__(self, bucket: str, **client_options: Any) -> None:
		"""延迟使用 boto3 创建客户端。"""
		import boto3

		self.bucket = bucket
		self.client = boto3.client("s3", **client_options)

	def put(self, key: str, content: bytes, content_type: str | None = None) -> ArtifactReference:
		"""上传私有对象并返回内容寻址引用。"""
		_normalize_key(key)
		media_type = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
		self.client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=media_type)
		checksum = hashlib.sha256(content).hexdigest()
		return {"id": checksum, "storage_key": key, "content_type": media_type, "size": len(content), "checksum": checksum}

	def get(self, key: str) -> bytes:
		"""读取私有对象。"""
		_normalize_key(key)
		return bytes(self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read())

	def exists(self, key: str) -> bool:
		"""通过 HEAD 请求判断对象是否存在。"""
		_normalize_key(key)
		try:
			self.client.head_object(Bucket=self.bucket, Key=key)
			return True
		except self.client.exceptions.ClientError:
			return False


def _normalize_key(key: str) -> str:
	"""校验对象存储 Key。"""
	posix = PurePosixPath(key.replace("\\", "/"))
	if posix.is_absolute() or ".." in posix.parts or not posix.parts:
		raise ValueError("非法制品路径")
	return str(posix)


def create_artifact_store(config: dict[str, Any], base_dir: Path) -> ArtifactStore:
	"""根据配置创建本地或 S3/MinIO 制品存储。"""
	settings = config.get("artifacts", {}) if isinstance(config, dict) else {}
	if settings.get("backend") == "s3":
		bucket = str(settings.get("bucket") or "").strip()
		if not bucket:
			raise ValueError("S3 制品存储缺少 bucket")
		options = {
			"endpoint_url": settings.get("endpoint_url") or None,
			"aws_access_key_id": os.environ.get("BOSSHUNTER_S3_ACCESS_KEY"),
			"aws_secret_access_key": os.environ.get("BOSSHUNTER_S3_SECRET_KEY"),
			"region_name": settings.get("region") or None,
		}
		return S3ArtifactStore(bucket, **{key: value for key, value in options.items() if value})
	root = Path(str(settings.get("local_path") or base_dir / "data" / "artifacts"))
	return LocalArtifactStore(root if root.is_absolute() else base_dir / root)


__all__ = ["ArtifactReference", "ArtifactStore", "LocalArtifactStore", "S3ArtifactStore", "create_artifact_store"]
