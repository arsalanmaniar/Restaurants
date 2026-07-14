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


def available_methods() -> list[PaymentMethod]:
    """Payment methods we can genuinely offer. COD always works; the online ones only
    once credentials exist — so the AI never offers a payment option that would then
    fail at the last step."""
    methods = [PaymentMethod.COD]
    methods.extend(
        method
        for method, provider in PROVIDER_FOR_METHOD.items()
        if is_configured(provider)
    )
    return methods
