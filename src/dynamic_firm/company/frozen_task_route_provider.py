"""Provider-port adapter that dispatches through an already frozen task route.

This adapter owns no route-selection state.  Each physical provider call first
checks cancellation, resolves exactly one frozen binding from its supplied
physical request identifier, and asks the frozen registry to construct that
binding's exact provider adapter.
"""
from __future__ import annotations

from collections.abc import Callable

from dynamic_firm.runtime.models import (
    ModelRequest,
    ModelResponse,
    ModelStreamProgress,
    StructuredOutputRequest,
    StructuredOutputResponse,
)
from dynamic_firm.runtime.ports import (
    CancellationToken,
    ModelProviderError,
    OperationCancelled,
)

from .execution_route_binding import ExecutionRouteBinding
from .frozen_route_admission import FrozenRouteAdmission
from .route_provider_registry import FrozenRouteProviderRegistry


class FrozenTaskRouteProvider:
    """A stateless provider port for task-scoped frozen execution bindings.

    ``resolve_binding`` is deliberately injected: it is the only component
    allowed to map a run/request identifier to an already-selected binding.
    ``model_profile`` is never read because request-provided profiles are not
    route authority.
    """

    def __init__(
        self,
        resolve_binding: Callable[[str], ExecutionRouteBinding],
        registry: FrozenRouteProviderRegistry,
        *,
        resolve_admission: Callable[[str], FrozenRouteAdmission] | None = None,
    ) -> None:
        if not callable(resolve_binding):
            raise ValueError("a frozen binding resolver is required")
        if not isinstance(registry, FrozenRouteProviderRegistry):
            raise ValueError("a frozen route provider registry is required")
        self._resolve_binding = resolve_binding
        self._resolve_admission = resolve_admission
        self._registry = registry
        # These maps hold no routing state.  They only retain the adapter
        # instance that actually observed one in-flight physical call until
        # the parent can append its cancellation receipt.
        self._inflight_adapters: dict[str, object] = {}
        self._cancelled_adapters: dict[str, object] = {}

    def _provider_for(
        self, physical_id: str, cancellation: CancellationToken
    ) -> tuple[object, ExecutionRouteBinding]:
        # A cancelled request must not cause resolution, factory construction,
        # credential-reference handling, or any provider-side effect.
        cancellation.raise_if_cancelled()
        try:
            binding = self._resolve_binding(physical_id)
            if not isinstance(binding, ExecutionRouteBinding):
                raise TypeError("resolver did not return an ExecutionRouteBinding")
            if self._resolve_admission is not None:
                admission = self._resolve_admission(physical_id)
                if not isinstance(admission, FrozenRouteAdmission):
                    raise TypeError("resolver did not return a FrozenRouteAdmission")
                if admission.binding != binding:
                    raise ValueError("durable frozen route admission does not match binding")
            provider = self._registry.construct(binding)
        except Exception as exc:
            raise ModelProviderError(
                "FROZEN_ROUTE_UNAVAILABLE",
                "The frozen task route is unavailable.",
                retryable=False,
            ) from exc
        return provider, binding

    @staticmethod
    def _bound_request(
        request: ModelRequest,
        binding: ExecutionRouteBinding,
    ) -> ModelRequest:
        """Replace only request-controlled model identity with frozen identity."""

        return ModelRequest(
            messages=request.messages,
            tools=request.tools,
            model_profile=binding.requested_model_id,
            run_id=request.run_id,
            call_index=request.call_index,
        )

    @staticmethod
    def _bound_structured_request(
        request: StructuredOutputRequest,
        binding: ExecutionRouteBinding,
    ) -> StructuredOutputRequest:
        """Preserve structured-call shape while freezing model identity."""

        return StructuredOutputRequest(
            messages=request.messages,
            schema_name=request.schema_name,
            json_schema=request.json_schema,
            model_profile=binding.requested_model_id,
            request_id=request.request_id,
            call_index=request.call_index,
        )

    @staticmethod
    def _require_operation(provider: object, operation: str) -> Callable[..., object]:
        method = getattr(provider, operation, None)
        if not callable(method):
            raise ModelProviderError(
                "FROZEN_ROUTE_ADAPTER_INVALID",
                "The frozen task route has no compatible provider adapter.",
                retryable=False,
            )
        return method

    async def _call(
        self,
        physical_id: str,
        cancellation: CancellationToken,
        operation: Callable[..., object],
        *args: object,
    ) -> object:
        """Invoke one adapter while preserving only a cancelled call receipt."""

        provider = args[0]
        self._inflight_adapters[physical_id] = provider
        try:
            result = await operation(*args[1:])
            # A provider may have returned just as cancellation arrived; keep
            # its actual adapter available for the parent cancellation event.
            cancellation.raise_if_cancelled()
        except OperationCancelled:
            if self._inflight_adapters.get(physical_id) is provider:
                self._inflight_adapters.pop(physical_id, None)
                self._cancelled_adapters[physical_id] = provider
            raise
        finally:
            # ``asyncio.CancelledError`` is a BaseException.  Cleanup must
            # therefore live in ``finally`` rather than an ``Exception``
            # handler: a task timeout/shutdown must not retain an adapter or
            # turn it into a cancellation-receipt authority.  The
            # OperationCancelled branch above has already moved the exact
            # adapter out of this map when parent receipt forwarding applies.
            if self._inflight_adapters.get(physical_id) is provider:
                self._inflight_adapters.pop(physical_id, None)
        return result

    def consume_cancelled_request_id(self, physical_id: str) -> str | None:
        """Consume a receipt from the exact adapter that observed cancellation.

        This deliberately cannot resolve a binding or construct an adapter.
        Successful and non-cancelled failed calls have already been removed.
        """

        provider = self._cancelled_adapters.pop(physical_id, None)
        if provider is None:
            return None
        consumer = getattr(provider, "consume_cancelled_request_id", None)
        if not callable(consumer):
            return None
        try:
            value = consumer(physical_id)
        except Exception:
            return None
        return value if isinstance(value, str) and value else None

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        provider, binding = self._provider_for(request.run_id, cancellation)
        complete = self._require_operation(provider, "complete")
        response = await self._call(
            request.run_id,
            cancellation,
            complete,
            provider,
            self._bound_request(request, binding),
            cancellation,
        )
        if not isinstance(response, ModelResponse):
            raise TypeError("frozen route adapter returned an invalid model response")
        return response

    async def complete_stream(
        self,
        request: ModelRequest,
        cancellation: CancellationToken,
        progress: Callable[[ModelStreamProgress], None],
    ) -> ModelResponse:
        provider, binding = self._provider_for(request.run_id, cancellation)
        complete_stream = self._require_operation(provider, "complete_stream")
        response = await self._call(
            request.run_id,
            cancellation,
            complete_stream,
            provider,
            self._bound_request(request, binding),
            cancellation,
            progress,
        )
        if not isinstance(response, ModelResponse):
            raise TypeError("frozen route adapter returned an invalid model response")
        return response

    async def complete_structured(
        self,
        request: StructuredOutputRequest,
        cancellation: CancellationToken,
    ) -> StructuredOutputResponse:
        # Structured-output requests use their immutable request ID as their
        # physical-call identity; callers must bind it before dispatch.
        provider, binding = self._provider_for(request.request_id, cancellation)
        complete_structured = self._require_operation(provider, "complete_structured")
        response = await self._call(
            request.request_id,
            cancellation,
            complete_structured,
            provider,
            self._bound_structured_request(request, binding),
            cancellation,
        )
        if not isinstance(response, StructuredOutputResponse):
            raise TypeError("frozen route adapter returned an invalid structured response")
        return response
