# src/utils/config_loader.py
import json
import os

class ConfigLoader:
    _instance = None
    _config = {}
    _default_private_override_paths = {
        "data_provider.tushare_token",
        "data_provider.default_api_key",
        "data_provider.llm_api_key",
        "data_provider.strategy_llm_api_key",
        "data_provider.api_key",
        "data_provider.mysql_password",
        "data_provider.postgres_password",
        "data_provider.default_api_url",
        "webhook_notification.feishu_webhook_url",
        "webhook_notification.feishu_secret",
        "evolution.llm.api_key",
    }
    _default_private_passthrough_paths = {
        "targets",
        "strategies.active_ids",
    }

    def __new__(cls, config_path="config.json"):
        if cls._instance is None:
            cls._instance = super(ConfigLoader, cls).__new__(cls)
            cls._instance.load_config(config_path)
        return cls._instance

    def load_config(self, config_path):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(base_dir))
        base_config_path = config_path if os.path.exists(config_path) else os.path.join(project_root, "config.json")
        base_config = self._load_json_config(base_config_path)
        private_config_path = str(
            os.environ.get("CONFIG_PRIVATE_PATH", "")
            or self._get_path_value(base_config, "system.private_config_path", "")
            or os.path.join(project_root, "config.private.json")
        ).strip()
        if private_config_path and (not os.path.isabs(private_config_path)):
            private_config_path = os.path.join(project_root, private_config_path)
        private_config_raw = self._load_json_config(private_config_path, silent=True)
        private_override_paths = self.resolve_private_override_paths(base_config)
        private_config = self._filter_private_override_config(private_config_raw, private_override_paths)
        private_passthrough = self._extract_private_passthrough_config(private_config_raw)
        merged_private = self._deep_merge_dict(private_config, private_passthrough)
        self._config = self._deep_merge_dict(base_config, merged_private)

    def _load_json_config(self, config_path, silent=False):
        import re
        if not os.path.exists(config_path):
            if not silent:
                print(f"Config file not found: {config_path}")
            return {}
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
                pattern = r'("[^"]*")|(\/\/.*)'
                def replace(match):
                    if match.group(1):
                        return match.group(1)
                    return ""
                content = re.sub(pattern, replace, content)
                return json.loads(content)
        except Exception as e:
            if not silent:
                print(f"Error loading config: {e}")
            return {}

    def _deep_merge_dict(self, base, override):
        if not isinstance(base, dict):
            return override if override is not None else base
        if not isinstance(override, dict):
            return base
        merged = dict(base)
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = self._deep_merge_dict(merged[k], v)
            else:
                merged[k] = v
        return merged

    def _path_exists(self, payload, path):
        if not isinstance(payload, dict):
            return False
        cur = payload
        for key in str(path).split('.'):
            if not isinstance(cur, dict) or key not in cur:
                return False
            cur = cur.get(key)
        return True

    def _get_path_value(self, payload, path, default=None):
        if not isinstance(payload, dict):
            return default
        cur = payload
        for key in str(path).split('.'):
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur.get(key)
        return cur

    def _set_path_value(self, payload, path, value):
        if not isinstance(payload, dict):
            return
        keys = str(path).split('.')
        cur = payload
        for key in keys[:-1]:
            nxt = cur.get(key)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[key] = nxt
            cur = nxt
        cur[keys[-1]] = value

    def _delete_path_value(self, payload, path):
        if not isinstance(payload, dict):
            return
        keys = str(path).split('.')
        chain = []
        cur = payload
        for key in keys:
            if not isinstance(cur, dict) or key not in cur:
                return
            chain.append((cur, key))
            cur = cur.get(key)
        parent, last_key = chain[-1]
        parent.pop(last_key, None)
        for parent, key in reversed(chain[:-1]):
            child = parent.get(key)
            if isinstance(child, dict) and not child:
                parent.pop(key, None)
            else:
                break

    def _project_root(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.dirname(os.path.dirname(base_dir))

    def _private_config_path(self, payload=None):
        override = str(os.environ.get("CONFIG_PRIVATE_PATH", "") or "").strip()
        if override:
            return override
        cfg_override = str(self._get_path_value(payload or {}, "system.private_config_path", "") or "").strip()
        if cfg_override:
            return cfg_override if os.path.isabs(cfg_override) else os.path.join(self._project_root(), cfg_override)
        return os.path.join(self._project_root(), "config.private.json")

    def _write_json_file(self, file_path, payload):
        folder = os.path.dirname(file_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    @classmethod
    def resolve_private_override_paths(cls, payload=None):
        env_value = str(os.environ.get("CONFIG_PRIVATE_OVERRIDE_PATHS", "") or "").strip()
        if env_value:
            return {p.strip() for p in env_value.split(",") if p and p.strip()}
        cfg_value = []
        if isinstance(payload, dict):
            cur = payload
            ok = True
            for key in "system.private_override_paths".split('.'):
                if not isinstance(cur, dict) or key not in cur:
                    ok = False
                    break
                cur = cur.get(key)
            if ok:
                cfg_value = cur
        if isinstance(cfg_value, list):
            normalized = {str(p or "").strip() for p in cfg_value}
            normalized = {p for p in normalized if p}
            if normalized:
                return normalized
        if isinstance(cfg_value, str) and cfg_value.strip():
            return {p.strip() for p in cfg_value.split(",") if p and p.strip()}
        return set(cls._default_private_override_paths)

    def _filter_private_override_config(self, payload, paths):
        if not isinstance(payload, dict):
            return {}
        filtered = {}
        for path in paths:
            if self._path_exists(payload, path):
                self._set_path_value(filtered, path, self._get_path_value(payload, path, ""))
        return filtered

    def _extract_private_passthrough_config(self, payload):
        if not isinstance(payload, dict):
            return {}
        out = {}
        for path in self._default_private_passthrough_paths:
            if self._path_exists(payload, path):
                value = self._get_path_value(payload, path, None)
                if self._is_effective_private_passthrough_value(value):
                    self._set_path_value(out, path, value)
        return out

    def _is_effective_private_passthrough_value(self, value):
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict, tuple, set)):
            return len(value) > 0
        return True

    def get(self, key, default=None):
        keys = key.split('.')
        value = self._config
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default

    @classmethod
    def reload(cls, config_path="config.json"):
        """Force reload the config from disk"""
        cls._instance = None
        return cls(config_path)

    def set(self, key, value):
        """
        Set a config value by dot notation key (e.g. "data_provider.source")
        """
        keys = key.split('.')
        current = self._config
        for k in keys[:-1]:
            current = current.setdefault(k, {})
        current[keys[-1]] = value

    def to_dict(self):
        return json.loads(json.dumps(self._config, ensure_ascii=False))

    def save(self, config_path="config.json"):
        project_root = self._project_root()
        target_path = config_path if os.path.isabs(config_path) else os.path.join(project_root, config_path)
        full_cfg = self.to_dict()
        secret_paths = self.resolve_private_override_paths(full_cfg)
        private_only_paths = set(self._default_private_passthrough_paths)
        private_path = self._private_config_path(full_cfg)
        private_exists = os.path.exists(private_path)
        existing_public_cfg = self._load_json_config(target_path, silent=True)
        if not isinstance(existing_public_cfg, dict):
            existing_public_cfg = {}

        public_cfg = json.loads(json.dumps(full_cfg, ensure_ascii=False))
        for path in secret_paths:
            if self._path_exists(public_cfg, path):
                self._set_path_value(public_cfg, path, "")
        for path in private_only_paths:
            if private_exists:
                if self._path_exists(existing_public_cfg, path):
                    self._set_path_value(public_cfg, path, self._get_path_value(existing_public_cfg, path, None))
                else:
                    self._delete_path_value(public_cfg, path)
                continue

        self._write_json_file(target_path, public_cfg)

        private_cfg = self._load_json_config(private_path, silent=True)
        if not isinstance(private_cfg, dict):
            private_cfg = {}
        private_changed = False

        for path in secret_paths:
            val = self._get_path_value(full_cfg, path, "")
            text = str(val or "")
            old_val = self._get_path_value(private_cfg, path, "")
            # Private config uses incremental upsert only.
            # Empty public/masked values should not clear existing private values.
            if not text.strip():
                continue
            if str(old_val) != text:
                self._set_path_value(private_cfg, path, text)
                private_changed = True

        for path in private_only_paths:
            if not private_exists:
                continue
            val = self._get_path_value(full_cfg, path, None)
            old_val = self._get_path_value(private_cfg, path, None)
            if val is None:
                # Missing value means no update, not deletion.
                continue
            if old_val != val:
                self._set_path_value(private_cfg, path, val)
                private_changed = True

        if private_changed:
            self._write_json_file(private_path, private_cfg)
