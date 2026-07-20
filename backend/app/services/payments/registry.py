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
    # This check is the only thing preventing that, so it is not negotiable.
    if name == PaymentProviderName.FAKE and not settings.debug:
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
    """Whether the FAKE provider is usable — i.e. we're in DEBUG mode. When true,
    we can offer JazzCash / EasyPaisa as options for demo/testing even without
    real merchant credentials (the callback still runs through FakeProvider's
    signed pipeline — every guard on the money path is exercised for real)."""
    return settings.debug and is_configured(PaymentProviderName.FAKE)


def provider_for_method(method: PaymentMethod) -> PaymentProviderName:
    """The provider that will actually settle this method's payments.

    Real provider if its credentials are set. Otherwise, in DEBUG mode, fall
    back to the FAKE provider so the flow can be demoed end-to-end without
    real merchant onboarding. In production (DEBUG=false) an unconfigured
    method raises ProviderNotConfigured — matching what `available_methods()`
    would have hidden from the AI in the first place.

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
    """Payment methods we can genuinely offer. COD always works; online ones need
    either real credentials, or the FAKE stand-in in DEBUG mode. The AI never
    offers a payment option that would then fail at the last step."""
    methods = [PaymentMethod.COD]
    fake_ok = _fake_stand_in_available()
    for method, provider in PROVIDER_FOR_METHOD.items():
        if is_configured(provider) or fake_ok:
            methods.append(method)
    return methods
