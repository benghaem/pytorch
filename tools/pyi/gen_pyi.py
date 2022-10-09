import argparse
import collections
from pprint import pformat
from typing import Dict, List, Sequence

from torchgen.api.python import (
    PythonSignatureGroup,
    PythonSignatureNativeFunctionPair,
    returns_named_tuple_pyi,
)
from torchgen.gen import parse_native_yaml

from torchgen.model import DispatchKey, Variant
from torchgen.utils import FileManager

from tools.autograd.gen_python_functions import (
    group_overloads,
    load_signatures,
    should_generate_py_binding,
)

"""
This module implements generation of type stubs for PyTorch,
enabling use of autocomplete in IDEs like PyCharm, which otherwise
don't understand C extension modules.

At the moment, this module only handles type stubs for torch and
torch.Tensor.  It should eventually be expanded to cover all functions
which come are autogenerated.

Here's our general strategy:

- We start off with a hand-written __init__.pyi.in file.  This
  file contains type definitions for everything we cannot automatically
  generate, including pure Python definitions directly in __init__.py
  (the latter case should be pretty rare).

- We go through automatically bound functions based on the
  type information recorded in native_functions.yaml and
  generate type hints for them (generate_type_hints)

There are a number of type hints which we've special-cased;
read gen_pyi for the gory details.
"""


def get_py_torch_functions(
    python_funcs: Sequence[PythonSignatureNativeFunctionPair],
    method: bool = False,
) -> Sequence[PythonSignatureGroup]:
    """
    Get declarations (grouped by name) which should be generated
    as either functions in the "torch" module or methods on Tensor.
    """

    def should_bind_function(python_func: PythonSignatureNativeFunctionPair) -> bool:
        return (
            should_generate_py_binding(python_func.function)
            and not python_func.function.python_module
            and Variant.function in python_func.function.variants
        )

    def should_bind_method(python_func: PythonSignatureNativeFunctionPair) -> bool:
        return (
            should_generate_py_binding(python_func.function)
            and not python_func.function.python_module
            and Variant.method in python_func.function.variants
        )

    should_bind = should_bind_method if method else should_bind_function
    return group_overloads([f for f in python_funcs if should_bind(f)])


# TODO: Consider defining some aliases for our Union[...] types, to make
# the stubs to read on the human eye.

DEVICE_PARAM = "device: Device=None"
FACTORY_PARAMS = (
    f"dtype: Optional[_dtype]=None, {DEVICE_PARAM}, requires_grad: _bool=False"
)

# this could be more precise w.r.t list contents etc. How to do Ellipsis?
INDICES = "indices: Union[None, _int, slice, Tensor, List, Tuple]"

blocklist = [
    "__init_subclass__",
    "__new__",
    "__subclasshook__",
    "cdist",
    "device",
    "grad",
    "requires_grad",
    "range",
    # defined in functional
    "einsum",
    # reduction argument; these bindings don't make sense
    "binary_cross_entropy_with_logits",
    "ctc_loss",
    "cosine_embedding_loss",
    "hinge_embedding_loss",
    "kl_div",
    "margin_ranking_loss",
    "triplet_margin_loss",
    # Somehow, these are defined in both _C and in functional. Ick!
    "broadcast_tensors",
    # Manually define named tensor type stubs in __init__.pyi.in
    "align_tensors",
    "meshgrid",
    "cartesian_prod",
    "block_diag",
    "norm",
    "chain_matmul",
    "stft",
    "tensordot",
    "split",
    "unique_consecutive",
    "atleast_1d",
    "atleast_2d",
    "atleast_3d",
    # These are handled specially by python_arg_parser.cpp
    "add",
    "add_",
    "add_out",
    "sub",
    "sub_",
    "sub_out",
    "mul",
    "mul_",
    "mul_out",
    "div",
    "div_",
    "div_out",
    "true_divide",
    "true_divide_",
    "true_divide_out",
    "floor_divide",
    "floor_divide_",
    "floor_divide_out",
    "to",
    "_to_copy",
    "copy_",
]

binary_ops = (
    "add",
    "sub",
    "mul",
    "div",
    "pow",
    "lshift",
    "rshift",
    "mod",
    "truediv",
    "matmul",
    "floordiv",
    "radd",
    "rsub",
    "rmul",
    "rtruediv",
    "rfloordiv",
    "rpow",  # reverse arithmetic
    "and",
    "or",
    "xor",
    "rand",
    "ror",
    "rxor",  # logic
    "iadd",
    "iand",
    "idiv",
    "ilshift",
    "imul",
    "ior",
    "irshift",
    "isub",
    "ixor",
    "ifloordiv",
    "imod",  # inplace ops
)
symmetric_comparison_ops = ("eq", "ne")
asymmetric_comparison_ops = ("ge", "gt", "lt", "le")
comparison_ops = symmetric_comparison_ops + asymmetric_comparison_ops

