import re
from pathlib import Path

from astrbot.api import logger


def get_plugin_root() -> Path:
    """获取插件根目录。"""
    return Path(__file__).resolve().parent.parent


def get_metadata_path() -> Path:
    """获取插件 metadata.yaml 路径。"""
    return get_plugin_root() / "metadata.yaml"


def get_plugin_version(default: str = "unknown", strip_v_prefix: bool = False) -> str:
    """通过读取插件根目录中的 metadata.yaml 获取插件版本号。"""
    try:
        metadata_path = get_metadata_path()
        if metadata_path.exists():
            with open(metadata_path, encoding="utf-8") as f:
                for line in f:
                    match = re.match(r"^\s*version:\s*([^#\n]+)", line)
                    if match:
                        version = match.group(1).strip().strip('"').strip("'")
                        if strip_v_prefix:
                            version = version.lstrip("vV")
                        return version or default
        else:
            logger.debug(f"[主动消息] metadata.yaml 未找到喵: {metadata_path}")
    except Exception as e:
        logger.error(f"[主动消息] 获取插件版本失败喵: {e}")

    return default
