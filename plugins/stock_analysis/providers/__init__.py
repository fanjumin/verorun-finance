"""VeroRun stock analysis data providers.

每个 provider 按数据类别声明自己供数（supports()），gateway 据此做类别路由与 failover。
"""
from .base import BaseProvider, DataCategory
from .base_v2 import BaseProviderV2, FetchResult, SecretResolver
from .akshare_provider import AkshareProvider
from .fmp_provider import FMPProvider
from .polygon_provider import PolygonProvider
from .sina import SinaProvider
from .tencent import TencentProvider
from .tushare_provider import TushareProvider
from .user_supplied import UserSuppliedProvider
from .terminal_provider import WindProvider, ChoiceProvider

__all__ = [
    "BaseProvider", "BaseProviderV2", "DataCategory", "FetchResult", "SecretResolver",
    "AkshareProvider", "FMPProvider", "PolygonProvider",
    "SinaProvider", "TencentProvider", "TushareProvider", "UserSuppliedProvider",
    "WindProvider", "ChoiceProvider",
]
