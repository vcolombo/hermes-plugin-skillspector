# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""Feature-detecting binding between the Hermes host LLM and SkillSpector.

Preferred (native) path: SkillSpector versions that ship scoped provider
injection (``skillspector.providers.use_provider`` / ``reset_provider``,
merged upstream in NVIDIA/SkillSpector#249, released in 2.4.x) get the
vendored :class:`BridgeHostProvider` injected through SkillSpector's own
ContextVar — no patching. The provider still reads the host LLM from this
package's ContextVar, so both paths share one binding model.

Bridge path (stock SkillSpector): the vendored :class:`BridgeHostProvider`
is exposed through SkillSpector's sanctioned duck-typed CLI-capability
surface, which needs exactly two seams that stock SkillSpector does not yet
provide, installed here as idempotent *wrappers* (originals are always
delegated to whenever no host LLM is bound):

1. **Provider selection** — ``skillspector.providers._select_active_provider``
   has no injection hook, so a wrapper prefers the bridge provider while a
   host LLM is bound for the current context.
2. **The MCP availability gate** — stock ``run_scan`` gates the LLM pass on
   credential resolution, which a credential-less host provider can never
   pass. The wrapper reports availability while a host LLM is bound. Both
   known gate spellings are handled (``resolve_provider_credentials``,
   ``resolve_chat_model_credentials``); if a future SkillSpector gates on
   capability (``is_llm_available``), no patch is needed and none is applied.

Both patches become inert (pure delegation) the moment the scan finishes and
the ContextVar is reset.
"""

from __future__ import annotations

from collections.abc import Callable

from .host_llm import BridgeHostProvider, get_host_llm, reset_host_llm, set_host_llm

_PATCHED_MARKER = "_hermes_skillspector_bridge_patch"
_patches_installed = False


def _install_selection_patch() -> None:
    import skillspector.providers as providers

    original = providers._select_active_provider
    if getattr(original, _PATCHED_MARKER, False):
        return

    def _select_with_host(*args: object, **kwargs: object):
        if get_host_llm() is not None:
            return BridgeHostProvider()
        return original(*args, **kwargs)

    setattr(_select_with_host, _PATCHED_MARKER, True)
    providers._select_active_provider = _select_with_host


def _install_gate_patch() -> None:
    import skillspector.mcp_server as mcp_server

    for name in ("resolve_provider_credentials", "resolve_chat_model_credentials"):
        original = getattr(mcp_server, name, None)
        if original is None or getattr(original, _PATCHED_MARKER, False):
            continue

        def _resolve_with_host(*args: object, _orig=original, **kwargs: object):
            if get_host_llm() is not None:
                # Truthy sentinel: the host owns real credentials; the gate
                # only checks "is not None".
                return ("host", None)
            return _orig(*args, **kwargs)

        setattr(_resolve_with_host, _PATCHED_MARKER, True)
        setattr(mcp_server, name, _resolve_with_host)
    # If neither name exists, the installed SkillSpector gates on capability
    # (is_llm_available), which already honours duck-typed CLI-capable
    # providers — nothing to patch.


def _ensure_patches() -> None:
    global _patches_installed
    if _patches_installed:
        return
    _install_selection_patch()
    _install_gate_patch()
    _patches_installed = True


def bind(host_llm: object) -> Callable[[], None]:
    """Bind *host_llm* for the current scan; returns a cleanup callable.

    Uses SkillSpector's native embedded-provider support when present,
    otherwise activates the vendored bridge.
    """
    import skillspector.providers as providers

    native_use = getattr(providers, "use_provider", None)
    native_reset = getattr(providers, "reset_provider", None)
    if callable(native_use) and callable(native_reset):
        host_token = set_host_llm(host_llm)
        provider_token = native_use(BridgeHostProvider())

        def _cleanup() -> None:
            native_reset(provider_token)
            reset_host_llm(host_token)

        return _cleanup

    _ensure_patches()
    token = set_host_llm(host_llm)
    return lambda: reset_host_llm(token)
