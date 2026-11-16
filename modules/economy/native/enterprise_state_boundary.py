"""Native/fallback binding for enterprise planning writes."""
import importlib


def apply_outlook_python(enterprise, outlook):
    enterprise.update(outlook)
    return enterprise


try:
    _kernel = importlib.import_module('.enterprise_state_kernel', __package__)
except (ImportError, ModuleNotFoundError):
    _kernel = None

apply_outlook = _kernel.apply_outlook if _kernel is not None else apply_outlook_python
NATIVE_ENTERPRISE_STATE = _kernel is not None