unary_ops = ("neg", "abs", "invert")
to_py_type_ops = ("bool", "float", "complex", "long", "index", "int", "nonzero")
all_ops = binary_ops + comparison_ops + unary_ops + to_py_type_ops


def sig_for_ops(opname: str) -> List[str]:
    """sig_for_ops(opname : str) -> List[str]

    Returns signatures for operator special functions (__add__ etc.)"""

    # we have to do this by hand, because they are hand-bound in Python

    assert opname.endswith("__") and opname.startswith("__"), "Unexpected op {}".format(
        opname
    )

    name = opname[2:-2]
    if name in binary_ops:
        return ["def {}(self, other: Any) -> Tensor: ...".format(opname)]
    elif name in comparison_ops:
        sig = "def {}(self, other: Any) -> Tensor: ...".format(opname)
        if name in symmetric_comparison_ops:
            # unsafe override https://github.com/python/mypy/issues/5704
            sig += "  # type: ignore[override]"
        return [sig]
    elif name in unary_ops:
        return ["def {}(self) -> Tensor: ...".format(opname)]
    elif name in to_py_type_ops:
        if name in {"bool", "float", "complex"}:
            tname = name
        elif name == "nonzero":
            tname = "bool"
        else:
            tname = "int"
        if tname in {"float", "int", "bool", "complex"}:
            tname = "builtins." + tname
        return ["def {}(self) -> {}: ...".format(opname, tname)]
    else:
        raise Exception("unknown op", opname)


def generate_type_hints(sig_group: PythonSignatureGroup) -> List[str]:
    type_hints: List[str] = []

    # Some deprecated ops that are on the blocklist are still included in pyi
    if sig_group.signature.name in blocklist and not sig_group.signature.deprecated:
        return type_hints

    # deprecated signatures have separate entries for their functional and out variants
    # (as opposed to the native ops, which fuse the two into a single signature).
    # generate the functional variant here, if an out variant exists.
    if sig_group.signature.deprecated and sig_group.outplace is not None:
        type_hint = sig_group.signature.signature_str_pyi(skip_outputs=True)
        type_hints.append(type_hint)

    # PythonSignatureGroups that have both a functional + out variant get a single signature, with an optional out argument
    # Generates the out variant if one exists. Otherwise, generate the functional variant
    type_hint = sig_group.signature.signature_str_pyi(
        skip_outputs=sig_group.outplace is None
    )
    type_hints.append(type_hint)

    # Some operators also additionally have a vararg variant of their signature
    type_hint_vararg = sig_group.signature.signature_str_pyi_vararg(
        skip_outputs=sig_group.outplace is None
    )
    if type_hint_vararg:
        type_hints.append(type_hint_vararg)

    return type_hints


def gen_nn_functional(fm: FileManager) -> None:
    # Functions imported into `torch.nn.functional` from `torch`, perhaps being filtered
    # through an `_add_docstr` call
    imports = [
        "conv1d",
        "conv2d",
        "conv3d",
        "conv_transpose1d",
        "conv_transpose2d",
        "conv_transpose3d",
        "conv_tbc",
        "avg_pool1d",
        "relu_",
        "selu_",
        "celu_",
        "rrelu_",
        "pixel_shuffle",
        "pixel_unshuffle",
        "channel_shuffle",
        "native_channel_shuffle",
        "pdist",
        "cosine_similarity",
    ]
    # Functions generated by `torch._jit_internal.boolean_dispatch`
    dispatches = [
        "fractional_max_pool2d",
        "fractional_max_pool3d",
        "max_pool1d",
        "max_pool2d",
        "max_pool3d",
        "adaptive_max_pool1d",
        "adaptive_max_pool2d",
        "adaptive_max_pool3d",
    ]
    # Functions directly imported from `torch._C`
    from_c = [
        "avg_pool2d",
        "avg_pool3d",
        "hardtanh_",
        "elu_",
        "leaky_relu_",
        "logsigmoid",
        "softplus",
        "softshrink",
        "one_hot",
    ]
    import_code = ["from .. import {0} as {0}".format(_) for _ in imports]
    # TODO make these types more precise
    dispatch_code = ["{}: Callable".format(_) for _ in (dispatches + from_c)]
    fm.write_with_template(
        "torch/nn/functional.pyi",
        "torch/nn/functional.pyi.in",
        lambda: {
            "imported_hints": import_code,
            "dispatched_hints": dispatch_code,
        },
    )

    # functional.pyi already contains the definitions for those functions
    # so, we don't export then to it
    from_c.extend(["hardtanh", "leaky_relu", "hardsigmoid"])
    dispatch_code = ["{}: Callable".format(_) for _ in (dispatches + from_c)]
    fm.write_with_template(
        "torch/_C/_nn.pyi",
        "torch/_C/_nn.pyi.in",
        lambda: {
            "imported_hints": import_code,
            "dispatched_hints": dispatch_code,
        },
    )


