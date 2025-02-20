import torch
import intel_extension_for_pytorch  # noqa
from functools import wraps

from torch.overrides import (
    handle_torch_function,
    has_torch_function,
    get_overridable_functions,
    get_testing_overrides,
    is_tensor_method_or_property,
    TorchFunctionMode,
)

__all__ = [
    "handle_torch_function",
    "has_torch_function",
    "get_overridable_functions",
    "get_testing_overrides",
    "is_tensor_method_or_property",
]

import functools  # noqa
from functools import partial

# The dispatch table for tensor factory's __torch_function__ implementation.
HANDLED_FUNCTIONS_SUB = {}

DEFAULT_XPU_DEVICE = "xpu"
DEFAULT_DTYPE = torch.float


def implements_sub(torch_function):
    "Register a torch function override for SubTensor"
    HANDLED_FUNCTIONS_SUB[torch_function] = partial(
        torch_function, device=DEFAULT_XPU_DEVICE, dtype=DEFAULT_DTYPE
    )


implements_sub(torch.empty)


def set_default_tensor_type(tensor_type):
    class XPUDefaultTensorTypeMode(TorchFunctionMode):
        def __init__(self, tensor_type):
            if tensor_type is torch.xpu.FloatTensor:
                self.dtype = torch.float
            if tensor_type is torch.xpu.DoubleTensor:
                self.dtype = torch.float64

        def __torch_function__(self, func, types, args=(), kwargs=None):
            if kwargs is None:
                kwargs = {}

            if func in HANDLED_FUNCTIONS_SUB:
                return partial(func, device="xpu", dtype=self.dtype)(*args, **kwargs)

            return func(*args, **kwargs)

    if tensor_type in [torch.xpu.FloatTensor, torch.xpu.DoubleTensor]:
        mode_info = torch.overrides._TorchFunctionModeInfo()

        old = mode_info.get_mode()
        if old is None:
            inner = mode_info.base_mode_class(inner=None)
        else:
            inner = old

        mode = partial(XPUDefaultTensorTypeMode, tensor_type)(inner=inner)
        mode_info.set_mode(mode)


def enable_cl_to():
    class XPUDefaultTensorTypeMode(TorchFunctionMode):
        def __init__(self):
            pass

        def __torch_function__(self, func, types, args=(), kwargs=None):
            if kwargs is None:
                kwargs = {}

            if func in [torch.Tensor.to]:
                if "memory_format" in kwargs:
                    if kwargs["memory_format"] is torch.channels_last:
                        # This is very hacking code for PoC
                        if args[0].dim == 4:
                            # Error: Correct this for moving the 2nd dim to the last one
                            return (
                                args[0].transpose(1, -1).contiguous().transpose(1, -1)
                            )
                    if kwargs["memory_format"] is torch.channels_last_3d:
                        pass
            return func(*args, **kwargs)

    mode_info = torch.overrides._TorchFunctionModeInfo()

    old = mode_info.get_mode()
    if old is None:
        inner = mode_info.base_mode_class(inner=None)
    else:
        inner = old

    mode = partial(XPUDefaultTensorTypeMode)(inner=inner)
    mode_info.set_mode(mode)


def fp64_tensor_totype_wrapper(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        for arg in args:
            if torch.is_tensor(arg) and arg.is_xpu:
                return arg.to(torch.float)
        for k, kwarg in kwargs.items():
            if torch.is_tensor(kwarg) and kwarg.is_xpu:
                return kwarg.to(torch.float)
        return f(*args, **kwargs)

    return wrapper


def override_tensor_totype():
    r"""Override _tensor_totype to avoid triggering fp64 error when printing XPU tensor on ATS-M"""
    torch._tensor_str.tensor_totype = fp64_tensor_totype_wrapper(
        torch._tensor_str.tensor_totype
    )


def args_to_xpu(args):
    if torch.is_tensor(args) and args.is_xpu:
        return args.to("cpu")
    elif isinstance(args, (tuple, list)):
        args_list = list(args)
        for i, arg in enumerate(args_list):
            args_list[i] = args_to_xpu(arg)
        if isinstance(args, tuple):
            return tuple(args_list)
        else:
            return args_list
    else:
        return args


def fp64_assert_equal_wrapper(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        args = args_to_xpu(args)
        return f(*args, **kwargs)

    return wrapper


def override_assert_equal():
    r"""Override assertEqual to avoid triggering fp64 error on tensor comparison in test case"""
    override_disable_global_flags()
    from torch.testing._internal.common_utils import TestCase

    TestCase.assertEqual = fp64_assert_equal_wrapper(TestCase.assertEqual)


def _disable_global_flags():
    pass


def override_disable_global_flags():
    r"""
    In PyTorch design, `__allow_nonbracketed_mutation_flag` is a flag to forbid bare assignment
    to torch.backends.<cudnn|mkldnn>.enabled and friends when running test suite. This flag will
    be forced to set to False by function `disable_global_flags` which is defined in
    torch.testing._internal.common_utils when overriding TestCase.assertEqual. It may result in
    a runtime error on subsequent cudnn|mkldnn setting, if any. The function here is to override
    `disable_global_flags` with an empty one to keep the flag `__allow_nonbracketed_mutation_flag`
    from being changed.
    """
    torch.backends.disable_global_flags = _disable_global_flags
