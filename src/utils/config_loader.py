# src/utils/config_loader.py
import json
import os

class ConfigLoader:
    _instance = None
    _config = {}

    def __new__(cls, config_path="config.json"):
        if cls._instance is None:
            cls._instance = super(ConfigLoader, cls).__new__(cls)
            cls._instance.load_config(config_path)
        return cls._instance

    def load_config(self, config_path):
        """Load config from json file, ignoring comments"""
        import re
        if not os.path.exists(config_path):
             # Try absolute path based on this file
             base_dir = os.path.dirname(os.path.abspath(__file__)) # src/utils
             project_root = os.path.dirname(os.path.dirname(base_dir)) # project root
             config_path = os.path.join(project_root, "config.json")
        
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    # Simple comment removal: // until end of line, but be careful with http://
                    # Better regex: match strings OR comments
                    pattern = r'("[^"]*")|(\/\/.*)'
                    def replace(match):
                        if match.group(1): return match.group(1) # string
                        return "" # comment
                    
                    content = re.sub(pattern, replace, content)
                    self._config = json.loads(content)
            except Exception as e:
                print(f"Error loading config: {e}")
                self._config = {}
        else:
            print(f"Config file not found: {config_path}")
            self._config = {}

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
        if not os.path.exists(config_path):
            base_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(os.path.dirname(base_dir))
            config_path = os.path.join(project_root, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self._config, f, ensure_ascii=False, indent=2)
