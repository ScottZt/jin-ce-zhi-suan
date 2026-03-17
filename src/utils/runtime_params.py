from src.utils.config_loader import ConfigLoader

def get_value(path, default=None, reload_config=True):
    cfg = ConfigLoader.reload() if reload_config else ConfigLoader()
    return cfg.get(path, default)
