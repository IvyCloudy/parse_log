"""统一配置加载。

优先级（高 -> 低）：
    1. 环境变量（LOG_SOURCE / LOG_API_URL / LOG_API_TOKEN / LOG_FILE / LOG_SERVICES / CONFIG_FILE）
    2. 配置文件 config.yaml（路径可用环境变量 CONFIG_FILE 覆盖，默认项目根 config.yaml）
    3. 代码内默认值

配置文件示例见 config.yaml。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

try:
    import yaml  # 配置文件使用 yaml；未安装时自动降级为仅环境变量
except ImportError:  # pragma: no cover
    yaml = None


# 单一默认值来源：Settings 与 pick() 均引用此处，避免代码与 yaml 语义漂移（问题 #2）。
_DEFAULTS = dict(
    log_source="file",
    log_api_url="",
    log_api_token="",
    log_file="",
    log_services="",
)


@dataclass
class Settings:
    # 数据源类型：mock / file / http
    # 默认值与 config.yaml 对齐（file 本地可落地，无需凭据即可演示）；
    # http 需 LOG_API_URL 等凭据，不应作为无配置时的默认行为。
    log_source: str = _DEFAULTS["log_source"]
    # http 数据源
    log_api_url: str = _DEFAULTS["log_api_url"]
    log_api_token: str = _DEFAULTS["log_api_token"]
    # file 数据源
    log_file: str = _DEFAULTS["log_file"]
    # 显式指定的服务单元列表（serviceUinitId），逗号分隔；空=依赖服务发现
    log_services: str = _DEFAULTS["log_services"]

    def services_list(self) -> Optional[List[str]]:
        s = self.log_services.strip()
        if not s:
            return None
        return [x.strip() for x in s.split(",") if x.strip()]


def _load_yaml(path: str) -> dict:
    if yaml is None:
        return {}
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    # 支持顶层 data_source: 或 log_source: 两种写法
    norm: dict = {}
    if isinstance(data, dict):
        norm["log_source"] = data.get("log_source", data.get("data_source", ""))
        ds = data.get("data_source", {}) if isinstance(data.get("data_source"), dict) else {}
        norm["log_api_url"] = ds.get("api_url", data.get("log_api_url", ""))
        norm["log_api_token"] = ds.get("api_token", data.get("log_api_token", ""))
        norm["log_file"] = ds.get("file", data.get("log_file", ""))
        norm["log_services"] = ds.get("services", data.get("log_services", ""))
    # 去掉空串，保留非空
    return {k: v for k, v in norm.items() if v not in (None, "")}


def load_settings(config_path: Optional[str] = None) -> Settings:
    config_path = config_path or os.getenv("CONFIG_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml"
    )
    file_cfg = _load_yaml(config_path)

    def pick(*keys: str) -> str:
        # 环境变量优先 -> 配置文件 -> 默认
        for k in keys:
            env = os.getenv(k)
            if env not in (None, ""):
                return env
        for k in keys:
            if k in file_cfg and file_cfg[k] not in (None, ""):
                return str(file_cfg[k])
        return _DEFAULTS.get(keys[-1].lower(), "")

    # 兼容历史环境变量名：LOG_SOURCE / LOG_API_URL / LOG_API_TOKEN / LOG_FILE / LOG_SERVICES
    return Settings(
        log_source=pick("LOG_SOURCE", "log_source"),
        log_api_url=pick("LOG_API_URL", "log_api_url"),
        log_api_token=pick("LOG_API_TOKEN", "log_api_token"),
        log_file=pick("LOG_FILE", "log_file"),
        log_services=pick("LOG_SERVICES", "log_services"),
    )


# 模块加载时即解析（服务启动期一次）
settings = load_settings()
