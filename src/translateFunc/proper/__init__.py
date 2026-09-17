"""translateFunc.proper — Proper noun utilities and JP/EN cross-validation."""
from translateFunc.proper.flat import (
    flatten_dict_enhanced,
    update_dict_with_flattened,
    get_value_by_path,
)
from translateFunc.proper.analyze import extract_contexts
from translateFunc.proper.new_terms import (
    NewTermCollector,
    load_learned,
    merge_terms,
)

__all__ = [
    "flatten_dict_enhanced",
    "update_dict_with_flattened",
    "get_value_by_path",
    "extract_contexts",
    "NewTermCollector",
    "load_learned",
    "merge_terms",
]
