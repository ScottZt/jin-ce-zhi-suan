# src/strategies/strategy_factory.py
from src.strategies.implemented_strategies import (
    Strategy00, Strategy01, Strategy02, Strategy03, Strategy04, Strategy05,
    Strategy06, Strategy07, Strategy08, Strategy09
)
from src.strategies.my_strategy import MyStrategy
from src.strategies.strategy_checkmate import StrategyCM
from src.strategies.strategy_manager_repo import (
    load_custom_strategies,
    load_disabled_ids,
    load_deleted_ids,
    instantiate_custom_strategy
)
from src.utils.config_loader import ConfigLoader

def create_strategies(apply_active_filter=True):
    """
    Generate the active stock strategies.
    """
    strategies = [
        Strategy00(),
        Strategy01(),
        Strategy02(),
        Strategy03(),
        Strategy04(),
        Strategy05(),
        Strategy06(),
        Strategy07(),
        Strategy08(),
        Strategy09(),
        StrategyCM(),  # Checkmate 将死趋势回调策略
        MyStrategy()
    ]
    builtin_ids = {str(s.id).strip() for s in strategies}
    disabled_ids = load_disabled_ids()
    deleted_ids = load_deleted_ids()
    if deleted_ids:
        strategies = [s for s in strategies if str(s.id) not in deleted_ids]
    custom_rows = load_custom_strategies()
    for row in custom_rows:
        sid = str(row.get("id", "")).strip()
        if sid and sid in builtin_ids:
            continue
        if sid and sid in deleted_ids:
            continue
        if sid and sid in disabled_ids:
            continue
        try:
            strategy = instantiate_custom_strategy(row)
            if strategy is not None:
                strategies.append(strategy)
        except Exception:
            continue
    if disabled_ids:
        strategies = [s for s in strategies if str(s.id) not in disabled_ids]
    if apply_active_filter:
        strategies_before_filter = list(strategies)
        cfg = ConfigLoader.reload()
        active_ids = cfg.get("strategies.active_ids", [])
        if isinstance(active_ids, list):
            active = {str(x).strip() for x in active_ids if str(x).strip()}
            if active:
                strategies = [s for s in strategies if str(s.id).strip() in active]
                if not strategies:
                    strategies = strategies_before_filter
    return strategies
