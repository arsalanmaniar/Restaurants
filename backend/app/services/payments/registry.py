from app.core.config import settings
from app.models import PaymentMethod, PaymentProviderName
from app.services.payments.base import PaymentProvider, ProviderNotConfigured
from app.services.payments.easypaisa import EasyPaisaProvider
from app.services.payments.fake import FakeProvider
from app.services.payments.jazzcash import JazzCashProvider

_BUILDERS = {
    PaymentProviderName.JAZZCASH: JazzCashProvider,
    PaymentProviderName.EASYPAISA: EasyPaisaProvider,
    PaymentProviderName.FAKE: FakeProvider,
}

# Which order payment_method maps to which gateway.
PROVIDER_FOR_METHOD = {
    PaymentMethod.JAZZCASH: PaymentProviderName.JAZZCASH,
    PaymentMethod.EASYPAISA: PaymentProviderName.EASYPAISA,
}


def get_provider(name: PaymentProviderName) -> PaymentProvider:
    # A fake gateway reachable in production means anyone can mark an order paid.
    # Allowed in DEBUG, or on a deployment that has DELIBERATELY opted into demo
    # payments (demo_payments_enabled) — a demo has no real money and no real
    # fulfilment. Both default off in a real production deployment.
    if name == PaymentProviderName.FAKE and not (
        settings.debug or settings.demo_payments_enabled
    ):
        raise ProviderNotConfigured("The fake payment provider is not available in production")

    builder = _BUILDERS.get(name)
    if builder is None:
        raise ProviderNotConfigured(f"Unknown payment provider: {name}")

    return builder()


def is_configured(name: PaymentProviderName) -> bool:
    """Whether we could actually take money through this provider right now."""
    try:
        get_provider(name)
        return True
    except ProviderNotConfigured:
        return False


def _fake_stand_in_available() -> bool:
    """Whether the FAKE provider is usable — in DEBUG, or on a deployment that has
    explicitly opted into demo payments (demo_payments_enabled). When true, the
    generic online option (and, in DEBUG, the named gateways) can be offered without
    real merchant credentials — the callback still runs through FakeProvider's signed
    pipeline, so every guard on the money path is exercised for real."""
    return (settings.debug or settings.demo_payments_enabled) and is_configured(
        PaymentProviderName.FAKE
    )


def provider_for_method(method: PaymentMethod) -> PaymentProviderName:
    """The provider that will actually settle this method's payments.

    Real provider if its credentials are set. Otherwise, when the fake stand-in
    is available (DEBUG, or a demo deployment with demo_payments_enabled), fall
    back to the FAKE provider so the flow can be demoed end-to-end without real
    merchant onboarding — this is what settles the generic `online` method. In a
    real production deployment (neither flag set) an unconfigured method raises
    ProviderNotConfigured — matching what `available_methods()` would have hidden
    from the AI in the first place.

    COD is not a gateway-backed method (money changes hands at delivery, not
    through a payment adapter). place_order returns before calling this for
    COD orders; asking here would mean COD has been misrouted.
    """
    if method == PaymentMethod.COD:
        raise ProviderNotConfigured(
            "cod is settled at delivery, not through a payment provider"
        )
    real = PROVIDER_FOR_METHOD.get(method)
    if real is not None and is_configured(real):
        return real
    if _fake_stand_in_available():
        return PaymentProviderName.FAKE
    raise ProviderNotConfigured(f"{method.value} is not available")


def available_methods() -> list[PaymentMethod]:
    """Payment methods we can genuinely offer, in the order they should be offered.

    COD always works. A NAMED gateway (jazzcash / easypaisa) is offered only when its
    OWN credentials are configured — we never advertise a specific gateway we don't
    actually have. When the fake stand-in is available it backs a single GENERIC
    ONLINE option (shown to the customer as "online payment"); in DEBUG it also backs
    the named gateways so their full flow is exercised in dev/tests. The AI never
    offers a method that would fail at the last step — both preview_bill and
    place_order refuse anything not in this list.
    """
    methods = [PaymentMethod.COD]
    # Real named gateways — only with their own credentials.
    for method, provider in PROVIDER_FOR_METHOD.items():
        if is_configured(provider):
            methods.append(method)
    if _fake_stand_in_available():
        # DEBUG (dev/tests) exercises the named-gateway flow through the fake
        # provider too, so those stay available there.
        if settings.debug:
            for method in (PaymentMethod.JAZZCASH, PaymentMethod.EASYPAISA):
                if method not in methods:
                    methods.append(method)
        # The generic demo online option — in DEBUG and on a non-debug demo deployment.
        if PaymentMethod.ONLINE not in methods:
            methods.append(PaymentMethod.ONLINE)
    return methods
