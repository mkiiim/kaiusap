"""Every annotation in src/ resolves on every supported Python (3.12 or later).

Python 3.14 evaluates annotations lazily (PEP 649), so an annotation that only
fails when evaluated, such as one naming a class member that hides a built-in,
breaks import on 3.12 and 3.13 but not on 3.14. Resolving every annotation here
fails on 3.14 too.
"""
import importlib
import inspect
import pkgutil
import typing

import kaiusap

# __main__ runs the CLI when imported.
SKIP = {"kaiusap.__main__"}


def modules():
    names = ["kaiusap"] + [info.name for info in pkgutil.walk_packages(kaiusap.__path__, "kaiusap.")]
    return [importlib.import_module(name) for name in sorted(names) if name not in SKIP]


def functions_of(cls):
    for value in vars(cls).values():
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        if isinstance(value, property):
            yield from (f for f in (value.fget, value.fset, value.fdel) if f)
        elif inspect.isfunction(value):
            yield value


def annotated():
    """Every class and function defined in src/, with each class's own functions."""
    seen = []
    for module in modules():
        for _, value in vars(module).items():
            if getattr(value, "__module__", None) != module.__name__:
                continue
            if inspect.isclass(value):
                seen.append(value)
                seen.extend(functions_of(value))
            elif inspect.isfunction(value):
                seen.append(value)
    return seen


def test_every_annotation_in_src_resolves():
    objects = annotated()
    assert any(getattr(o, "__name__", None) == "Provider" for o in objects)
    failures = []
    for obj in objects:
        try:
            typing.get_type_hints(obj)
        except Exception as exc:  # noqa: BLE001 - every failure is reported
            failures.append(f"{obj.__module__}.{obj.__qualname__}: {type(exc).__name__}: {exc}")
    assert not failures, "\n".join(failures)


def test_provider_annotations_name_the_builtin_list():
    from kaiusap.adapters import Provider
    assert typing.get_type_hints(Provider.list)["return"] == list[dict]
    assert typing.get_type_hints(Provider.mirror)["return"] == list[str]
    assert typing.get_type_hints(Provider.retire)["return"] == list[str]