def gen_pyi(
    native_yaml_path: str,
    tags_yaml_path: str,
    deprecated_yaml_path: str,
    fm: FileManager,
) -> None:
    """gen_pyi()

    This function generates a pyi file for torch.
    """

    # Some of this logic overlaps with generate_python_signature in
    # tools/autograd/gen_python_functions.py; however, this
    # function is all about generating mypy type signatures, whereas
    # the other function generates are custom format for argument
    # checking.  If you are update this, consider if your change
    # also needs to update the other file.

    # Dictionary for NamedTuple definitions
    namedtuples: Dict[str, str] = {}

    # Generate type signatures for top-level functions
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    unsorted_function_hints: Dict[str, List[str]] = collections.defaultdict(list)

    for n, n1, n2 in [
        ("csr", "crow", "col"),
        ("csc", "ccol", "row"),
        ("bsr", "crow", "col"),
        ("bsc", "ccol", "row"),
    ]:
        unsorted_function_hints.update(
            {
                f"sparse_{n}_tensor": [
                    f"def sparse_{n}_tensor({n1}_indices: Union[Tensor, List],"
                    f"{n2}_indices: Union[Tensor, List],"
                    " values: Union[Tensor, List], size: Optional[_size]=None,"
                    " *, dtype: Optional[_dtype]=None,"
                    " device: Union[_device, str, None]=None, requires_grad:_bool=False) -> Tensor: ..."
                ],
                f"_sparse_{n}_tensor_unsafe": [
                    f"def _sparse_{n}_tensor_unsafe({n1}_indices: Union[Tensor, List],"
                    f"{n2}_indices: Union[Tensor, List],"
                    " values: Union[Tensor, List], size: List[int],"
                    " dtype: Optional[_dtype] = None, device: Optional[_device] = None,"
                    " requires_grad: bool = False) -> Tensor: ..."
                ],
            }
        )

    unsorted_function_hints.update(
        {
            "set_flush_denormal": ["def set_flush_denormal(mode: _bool) -> _bool: ..."],
            "get_default_dtype": ["def get_default_dtype() -> _dtype: ..."],
            "asarray": [
                "def asarray(obj: Any, *, dtype: Optional[_dtype]=None, "
                "device: Union[_device, str, None]=None, copy: Optional[_bool]=None, "
                "requires_grad: _bool=False) -> Tensor: ..."
            ],
            "from_numpy": ["def from_numpy(ndarray) -> Tensor: ..."],
            "frombuffer": [
                "def frombuffer(buffer: Any, *, dtype: _dtype, count: int=-1, "
                "offset: int=0, device: Union[_device, str, None]=None, "
                "requires_grad: _bool=False) -> Tensor: ..."
            ],
            "numel": ["def numel(self: Tensor) -> _int: ..."],
            "as_tensor": [
                f"def as_tensor(data: Any, dtype: Optional[_dtype]=None, {DEVICE_PARAM}) -> Tensor: ..."
            ],
            "get_num_threads": ["def get_num_threads() -> _int: ..."],
            "set_num_threads": ["def set_num_threads(num: _int) -> None: ..."],
            "init_num_threads": ["def init_num_threads() -> None: ..."],
            "get_num_interop_threads": ["def get_num_interop_threads() -> _int: ..."],
            "set_num_interop_threads": [
                "def set_num_interop_threads(num: _int) -> None: ..."
            ],
            # These functions are explicitly disabled by
            # SKIP_PYTHON_BINDINGS because they are hand bound.
            # Correspondingly, we must hand-write their signatures.
            "tensor": [
                "def tensor(data: Any, {}) -> Tensor: ...".format(FACTORY_PARAMS)
            ],
            "sparse_coo_tensor": [
                "def sparse_coo_tensor(indices: Tensor, values: Union[Tensor,List],"
                " size: Optional[_size]=None, *, dtype: Optional[_dtype]=None,"
                " device: Union[_device, str, None]=None, requires_grad:_bool=False) -> Tensor: ..."
            ],
            "_sparse_coo_tensor_unsafe": [
                "def _sparse_coo_tensor_unsafe(indices: Tensor, values: Tensor, size: List[int],"
                " dtype: Optional[_dtype] = None, device: Optional[_device] = None,"
                " requires_grad: bool = False) -> Tensor: ..."
            ],
            "sparse_compressed_tensor": [
                "def sparse_compressed_tensor(compressed_indices: Union[Tensor, List],"
                "plain_indices: Union[Tensor, List],"
                " values: Union[Tensor, List], size: Optional[_size]=None,"
                " *, dtype: Optional[_dtype]=None, layout: Optional[_layout] = None,"
                " device: Union[_device, str, None]=None, requires_grad:_bool=False) -> Tensor: ..."
            ],
            "_sparse_compressed_tensor_unsafe": [
                "def _sparse_compressed_tensor_unsafe(comp_indices: Union[Tensor, List],"
                "plain_indices: Union[Tensor, List],"
                " values: Union[Tensor, List], size: List[int],"
                " dtype: Optional[_dtype] = None, layout: Optional[_layout] = None,"
                " device: Optional[_device] = None,"
                " requires_grad: bool = False) -> Tensor: ..."
            ],
            "_sync": ["def _sync(t: Tensor) -> None: ..."],
            "_is_functional_tensor": [
                "def _is_functional_tensor(t: Tensor) -> _bool: ..."
            ],
            "_from_functional_tensor": [
                "def _from_functional_tensor(t: Tensor) -> Tensor: ..."
            ],
            "_to_functional_tensor": [
                "def _to_functional_tensor(t: Tensor) -> Tensor: ..."
            ],
            "range": [
                "def range(start: Number, end: Number,"
                " step: Number=1, *, out: Optional[Tensor]=None, {}) -> Tensor: ...".format(
                    FACTORY_PARAMS
                )
            ],
            "arange": [
                "def arange(start: Number, end: Number, step: Number, *,"
                " out: Optional[Tensor]=None, {}) -> Tensor: ...".format(
                    FACTORY_PARAMS
                ),
                "def arange(start: Number, end: Number, *, out: Optional[Tensor]=None, {}) -> Tensor: ...".format(
                    FACTORY_PARAMS
                ),
                "def arange(end: Number, *, out: Optional[Tensor]=None, {}) -> Tensor: ...".format(
                    FACTORY_PARAMS
                ),
            ],
            "linspace": [
                "def linspace(start: Number, end: Number, steps: Optional[_int]=None, *,"
                " out: Optional[Tensor]=None, {}) -> Tensor: ...".format(FACTORY_PARAMS)
            ],
            "logspace": [
                "def logspace(start: Number, end: Number, steps: Optional[_int]=None, base: _float=10.0, *,"
                " out: Optional[Tensor]=None, {}) -> Tensor: ...".format(FACTORY_PARAMS)
            ],
            "randint": [
                "def randint(low: _int, high: _int, size: _size, *,"
                " generator: Optional[Generator]=None, {}) -> Tensor: ...".format(
                    FACTORY_PARAMS
                ),
                "def randint(high: _int, size: _size, *,"
                " generator: Optional[Generator]=None, {}) -> Tensor: ...".format(
                    FACTORY_PARAMS
                ),
            ],
            "full": [
                "def full(size: _size, fill_value: Number, *,"
                " out: Optional[Tensor]=None,"
                " layout: _layout=strided, {}) -> Tensor: ...".format(FACTORY_PARAMS),
                "def full(size: _size, fill_value: Number, *,"
                " names: List[Union[str, None]],"
                " layout: _layout=strided, {}) -> Tensor: ...".format(FACTORY_PARAMS),
            ],
            "is_grad_enabled": ["def is_grad_enabled() -> _bool: ..."],
            "is_inference_mode_enabled": [
                "def is_inference_mode_enabled() -> _bool: ..."
            ],
            "nonzero": [
                "def nonzero(input: Tensor, *, as_tuple: Literal[False]=False, out: Optional[Tensor]=None) -> Tensor: ...",
                "def nonzero(input: Tensor, *, as_tuple: Literal[True]) -> Tuple[Tensor, ...]: ...",
            ],
            "binary_cross_entropy_with_logits": [
                "def binary_cross_entropy_with_logits(input: Tensor, target: Tensor, "
                "weight: Optional[Tensor] = None, size_average: Optional[bool] = None, "
                "reduce: Optional[bool] = None, reduction: str = ..., "
                "pos_weight: Optional[Tensor] = None) -> Tensor: ..."
            ],
            "cosine_embedding_loss": [
                "def cosine_embedding_loss(input1: Tensor, input2: Tensor, "
                "target: Tensor, margin: float = ..., size_average: Optional[bool] = ..., "
                "reduce: Optional[bool] = ..., reduction: str = ...) -> Tensor: ..."
            ],
            "ctc_loss": [
                "def ctc_loss(log_probs: Tensor, targets: Tensor, input_lengths: Tensor, target_lengths: Tensor,"
                " blank: int = ..., reduction: str = ..., zero_infinity: bool = ...) -> Tensor: ..."
            ],
            "hinge_embedding_loss": [
                "def hinge_embedding_loss(input: Tensor, target: Tensor, margin: float = ...,"
                " size_average: Optional[bool] = ..., reduce: Optional[bool] = ..., "
                "reduction: str = ...) -> Tensor: ..."
            ],
            "kl_div": [
                "def kl_div(input: Tensor, target: Tensor, size_average: Optional[bool] = ..., "
                "reduce: Optional[bool] = ..., reduction: str = ..., log_target: bool = ...) -> Tensor: ..."
            ],
            "margin_ranking_loss": [
                "def margin_ranking_loss(input1: Tensor, input2: Tensor, target: Tensor,"
                " margin: float = ..., size_average: Optional[bool] = ..., "
                " reduce: Optional[bool] = ..., reduction: str = ...) -> Tensor: ..."
            ],
            "triplet_margin_loss": [
                "def triplet_margin_loss(anchor: Tensor, positive: Tensor, negative: Tensor, "
                "margin: float = ..., p: float = ..., eps: float = ..., swap: bool = ..., "
                "size_average: Optional[bool] = ..., "
                "reduce: Optional[bool] = ..., reduction: str = ...) -> Tensor: ..."
            ],
            "dsmm": ["def dsmm(input: Tensor, mat2: Tensor) -> Tensor: ..."],
            "hsmm": ["def hsmm(input: Tensor, mat2: Tensor) -> Tensor: ..."],
            "saddmm": [
                "def saddmm(input: Tensor, mat1: Tensor, mat2: Tensor, *, beta: Number=1, "
                "alpha: Number=1, out: Optional[Tensor]=None) -> Tensor: ..."
            ],
            "spmm": ["def spmm(input: Tensor, mat2: Tensor) -> Tensor: ..."],
            "div": [
                "def div(input: Union[Tensor, Number], other: Union[Tensor, Number], *, "
                "rounding_mode: Optional[str] = None, out: Optional[Tensor]=None) -> Tensor: ..."
            ],
        }
    )
    for binop in ["mul", "true_divide", "floor_divide"]:
        unsorted_function_hints[binop].append(
            "def {}(input: Union[Tensor, Number],"
            " other: Union[Tensor, Number],"
            " *, out: Optional[Tensor]=None) -> Tensor: ...".format(binop)
        )
    for binop in ["add", "sub"]:
        unsorted_function_hints[binop].append(
            "def {}(input: Union[Tensor, Number],"
            " other: Union[Tensor, Number],"
            " *, alpha: Optional[Number]=1, out: Optional[Tensor]=None) -> Tensor: ...".format(
                binop
            )
        )

    native_functions = parse_native_yaml(
        native_yaml_path, tags_yaml_path
    ).native_functions
    native_functions = list(filter(should_generate_py_binding, native_functions))

    function_signatures = load_signatures(
        native_functions, deprecated_yaml_path, method=False, pyi=True
    )
    sig_groups = get_py_torch_functions(function_signatures)
    for group in sorted(sig_groups, key=lambda g: g.signature.name):
        name = group.signature.name
        unsorted_function_hints[name] += generate_type_hints(group)

        named_tuple = returns_named_tuple_pyi(group.signature)
        if named_tuple is not None and not group.signature.deprecated:
            # deprecated namedtuples are currently not included for torch functions
            tuple_name, tuple_def = named_tuple
            if tuple_name in namedtuples:
                assert namedtuples[tuple_name] == tuple_def
            else:
                namedtuples[tuple_name] = tuple_def

    function_hints = []
    for name, hints in sorted(unsorted_function_hints.items()):
        if len(hints) > 1:
            hints = ["@overload\n" + h for h in hints]
        function_hints += hints

    # Generate type signatures for Tensor methods
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    unsorted_tensor_method_hints: Dict[str, List[str]] = collections.defaultdict(list)
    unsorted_tensor_method_hints.update(
        {
            "size": [
                "def size(self) -> Size: ...",
                "def size(self, dim: _int) -> _int: ...",
            ],
            "stride": [
                "def stride(self) -> Tuple[_int]: ...",
                "def stride(self, _int) -> _int: ...",
            ],
            "new_ones": [
                "def new_ones(self, size: _size, {}) -> Tensor: ...".format(
                    FACTORY_PARAMS
                )
            ],
            "new_tensor": [
                "def new_tensor(self, data: Any, {}) -> Tensor: ...".format(
                    FACTORY_PARAMS
                )
            ],
            # new and __init__ have the same signatures differ only in return type
            # Adapted from legacy_tensor_ctor and legacy_tensor_new
            "new": [
                "def new(self, *args: Any, {}) ->Tensor: ...".format(DEVICE_PARAM),
                "def new(self, storage: Storage) -> Tensor: ...",
                "def new(self, other: Tensor) -> Tensor: ...",
                "def new(self, size: _size, *, {}) -> Tensor: ...".format(DEVICE_PARAM),
            ],
            "__init__": [
                "def __init__(self, *args: Any, {}) -> None: ...".format(DEVICE_PARAM),
                "def __init__(self, storage: Storage) -> None: ...",
                "def __init__(self, other: Tensor) -> None: ...",
                "def __init__(self, size: _size, *, {}) -> None: ...".format(
                    DEVICE_PARAM
                ),
            ],
            "as_subclass": ["def as_subclass(self, cls: Type[S]) -> S: ..."],
            "_make_subclass": [
                "def _make_subclass(cls, data: Tensor, require_grad: _bool = False, dispatch_strides: _bool=False,"
                " dispatch_device: _bool=False, device_for_backend_keys: Optional[_device] = None) -> Tensor: ..."
            ],
            "__getitem__": ["def __getitem__(self, {}) -> Tensor: ...".format(INDICES)],
            "__setitem__": [
                "def __setitem__(self, {}, val: Union[Tensor, Number])"
                " -> None: ...".format(INDICES)
            ],
            "tolist": ["def tolist(self) -> List: ..."],
            "requires_grad_": [
                "def requires_grad_(self, mode: _bool=True) -> Tensor: ..."
            ],
            "element_size": ["def element_size(self) -> _int: ..."],
            "data_ptr": ["def data_ptr(self) -> _int: ..."],
            "dim": ["def dim(self) -> _int: ..."],
            "nonzero": [
                "def nonzero(self, *, as_tuple: Literal[False]=False) -> Tensor: ...",
                "def nonzero(self, *, as_tuple: Literal[True]) -> Tuple[Tensor, ...]: ...",
            ],
            "numel": ["def numel(self) -> _int: ..."],
            "ndimension": ["def ndimension(self) -> _int: ..."],
            "nelement": ["def nelement(self) -> _int: ..."],
            "cuda": [
                "def cuda(self, device: Optional[Union[_device, _int, str]]=None, non_blocking: _bool=False) -> Tensor: ..."
            ],
            "numpy": ["def numpy(self, *, force: _bool=False) -> Any: ..."],
            "apply_": ["def apply_(self, callable: Callable) -> Tensor: ..."],
            "map_": [
                "def map_(self, tensor: Tensor, callable: Callable) -> Tensor: ..."
            ],
            "map2_": [
                "def map2_(self, x: Tensor, y: Tensor, callable: Callable) -> Tensor: ..."
            ],
            "storage": ["def _storage(self) -> Storage: ..."],
            "storage_type": ["def storage_type(self) -> Storage: ..."],
            "type": [
                "def type(self, dtype: None=None, non_blocking: _bool=False) -> str: ...",
                "def type(self, dtype: Union[str, _dtype], non_blocking: _bool=False) -> Tensor: ...",
            ],
            "get_device": ["def get_device(self) -> _int: ..."],
            "contiguous": [
                "def contiguous(self, memory_format=torch.contiguous_format) -> Tensor: ..."
            ],
            "has_names": ["def has_names(self) -> _bool: ..."],
            "is_contiguous": [
                "def is_contiguous(self, memory_format=torch.contiguous_format) -> _bool: ..."
            ],
            "_is_view": ["def _is_view(self) -> _bool: ..."],
            "is_cuda": ["is_cuda: _bool"],
            "is_leaf": ["is_leaf: _bool"],
            "is_nested": ["is_nested: _bool"],
            "is_sparse": ["is_sparse: _bool"],
            "is_sparse_csr": ["is_sparse_csr: _bool"],
            "is_quantized": ["is_quantized: _bool"],
            "is_meta": ["is_meta: _bool"],
            "is_mps": ["is_mps: _bool"],
            "is_ort": ["is_ort: _bool"],
            "is_mkldnn": ["is_mkldnn: _bool"],
            "is_vulkan": ["is_vulkan: _bool"],
            "is_ipu": ["is_ipu: _bool"],
            "storage_offset": ["def storage_offset(self) -> _int: ..."],
            "to": [
                "def to(self, dtype: _dtype, non_blocking: _bool=False, copy: _bool=False) -> Tensor: ...",
                "def to(self, device: Optional[Union[_device, str]]=None, dtype: Optional[_dtype]=None, "
                "non_blocking: _bool=False, copy: _bool=False) -> Tensor: ...",
                "def to(self, other: Tensor, non_blocking: _bool=False, copy: _bool=False) -> Tensor: ...",
            ],
            "item": ["def item(self) -> Number: ..."],
            "copy_": [
                "def copy_(self, src: Tensor, non_blocking: _bool=False) -> Tensor: ..."
            ],
            "set_": [
                "def set_(self, storage: Union[Storage, TypedStorage], offset: _int, size: _size, stride: _size) -> Tensor: ...",
                "def set_(self, storage: Union[Storage, TypedStorage]) -> Tensor: ...",
            ],
            "split": [
                "def split(self, split_size: _int, dim: _int=0) -> Sequence[Tensor]: ...",
                "def split(self, split_size: Tuple[_int, ...], dim: _int=0) -> Sequence[Tensor]: ...",
            ],
            "div": [
                "def div(self, other: Union[Tensor, Number], *, rounding_mode: Optional[str] = None) -> Tensor: ..."
            ],
            "div_": [
                "def div_(self, other: Union[Tensor, Number], *, rounding_mode: Optional[str] = None) -> Tensor: ..."
            ],
        }
    )
    for binop in ["mul", "true_divide", "floor_divide"]:
        for inplace in [False, True]:
            out_suffix = ", *, out: Optional[Tensor]=None"
            if inplace:
                binop += "_"
                out_suffix = ""
            unsorted_tensor_method_hints[binop].append(
                "def {}(self, other: Union[Tensor, Number, torch.SymIntNode, torch.SymFloatNode]{})"
                " -> Tensor: ...".format(binop, out_suffix)
            )
    for binop in ["add", "sub"]:
        for inplace in [False, True]:
            out_suffix = ", out: Optional[Tensor]=None"
            if inplace:
                binop += "_"
                out_suffix = ""
            unsorted_tensor_method_hints[binop].append(
                "def {}(self, other: Union[Tensor, Number, torch.SymIntNode, torch.SymFloatNode], "
                "*, alpha: Optional[Number]=1{})"
                " -> Tensor: ...".format(binop, out_suffix)
            )
    simple_conversions = [
        "byte",
        "char",
        "cpu",
        "double",
        "float",
        "half",
        "int",
        "long",
        "short",
        "bool",
        "bfloat16",
    ]
    for name in simple_conversions:
        unsorted_tensor_method_hints[name].append(
            "def {}(self) -> Tensor: ...".format(name)
        )

    # pyi tensor methods don't currently include deprecated signatures for some reason
    # TODO: we should probably add them in
    tensor_method_signatures = load_signatures(
        native_functions,
        deprecated_yaml_path,
        method=True,
        skip_deprecated=True,
        pyi=True,
    )
    tensor_method_sig_groups = get_py_torch_functions(
        tensor_method_signatures, method=True
    )

    for group in sorted(tensor_method_sig_groups, key=lambda g: g.signature.name):
        name = group.signature.name
        unsorted_tensor_method_hints[name] += generate_type_hints(group)

        named_tuple = returns_named_tuple_pyi(group.signature)
        if named_tuple is not None and not group.signature.deprecated:
            # deprecated namedtuples are currently not included for torch functions
            tuple_name, tuple_def = named_tuple
            if tuple_name in namedtuples:
                assert namedtuples[tuple_name] == tuple_def
            else:
                namedtuples[tuple_name] = tuple_def

    for op in all_ops:
        name = "__{}__".format(op)
        unsorted_tensor_method_hints[name] += sig_for_ops(name)

    tensor_method_hints = []
    for name, hints in sorted(unsorted_tensor_method_hints.items()):
        if len(hints) > 1:
            hints = ["@overload\n" + h for h in hints]
        tensor_method_hints += hints

    # TODO: Missing type hints for nn

    # Generate namedtuple definitions
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    namedtuple_defs = [
        "{} = {}".format(name, defn) for name, defn in namedtuples.items()
    ]

    # Generate type signatures for legacy classes
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    legacy_storage_base_hints = ["class StorageBase(object): ..."]

    legacy_class_hints = []
    for c in (
        "DoubleTensor",
        "FloatTensor",
        "LongTensor",
        "IntTensor",
        "ShortTensor",
        "HalfTensor",
        "CharTensor",
        "ByteTensor",
        "BoolTensor",
    ):
        legacy_class_hints.append("class {}(Tensor): ...".format(c))

    # Generate type signatures for dtype classes
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    # TODO: don't explicitly list dtypes here; get it from canonical
    # source
    dtype_class_hints = [
        "{}: dtype = ...".format(n)
        for n in [
            "float32",
            "float",
            "float64",
            "double",
            "float16",
            "bfloat16",
            "half",
            "uint8",
            "int8",
            "int16",
            "short",
            "int32",
            "int",
            "int64",
            "long",
            "complex32",
            "complex64",
            "cfloat",
            "complex128",
            "cdouble",
            "quint8",
            "qint8",
            "qint32",
            "bool",
            "quint4x2",
            "quint2x4",
        ]
    ]

    # Generate __all__ directive
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    # Include only the functions that contain hints, to prevent undefined
    # symbols to be included in the `__all__` directive.
    hinted_function_names = [
        name for name, hint in unsorted_function_hints.items() if hint
    ]
    all_symbols = sorted(list(namedtuples.keys()) + hinted_function_names)
    all_directive = pformat(all_symbols, width=100, compact=True).split("\n")
    all_directive[0] = "__all__ = {}".format(all_directive[0])

    # Dispatch key hints
    # ~~~~~~~~~~~~~~~~~~
    dispatch_key_hints = [f"{d.name}: DispatchKey = ..." for d in DispatchKey]

    # Write out the stub
    # ~~~~~~~~~~~~~~~~~~

    env = {
        "namedtuple_defs": namedtuple_defs,
        "function_hints": function_hints,
        "tensor_method_hints": tensor_method_hints,
        "legacy_class_hints": legacy_class_hints,
        "legacy_storage_base_hints": legacy_storage_base_hints,
        "dtype_class_hints": dtype_class_hints,
        "dispatch_key_hints": dispatch_key_hints,
        "all_directive": all_directive,
    }
    fm.write_with_template(
        "torch/_C/__init__.pyi",
        "torch/_C/__init__.pyi.in",
        lambda: {
            "generated_comment": "@" + "generated from torch/_C/__init__.pyi.in",
            **env,
        },
    )
    fm.write_with_template(
        "torch/_C/_VariableFunctions.pyi",
        "torch/_C/_VariableFunctions.pyi.in",
        lambda: {
            "generated_comment": "@"
            + "generated from torch/_C/_VariableFunctions.pyi.in",
            **env,
        },
    )
    fm.write_with_template(
        "torch/_VF.pyi",
        "torch/_C/_VariableFunctions.pyi.in",
        lambda: {
            "generated_comment": "@"
            + "generated from torch/_C/_VariableFunctions.pyi.in",
            **env,
        },
    )
    fm.write_with_template(
        "torch/return_types.pyi",
        "torch/_C/return_types.pyi.in",
        lambda: {
            "generated_comment": "@" + "generated from torch/_C/return_types.pyi",
            **env,
        },
    )
    gen_nn_functional(fm)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate type stubs for PyTorch")
    parser.add_argument(
        "--native-functions-path",
        metavar="NATIVE",
        default="aten/src/ATen/native/native_functions.yaml",
        help="path to native_functions.yaml",
    )
    parser.add_argument(
        "--tags-path",
        metavar="TAGS",
        default="aten/src/ATen/native/tags.yaml",
        help="path to tags.yaml",
    )
    parser.add_argument(
        "--deprecated-functions-path",
        metavar="DEPRECATED",
        default="tools/autograd/deprecated.yaml",
        help="path to deprecated.yaml",
    )
    parser.add_argument(
        "--out", metavar="OUT", default=".", help="path to output directory"
    )
    args = parser.parse_args()
    fm = FileManager(install_dir=args.out, template_dir=".", dry_run=False)
    gen_pyi(
        args.native_functions_path, args.tags_path, args.deprecated_functions_path, fm
    )


if __name__ == "__main__":
    main()
