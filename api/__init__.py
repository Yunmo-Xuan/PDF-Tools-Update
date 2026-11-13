from ._decorators import _wrap_api_methods
from .base import BaseApi
from .settings import SettingsMixin
from .wizard import WizardMixin
from .viewer import ViewerMixin
from .pages import PagesMixin
from .split_merge import SplitMergeMixin
from .stamp import StampMixin
from .watermark import WatermarkMixin
from .compress import CompressMixin
from .encrypt import EncryptMixin
from .convert import ConvertMixin
from .metadata import MetadataMixin
from .recent import RecentMixin
from .updater import UpdaterMixin
from .text_export import TextExportMixin
from .extract import ExtractMixin
from .make_pdf import MakePdfMixin
from .pagelayout import PageLayoutMixin
from .redact import RedactMixin
from .clean import CleanMixin
from .bookmark import BookmarkMixin
from .printing import PrintMixin
from .ocr import OcrMixin
from .enhance import EnhanceMixin
from .fonts import FontsMixin
from .page_model import PageModelMixin
from .shell import ShellMixin
from .translate import TranslateMixin
from .qifeng import QifengMixin
from .latex import LatexMixin


@_wrap_api_methods
class Api(
    BaseApi,
    SettingsMixin,
    WizardMixin,
    ViewerMixin,
    PagesMixin,
    SplitMergeMixin,
    StampMixin,
    WatermarkMixin,
    CompressMixin,
    EncryptMixin,
    ConvertMixin,
    MetadataMixin,
    RecentMixin,
    UpdaterMixin,
    # 7.x 新增的能力：导文本/提图 → 造 PDF → 页码页眉/裁剪缩放 → 涂黑/清理/书签
    TextExportMixin,
    ExtractMixin,
    MakePdfMixin,
    PageLayoutMixin,
    RedactMixin,
    CleanMixin,
    BookmarkMixin,
    PrintMixin,
    OcrMixin,
    EnhanceMixin,
    FontsMixin,
    PageModelMixin,
    ShellMixin,
    TranslateMixin,
    QifengMixin,
    LatexMixin,
):
    pass


__all__ = ['Api']
