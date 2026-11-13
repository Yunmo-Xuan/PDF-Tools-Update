r"""PDF 文本翻译的能力层：provider 可插拔 + 批量 + 缓存 + 如实报错。

这一层只管「拿到译文」，**不碰 PDF**：解析页面用 `core/page_model.py`，
写回用 `core/page_edit.py`，两个都是现成的，这里一个字都不重复实现。

## provider 的最小接口（接新引擎只要满足这一条）

```python
class 我的Provider:
    name = 'myengine'

    def translate_batch(self, texts, source, target):
        # texts: 去重后的原文列表（str，长度 >= 1，全非空）
        # source: 'auto' 或某个语言代码；target: 本软件支持的十种代码之一
        return {'ok': True,  'results': [{'src': 原文, 'dst': 译文}, ...]}
        # 失败时不要抛异常，也不要回空译文：
        return {'ok': False, 'error': 错误码, ...}
```

`results` **必须和 `texts` 同长度、同顺序**（按下标对位）；少一条、空译文一律算失败。
可选带 `'coverage'`（每项一个 0~1 的浮点，表示这句里有多少比例是真翻了而不是原样抄回），
本层会汇总后原样报给调用方。

## 两档内置 provider

* `offline`：**随包最小词表**（见 `_GLOSSARY`）。这是**兜底**，不是机器翻译：
  词面命中才换，翻不到的词**原样返回**，并如实报 `coverage`（通常远低于 1）。
  永远「可用」（不需要网络、不需要密钥），但不要把这种结果当成译文交给用户而不说明。
* `http`：可配置的服务地址 + 可选密钥，POST JSON，超时上限，网络类失败**重试一次就放弃**。
  **默认不启用**：配置里没填 `translate_url` 就直接 `error: 'no_url'`，一句译文都不编。

## 不许假装成功（这一层的存在意义）

连不上 / 超时 / 没配密钥地址 / 服务返回的形状不对 / 返回空译文 —— 全部
`{'success': False, 'error': 码, 'detail': 原料}`，绝不返回 `results: [{'dst': ''}]`
冒充翻好了，也绝不把原文当译文交回去再报 `success: True`（`unmatched` 会如实报数）。

## 配置怎么填（用户/开发者）

配置文件 `%APPDATA%\\PDFToolkit\\config.json`（源码运行同样是这个位置），四个键：

| 键 | 作用 | 默认 |
| --- | --- | --- |
| `translate_provider` | 用哪一档：`offline` / `http` | `offline` |
| `translate_url` | http 档的服务地址（`http://` 或 `https://`），**必填才有服务** | `''` |
| `translate_api_key` | 密钥，发成 `Authorization: Bearer <密钥>`；留空即匿名请求 | `''` |
| `translate_timeout` | 单次请求的超时秒数上限 | `12` |
| `translate_batch_max` | 一次 POST 最多带多少句 | `100` |
| `translate_lang_map` | 本软件语言码 → 服务方语言码的改名表，如 `{"zh-CN": "zh-CN", "en": "en-US"}` | `{}` |

调用方也可以不碰配置文件，直接把字典当 `settings=` 传进来（`api` 层走的就是这条：
它把 `load_config()` 的结果原样递过来，本层只挑 `translate_*` 那几个键）。

服务端契约（自建服务照这个写就行）：

```
POST <translate_url>
Content-Type: application/json
Authorization: Bearer <translate_api_key>    # 密钥为空则不发这个头

{"texts": ["Hello world", "Page 1"], "source": "auto", "target": "zh-CN",
 "provider": "http"}

→ 200 {"results": [{"src": "Hello world", "dst": "你好世界"},
                   {"src": "Page 1",       "dst": "第 1 页"}]}
```

认三种返回形状：`{"results": [...]}`、`{"translations": [...]}`、裸数组 `[...]`；
每格可以是 `{"src"/"dst"}`、`{"text"/"translation"/"target"}` 或直接一个字符串。
状态码 4xx 不重试（那是配置/授权错了，重试没有意义），超时/连不上/5xx 重试一次。

## 缓存

同一句原文不重复请求。落在既有缓存口径的位置：`%LOCALAPPDATA%\\PDFToolkit\\translate_cache`
（和 `ocr_cache`、`font_cache` 同一个爸爸，Local 而非 Roaming：缓存错了要能自愈）。
按 `provider + source + 原文` 为键、按 `provider+target` 分文件存，只存成功的译文。

## 取消

`translate_texts(..., should_stop=jobctl.should_stop)`：在**批次之间**查一次标记，
查到就停并回 `error: 'cancelled'`（带 `translated` 已翻好的条数）。逐页循环的取消
在 `api/translate.py` 那一边（`core/jobctl.py` 的槽位），这里只提供钩子。
"""

import json
import logging
import os
import re
import socket
import threading
import time
from urllib import error as _urlerror
from urllib import request as _urlrequest

from core.i18n import SUPPORTED as TARGET_LANGS
from core.paths import CONFIG_FILE, OCR_CACHE_DIR

# ==================== 常量 ====================
MODULE_VERSION = 1

DEFAULT_PROVIDER = 'offline'
PROVIDER_OFFLINE = 'offline'
PROVIDER_HTTP = 'http'

# 一次 POST 最多带多少句（服务方常有条数/体积上限，攒一大坨不如分几批）
MAX_BATCH = 100
# 超时上限（秒）。可被配置里的 translate_timeout 覆盖。
DEFAULT_TIMEOUT = 12.0
# 网络类失败重试几次就放弃。**就一次**，再多重试只会让用户多等，不会改变结果。
RETRY = 1

CONFIG_KEYS = ('translate_provider', 'translate_url', 'translate_api_key',
               'translate_timeout', 'translate_batch_max', 'translate_lang_map')

SETTINGS_DEFAULTS = {
    'translate_provider': DEFAULT_PROVIDER,
    'translate_url': '',
    'translate_api_key': '',
    'translate_timeout': DEFAULT_TIMEOUT,
    'translate_batch_max': MAX_BATCH,
    'translate_lang_map': {},
}

# 缓存目录：跟 ocr_cache / font_cache 同一个口径（Local AppData 下的 PDFToolkit）。
# 不改 core/paths.py（它只列固定要用的那几个），这里从 OCR_CACHE_DIR 的父目录推，
# 免得又写死一个 %LOCALAPPDATA% 出现两处真源。目录在真要写的时候才建。
CACHE_DIR = os.path.join(os.path.dirname(OCR_CACHE_DIR), 'translate_cache')
CACHE_VERSION = 2      # 2：每格从「裸译文」改成 [译文, 覆盖率]（v 对不上就当没缓存重来）
CACHE_LIMIT = 20000          # 单文件最多多少条，超了丢最旧的一半
CACHE_KEY_SEP = '\x1f'       # 键里 source 与 text 的分隔（不可见字符，正文里不会出现）

# 本层会回给调用方的错误码（api 层按码翻文案；验收对着这张表核）。
# no_texts / unsupported_target / unknown_provider / no_url / bad_url / timeout /
# unreachable / server_error / bad_status / bad_payload / provider_failed / cancelled
# 其中 timeout / unreachable / server_error 是「重试过一次还是不行」才报出来的。
ERROR_CODES = ('no_texts', 'unsupported_target', 'unknown_provider', 'not_configured',
               'no_url', 'bad_url', 'timeout', 'unreachable', 'server_error',
               'bad_status', 'bad_payload', 'provider_failed', 'cancelled')


def _error(code, **detail):
    """core 层的失败形状：只有码和原料，话术留给 api 层（core 不说人话，才别的入口能用）。"""
    result = {'success': False, 'error': code}
    if detail:
        result['detail'] = detail.get('detail') or detail
    return result


# ==================== 随包最小词表（offline 档） ====================
# 十列，顺序**就是** core.i18n.SUPPORTED 的顺序（和十本界面字典一档不差）：
#   zh-CN, zh-TW, en, ja, ko, fr, ru, de, es, it
# 一列一格 = 这个概念在该语言里的写法。查表时**任何一格都能当入口**（所以
# 「文档→document」和「document→文档」用的是同一行，不必写两份）。
#
# 这**不是**机器翻译，是随包的兜底词表：
#   * 只收 PDF 里高频的单词和极少数固定短语；
#   * 不做变格/变位/动词时态（俄语的格、德语的性数、西语的动词变位都照顾不到），
#     所以整句读起来是「单词卡拼起来的」，这是本表诚实的局限；
#   * 翻不到的词原样抄回，并把这个事实计进 coverage。
# 一份词表要能长期维护，就得一眼看得出「哪一行是哪个概念」，所以按主题分组写。
GLOSSARY_LANGS = ('zh-CN', 'zh-TW', 'en', 'ja', 'ko', 'fr', 'ru', 'de', 'es', 'it')

_GLOSSARY = (
    # ---------- 文档与结构 ----------
    ('文档', '文檔', 'document', '文書', '문서', 'document', 'документ', 'Dokument', 'documento', 'documento'),
    ('页面', '頁面', 'page', 'ページ', '페이지', 'page', 'страница', 'Seite', 'página', 'pagina'),
    ('文件', '檔案', 'file', 'ファイル', '파일', 'fichier', 'файл', 'Datei', 'archivo', 'file'),
    ('文件夹', '資料夾', 'folder', 'フォルダー', '폴더', 'dossier', 'папка', 'Ordner', 'carpeta', 'cartella'),
    ('标题', '標題', 'title', 'タイトル', '제목', 'titre', 'заголовок', 'Titel', 'título', 'titolo'),
    ('副标题', '副標題', 'subtitle', 'サブタイトル', '부제', 'sous-titre', 'подзаголовок', 'Untertitel', 'subtítulo', 'sottotitolo'),
    ('作者', '作者', 'author', '著者', '저자', 'auteur', 'автор', 'Autor', 'autor', 'autore'),
    ('章节', '章節', 'chapter', '章', '장', 'chapitre', 'глава', 'Kapitel', 'capítulo', 'capitolo'),
    ('小节', '小節', 'section', '節', '절', 'section', 'раздел', 'Abschnitt', 'sección', 'sezione'),
    ('目录', '目錄', 'contents', '目次', '목차', 'sommaire', 'содержание', 'Inhaltsverzeichnis', 'sumario', 'indice'),
    ('索引', '索引', 'index', '索引', '색인', 'index', 'указатель', 'Index', 'índice', 'indice analitico'),
    ('摘要', '摘要', 'summary', '概要', '요약', 'résumé', 'резюме', 'Zusammenfassung', 'resumen', 'riassunto'),
    ('引言', '引言', 'introduction', 'はじめに', '서론', 'introduction', 'введение', 'Einführung', 'introducción', 'introduzione'),
    ('结论', '結論', 'conclusion', '結論', '결론', 'conclusion', 'заключение', 'Fazit', 'conclusión', 'conclusione'),
    ('参考文献', '參考文獻', 'references', '参考文献', '참고 문헌', 'références', 'литература', 'Literatur', 'referencias', 'riferimenti'),
    ('附录', '附錄', 'appendix', '付録', '부록', 'annexe', 'приложение', 'Anhang', 'anexo', 'allegato'),
    ('前言', '前言', 'preface', '前言', '서문', 'préface', 'предисловие', 'Vorwort', 'prefacio', 'prefazione'),
    ('版本', '版本', 'version', 'バージョン', '버전', 'version', 'версия', 'Version', 'versión', 'versione'),
    ('页码', '頁碼', 'page number', 'ページ番号', '페이지 번호', 'numéro de page', 'номер страницы', 'Seitennummer', 'número de página', 'numero di pagina'),
    ('段落', '段落', 'paragraph', '段落', '문단', 'paragraphe', 'абзац', 'Absatz', 'párrafo', 'paragrafo'),
    ('行', '行', 'line', '行', '줄', 'ligne', 'строка', 'Zeile', 'línea', 'riga'),
    ('列', '列', 'column', '列', '열', 'colonne', 'столбец', 'Spalte', 'columna', 'colonna'),
    ('表格', '表格', 'table', '表', '표', 'tableau', 'таблица', 'Tabelle', 'tabla', 'tabella'),
    ('图表', '圖表', 'chart', 'グラフ', '차트', 'graphique', 'график', 'Diagramm', 'gráfico', 'grafico'),
    ('图形', '圖形', 'figure', '図', '그림', 'figure', 'рисунок', 'Abbildung', 'figura', 'figura'),
    ('图片', '圖片', 'image', '画像', '이미지', 'image', 'изображение', 'Bild', 'imagen', 'immagine'),
    ('文字', '文字', 'text', 'テキスト', '텍스트', 'texte', 'текст', 'Text', 'texto', 'testo'),
    ('书签', '書籤', 'bookmark', 'ブックマーク', 'ブックマーク', 'signet', 'закладка', 'Lesezeichen', 'marcapáginas', 'segnalibro'),
    ('水印', '浮水印', 'watermark', '透かし', '워터마크', 'filigrane', 'водяной знак', 'Wasserzeichen', 'marca de agua', 'filigrana'),
    ('版权', '版權', 'copyright', '著作権', '저작권', 'droit d\u2019auteur', 'авторское право', 'Urheberrecht', 'derecho de autor', 'diritto d\u2019autore'),
    ('手册', '手冊', 'manual', 'マニュアル', '매뉴얼', 'manuel', 'руководство', 'Handbuch', 'manual', 'manuale'),
    ('日期', '日期', 'date', '日付', '날짜', 'date', 'дата', 'Datum', 'fecha', 'data'),
    ('时间', '時間', 'time', '時間', '시간', 'temps', 'время', 'Zeit', 'tiempo', 'tempo'),
    ('年', '年', 'year', '年', '년', 'année', 'год', 'Jahr', 'año', 'anno'),
    ('月', '月', 'month', '月', '월', 'mois', 'месяц', 'Monat', 'mes', 'mese'),
    ('日', '日', 'day', '日', '일', 'jour', 'день', 'Tag', 'día', 'giorno'),
    ('星期', '星期', 'week', '週', '주', 'semaine', 'неделя', 'Woche', 'semana', 'settimana'),
    # ---------- 操作 ----------
    ('打开', '開啟', 'open', '開く', '열다', 'ouvrir', 'открыть', 'öffnen', 'abrir', 'aprire'),
    ('关闭', '關閉', 'close', '閉じる', '닫기', 'fermer', 'закрыть', 'schließen', 'cerrar', 'chiudere'),
    ('保存', '儲存', 'save', '保存', '저장', 'enregistrer', 'сохранить', 'speichern', 'guardare', 'salvare'),
    ('编辑', '編輯', 'edit', '編集', '편집', 'modifier', 'редактировать', 'bearbeiten', 'editar', 'modificare'),
    ('复制', '複製', 'copy', 'コピー', '복사', 'copier', 'копировать', 'kopieren', 'copiar', 'copiare'),
    ('删除', '刪除', 'delete', '削除', '삭제', 'supprimer', 'удалить', 'löschen', 'eliminar', 'eliminare'),
    ('插入', '插入', 'insert', '挿入', '삽입', 'insérer', 'вставить', 'einfügen', 'insertar', 'inserire'),
    ('导出', '匯出', 'export', 'エクスポート', '내보내기', 'exporter', 'экспортировать', 'exportieren', 'exportar', 'esportare'),
    ('导入', '匯入', 'import', 'インポート', '가져오기', 'importer', 'импортировать', 'importieren', 'importar', 'importare'),
    ('搜索', '搜尋', 'search', '検索', '검색', 'rechercher', 'искать', 'suchen', 'buscar', 'cercare'),
    ('替换', '取代', 'replace', '置換', '바꾸기', 'remplacer', 'заменить', 'ersetzen', 'reemplazar', 'sostituire'),
    ('打印', '列印', 'print', '印刷', '인쇄', 'imprimer', 'печатать', 'drucken', 'imprimir', 'stampare'),
    ('扫描', '掃描', 'scan', 'スキャン', '스캔', 'numériser', 'сканировать', 'scannen', 'escanear', 'scansionare'),
    ('识别', '識別', 'recognize', '認識', '인식', 'reconnaître', 'распознавать', 'erkennen', 'reconocer', 'riconoscere'),
    ('旋转', '旋轉', 'rotate', '回転', '회전', 'pivoter', 'вращать', 'drehen', 'rotar', 'ruotare'),
    ('裁剪', '裁剪', 'crop', '切り抜き', '자르기', 'rogner', 'обрезать', 'zuschneiden', 'recortar', 'ritagliare'),
    ('合并', '合併', 'merge', '結合', '병합', 'fusionner', 'объединить', 'zusammenführen', 'combinar', 'unire'),
    ('拆分', '拆分', 'split', '分割', '분할', 'diviser', 'разделить', 'aufteilen', 'dividir', 'dividere'),
    ('压缩', '壓縮', 'compress', '圧縮', '압축', 'compresser', 'сжимать', 'komprimieren', 'comprimir', 'comprimere'),
    ('加密', '加密', 'encrypt', '暗号化', '암호화', 'chiffrer', 'шифровать', 'verschlüsseln', 'cifrar', 'crittografare'),
    ('取消', '取消', 'cancel', 'キャンセル', '취소', 'annuler', 'отменить', 'abbrechen', 'cancelar', 'annullare'),
    ('确认', '確認', 'confirm', '確認', '확인', 'confirmer', 'подтвердить', 'bestätigen', 'confirmar', 'confermare'),
    ('提交', '提交', 'submit', '送信', '제출', 'soumettre', 'отправить', 'einreichen', 'enviar', 'inviare'),
    ('开始', '開始', 'start', '開始', '시작', 'commencer', 'начать', 'starten', 'empezar', 'iniziare'),
    ('结束', '結束', 'end', '終了', '종료', 'terminer', 'закончить', 'beenden', 'terminar', 'terminare'),
    ('选择', '選擇', 'select', '選択', '선택', 'sélectionner', 'выбрать', 'auswählen', 'seleccionar', 'selezionare'),
    ('翻译', '翻譯', 'translate', '翻訳', '번역', 'traduire', 'переводить', 'übersetzen', 'traducir', 'tradurre'),
    # ---------- 属性与设置 ----------
    ('设置', '設定', 'settings', '設定', '설정', 'paramètres', 'настройки', 'Einstellungen', 'ajustes', 'impostazioni'),
    ('选项', '選項', 'option', 'オプション', '옵션', 'option', 'вариант', 'Option', 'opción', 'opzione'),
    ('语言', '語言', 'language', '言語', '언어', 'langue', 'язык', 'Sprache', 'idioma', 'lingua'),
    ('名称', '名稱', 'name', '名前', '이름', 'nom', 'название', 'Name', 'nombre', 'nome'),
    ('大小', '大小', 'size', 'サイズ', '크기', 'taille', 'размер', 'Größe', 'tamaño', 'dimensione'),
    ('字体', '字體', 'font', 'フォント', '폰트', 'police', 'шрифт', 'Schriftart', 'fuente', 'carattere'),
    ('颜色', '顏色', 'color', '色', '색', 'couleur', 'цвет', 'Farbe', 'color', 'colore'),
    ('粗体', '粗體', 'bold', '太字', '굵게', 'gras', 'жирный', 'fett', 'negrita', 'grassetto'),
    ('斜体', '斜體', 'italic', 'イタリック', '기울임', 'italique', 'курсив', 'Kursiv', 'cursiva', 'corsivo'),
    ('样式', '樣式', 'style', 'スタイル', '양식', 'style', 'стиль', 'Stil', 'estilo', 'stile'),
    ('格式', '格式', 'format', 'フォーマット', '서식', 'format', 'формат', 'Format', 'formato', 'formato'),
    ('类型', '類型', 'type', 'タイプ', '유형', 'type', 'тип', 'Typ', 'tipo', 'tipo'),
    ('类别', '類別', 'category', 'カテゴリ', '분류', 'catégorie', 'категория', 'Kategorie', 'categoría', 'categoria'),
    ('数量', '數量', 'quantity', '数量', '수량', 'quantité', 'количество', 'Menge', 'cantidad', 'quantità'),
    ('数字', '數字', 'number', '数', '숫자', 'numéro', 'номер', 'Nummer', 'número', 'numero'),
    ('状态', '狀態', 'status', 'ステータス', '状態', 'état', 'статус', 'Status', 'estado', 'stato'),
    ('全部', '全部', 'all', 'すべて', '모두', 'tout', 'всё', 'alle', 'todo', 'tutto'),
    ('部分', '部分', 'part', '部分', '부분', 'partie', 'часть', 'Teil', 'parte', 'parte'),
    ('第一', '第一', 'first', '最初', '첫째', 'premier', 'первый', 'erste', 'primero', 'primo'),
    ('最后', '最後', 'last', '最後', '마지막', 'dernier', 'последний', 'letzte', 'último', 'ultimo'),
    ('下一个', '下一個', 'next', '次', '다음', 'suivant', 'следующий', 'nächste', 'siguiente', 'successivo'),
    ('上一个', '上一個', 'previous', '前', '이전', 'précédent', 'предыдущий', 'vorherige', 'anterior', 'precedente'),
    ('新的', '新的', 'new', '新しい', '새로운', 'nouveau', 'новый', 'neu', 'nuevo', 'nuovo'),
    ('旧的', '旧的', 'old', '古い', '오래된', 'ancien', 'старый', 'alt', 'viejo', 'vecchio'),
    ('左', '左', 'left', '左', '좌', 'gauche', 'левый', 'links', 'izquierda', 'sinistra'),
    ('右', '右', 'right', '右', '우', 'droite', 'правый', 'rechts', 'derecha', 'destra'),
    ('顶部', '頂部', 'top', '上部', '상단', 'haut', 'верх', 'oben', 'superior', 'parte superiore'),
    ('底部', '底部', 'bottom', '下部', '하단', 'bas', 'низ', 'unten', 'inferior', 'parte inferiore'),
    # ---------- 内容与信息 ----------
    ('内容', '內容', 'content', '内容', '콘텐츠', 'contenu', 'содержимое', 'Inhalt', 'contenido', 'contenuto'),
    ('信息', '資訊', 'information', '情報', '정보', 'information', 'информация', 'Information', 'información', 'informazione'),
    ('数据', '數據', 'data', 'データ', '데이터', 'données', 'данные', 'Daten', 'datos', 'dati'),
    ('系统', '系統', 'system', 'システム', '시스템', 'système', 'система', 'System', 'sistema', 'sistema'),
    ('网络', '網路', 'network', 'ネットワーク', '네트워크', 'réseau', 'сеть', 'Netzwerk', 'red', 'rete'),
    ('设备', '設備', 'device', 'デバイス', '기기', 'appareil', 'устройство', 'Gerät', 'dispositivo', 'dispositivo'),
    ('软件', '軟體', 'software', 'ソフトウェア', '소프트웨어', 'logiciel', 'программа', 'Software', 'software', 'software'),
    ('硬件', '硬體', 'hardware', 'ハードウェア', '하드웨어', 'matériel', 'оборудование', 'Hardware', 'hardware', 'hardware'),
    ('项目', '專案', 'project', 'プロジェクト', '프로젝트', 'projet', 'проект', 'Projekt', 'proyecto', 'progetto'),
    ('条目', '條目', 'item', '項目', '항목', 'élément', 'элемент', 'Element', 'elemento', 'elemento'),
    ('记录', '記錄', 'record', '記録', '기록', 'enregistrement', 'запись', 'Datensatz', 'registro', 'registrazione'),
    ('清单', '清單', 'list', 'リスト', '목록', 'liste', 'список', 'Liste', 'lista', 'lista'),
    ('模板', '範本', 'template', 'テンプレート', '템플릿', 'modèle', 'шаблон', 'Vorlage', 'plantilla', 'modello'),
    ('计划', '計畫', 'plan', '計画', '계획', 'plan', 'план', 'Plan', 'plan', 'piano'),
    ('术语', '術語', 'term', '用語', '용어', 'terme', 'термин', 'Begriff', 'término', 'termine'),
    ('定义', '定義', 'definition', '定義', '정의', 'définition', 'определение', 'Definition', 'definición', 'definizione'),
    ('示例', '示例', 'example', '例', '예시', 'exemple', 'пример', 'Beispiel', 'ejemplo', 'esempio'),
    ('步骤', '步驟', 'step', '手順', '단계', 'étape', 'шаг', 'Schritt', 'paso', 'passo'),
    ('流程', '流程', 'process', 'プロセス', '프로세스', 'processus', 'процесс', 'Ablauf', 'proceso', 'processo'),
    ('规则', '規則', 'rule', 'ルール', '규칙', 'règle', 'правило', 'Regel', 'regla', 'regola'),
    ('要求', '要求', 'requirement', '要件', '요구 사항', 'exigence', 'требование', 'Anforderung', 'requisito', 'requisito'),
    ('标准', '標準', 'standard', '標準', '기준', 'norme', 'стандарт', 'Standard', 'estándar', 'standard'),
    ('说明', '說明', 'description', '説明', '설명', 'description', 'описание', 'Beschreibung', 'descripción', 'descrizione'),
    ('备注', '備註', 'note', 'メモ', '메모', 'note', 'примечание', 'Notiz', 'nota', 'nota'),
    ('结果', '結果', 'result', '結果', '결과', 'résultat', 'результат', 'Ergebnis', 'resultado', 'risultato'),
    ('问题', '問題', 'problem', '問題', '문제', 'problème', 'проблема', 'Problem', 'problema', 'problema'),
    ('答案', '答案', 'answer', '回答', '대답', 'réponse', 'ответ', 'Antwort', 'respuesta', 'risposta'),
    ('输入', '輸入', 'input', '入力', '입력', 'saisie', 'ввод', 'Eingabe', 'entrada', 'input'),
    ('输出', '輸出', 'output', '出力', '출력', 'sortie', 'вывод', 'Ausgabe', 'salida', 'uscita'),
    # ---------- 质量与安全 ----------
    ('质量', '質量', 'quality', '品質', '품질', 'qualité', 'качество', 'Qualität', 'calidad', 'qualità'),
    ('安全', '安全', 'security', 'セキュリティ', '보안', 'sécurité', 'безопасность', 'Sicherheit', 'seguridad', 'sicurezza'),
    ('风险', '風險', 'risk', 'リスク', '위험', 'risque', 'риск', 'Risiko', 'riesgo', 'rischio'),
    ('错误', '錯誤', 'error', 'エラー', '오류', 'erreur', 'ошибка', 'Fehler', 'error', 'errore'),
    ('警告', '警告', 'warning', '警告', '경고', 'avertissement', 'предупреждение', 'Warnung', 'advertencia', 'avviso'),
    ('成功', '成功', 'success', '成功', '성공', 'succès', 'успех', 'Erfolg', 'éxito', 'successo'),
    ('失败', '失敗', 'failure', '失敗', '失敗', 'échec', 'неудача', 'Misserfolg', 'fallo', 'fallimento'),
    # ---------- 业务 ----------
    ('公司', '公司', 'company', '会社', '회사', 'société', 'компания', 'Unternehmen', 'empresa', 'azienda'),
    ('客户', '客戶', 'customer', '顧客', '고객', 'client', 'клиент', 'Kunde', 'cliente', 'cliente'),
    ('产品', '產品', 'product', '製品', '제품', 'produit', 'товар', 'Produkt', 'producto', 'prodotto'),
    ('服务', '服務', 'service', 'サービス', '서비스', 'service', 'услуга', 'Dienstleistung', 'servicio', 'servizio'),
    ('市场', '市場', 'market', '市場', '시장', 'marché', 'рынок', 'Markt', 'mercado', 'mercato'),
    ('生产', '生產', 'production', '生産', '생산', 'production', 'производство', 'Produktion', 'producción', 'produzione'),
    ('合同', '合約', 'contract', '契約', '계약', 'contrat', 'договор', 'Vertrag', 'contrato', 'contratto'),
    ('协议', '協議', 'agreement', '合意', '합의', 'accord', 'соглашение', 'Vereinbarung', 'acuerdo', 'accordo'),
    ('发票', '發票', 'invoice', '請求書', '인보이스', 'facture', 'счёт-фактура', 'Rechnung', 'factura', 'fattura'),
    ('收据', '收據', 'receipt', '領収書', '영수증', 'reçu', 'квитанция', 'Quittung', 'recibo', 'ricevuta'),
    ('报告', '報告', 'report', 'レポート', '보고서', 'rapport', 'отчёт', 'Bericht', 'informe', 'rapporto'),
    ('订单', '訂單', 'order', '注文', '주문', 'commande', 'заказ', 'Bestellung', 'pedido', 'ordine'),
    ('付款', '付款', 'payment', '支払い', '지급', 'paiement', 'оплата', 'Zahlung', 'pago', 'pagamento'),
    ('价格', '價格', 'price', '価格', '가격', 'prix', 'цена', 'Preis', 'precio', 'prezzo'),
    ('金额', '金額', 'amount', '金額', '금액', 'montant', 'сумма', 'Betrag', 'importe', 'importo'),
    ('合计', '合計', 'total', '合計', '합계', 'total', 'итог', 'Gesamtsumme', 'total', 'totale'),
    ('费用', '費用', 'fee', '手数料', '수수료', 'frais', 'сбор', 'Gebühr', 'tarifa', 'tariffa'),
    ('税', '稅', 'tax', '税', '세금', 'taxe', 'налог', 'Steuer', 'impuesto', 'imposta'),
    ('折扣', '折扣', 'discount', '割引', '할인', 'remise', 'скидка', 'Rabatt', 'descuento', 'sconto'),
    ('账户', '帳戶', 'account', 'アカウント', '계정', 'compte', 'счёт', 'Konto', 'cuenta', 'conto'),
    ('部门', '部門', 'department', '部門', '부서', 'département', 'отдел', 'Abteilung', 'departamento', 'repartizione'),
    ('员工', '員工', 'staff', 'スタッフ', '직원', 'personnel', 'персонал', 'Personal', 'personal', 'personale'),
    ('会议', '會議', 'meeting', '会議', '회의', 'réunion', 'встреча', 'Besprechung', 'reunión', 'riunione'),
    ('培训', '培訓', 'training', '研修', '교육', 'formation', 'обучение', 'Schulung', 'formación', 'formazione'),
    ('日程', '日程', 'schedule', 'スケジュール', '일정', 'planning', 'расписание', 'Zeitplan', 'calendario', 'calendario'),
    ('地址', '地址', 'address', '住所', '주소', 'adresse', 'адрес', 'Adresse', 'dirección', 'indirizzo'),
    ('电话', '電話', 'phone', '電話', '전화', 'téléphone', 'телефон', 'Telefon', 'teléfono', 'telefono'),
    ('邮件', '郵件', 'email', 'メール', '이메일', 'courriel', 'электронная почта', 'E-Mail', 'correo electrónico', 'email'),
    ('签名', '簽名', 'signature', '署名', '서명', 'signature', 'подпись', 'Unterschrift', 'firma', 'firma'),
    ('密码', '密碼', 'password', 'パスワード', '비밀번호', 'mot de passe', 'пароль', 'Passwort', 'contraseña', 'password'),
    ('用户名', '用戶名', 'username', 'ユーザー名', '사용자 이름', 'nom d\u2019utilisateur', 'имя пользователя', 'Benutzername', 'nombre de usuario', 'nome utente'),
    ('用户', '用戶', 'user', 'ユーザー', '사용자', 'utilisateur', 'пользователь', 'Benutzer', 'usuario', 'utente'),
    ('帮助', '幫助', 'help', 'ヘルプ', '도움말', 'aide', 'помощь', 'Hilfe', 'ayuda', 'aiuto'),
    ('国家', '國家', 'country', '国', '국가', 'pays', 'страна', 'Land', 'país', 'paese'),
    ('城市', '城市', 'city', '都市', '도시', 'ville', 'город', 'Stadt', 'ciudad', 'città'),
    ('地区', '地區', 'region', '地域', '지역', 'région', 'регион', 'Region', 'región', 'regione'),
    ('地点', '地點', 'location', '場所', '장소', 'lieu', 'место', 'Ort', 'ubicación', 'luogo'),
    # ---------- 固定短语（词表里唯一的「整句命中」样本，用来演示短语级匹配） ----------
    ('版权所有', '版權所有', 'all rights reserved', '無断転載を禁じます', '모든 권리 보유',
     'Tous droits réservés', 'Все права защищены', 'Alle Rechte vorbehalten',
     'Todos los derechos reservados', 'Tutti i diritti riservati'),
    ('内容目录', '內容目錄', 'table of contents', '内容目次', '본문 차례',
     'table des matières', 'оглавление', 'Inhaltsangabe', 'índice de contenidos', 'indice dei contenuti'),
    ('客户服务', '客戶服務', 'customer service', '顧客サービス', '고객 서비스',
     'service client', 'обслуживание клиентов', 'Kundenservice', 'atención al cliente', 'servizio clienti'),
    # ---------- 原样保留的专名（十格同词：命中=「这词不用翻」，coverage 才算得老实） ----------
    ('PDF', 'PDF', 'PDF', 'PDF', 'PDF', 'PDF', 'PDF', 'PDF', 'PDF', 'PDF'),
    ('Word', 'Word', 'Word', 'Word', 'Word', 'Word', 'Word', 'Word', 'Word', 'Word'),
    ('Excel', 'Excel', 'Excel', 'Excel', 'Excel', 'Excel', 'Excel', 'Excel', 'Excel', 'Excel'),
    ('中文', '中文', 'Chinese', '中国語', '중국어', 'chinois', 'китайский язык', 'Chinesisch', 'chino', 'cinese'),
    ('英语', '英語', 'English', '英語', '영어', 'anglais', 'английский язык', 'Englisch', 'inglés', 'inglese'),
    ('日语', '日語', 'Japanese', '日本語', '일본어', 'japonais', 'японский язык', 'Japanisch', 'japonés', 'giapponese'),
    ('韩语', '韓語', 'Korean', '韓国語', '한국어', 'coréen', 'корейский язык', 'Koreanisch', 'coreano', 'coreano'),
    ('法语', '法語', 'French', 'フランス語', '프랑스어', 'français', 'французский язык', 'Französisch', 'francés', 'francese'),
    ('德语', '德語', 'German', 'ドイツ語', '독일어', 'allemand', 'немецкий язык', 'Deutsch', 'alemán', 'tedesco'),
    ('俄语', '俄語', 'Russian', 'ロシア語', '러시아어', 'russe', 'русский язык', 'Russisch', 'ruso', 'russo'),
    ('西班牙语', '西班牙語', 'Spanish', 'スペイン語', '스페인어', 'espagnol', 'испанский язык', 'Spanisch', 'español', 'spagnolo'),
    ('意大利语', '意大利語', 'Italian', 'イタリア語', '이탈리아어', 'italien', 'итальянский язык', 'Italienisch', 'italiano', 'italiano'),
)

# 别名：同一概念的另一种常见写法，写成 (别名, 表里已有的词面) 而不是再复制一行，
# 是为了「一个概念只有一处定义」——以后改译文只改一行，不会出现同一个词两行译法不同。
# 别名不许指向别名：摊平时按顺序解，链式会断（断了会留一条日志，不会静默失效）。
_GLOSSARY_ALIASES = (
    ('页', '页面'),          # 「第 3 页」里的单字
    ('頁', '页面'),
    ('pdf', 'PDF'),
    ('word', 'Word'),
    ('excel', 'Excel'),
)

_WORD_RE = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*|\d+|[^\W\d_]", re.UNICODE)
_CJK_RE = re.compile(r'[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]')

_INDEX = None       # 词面（小写） -> 行
_CJK_KEYS = None    # 含 CJK 的词面集合（按长度降序，做最长匹配）
_MAX_WORDS = 1      # 表里多词短语最多几个词


def _form_index():
    """把词表摊平成「任意一格写法 → 整行」的索引，只做一次。

    同形词（英语 `data` 和意大利语 `data` 是两个概念、`firma` 在德语是「公司」在西语是
    「签名」……）只能有一个赢家，规则是**英文列优先**：这个兜底表服务的输入绝大多数是
    英文或带英文术语的 PDF，把英文当准绳最不容易翻错概念；同一行的其它列照样能当入口，
    只是不再抢走已经被英文占住的写法。
    还剩别的撞词（两边都不是英文列）时先到先得，并在日志里留一句——词表长到几百条以后
    「为什么这个词翻成了那个意思」是最难查的悬案，当场记一笔比事后猜便宜。
    """
    global _INDEX, _CJK_KEYS, _MAX_WORDS
    if _INDEX is not None:
        return _INDEX
    index = {}
    en_col = GLOSSARY_LANGS.index('en')
    taken_by_en = set()
    for row in _GLOSSARY:                       # 第一遍：英文列，占住就是最终答案
        key = str(row[en_col] or '').strip().lower()
        if key:
            index[key] = row
            taken_by_en.add(key)
            _MAX_WORDS = max(_MAX_WORDS, len(key.split()))
    for row in _GLOSSARY:                       # 第二遍：其它语言的写法当补充入口
        for col, form in enumerate(row):
            if col == en_col:
                continue
            key = str(form).strip().lower()
            if not key:
                continue
            if key in index and index[key] is not row:
                if key in taken_by_en:
                    continue                    # 英文列已经定性的同形词，安静让位
                logging.warning('translate: 词表里 "%s" 出现两次，保留先出现的那一行', key)
                continue
            index[key] = row
            _MAX_WORDS = max(_MAX_WORDS, len(key.split()))
    for alias, target in _GLOSSARY_ALIASES:
        hit = index.get(str(target).strip().lower())
        if hit is None:
            logging.warning('translate: 别名 "%s" 指向没有的词面 "%s"，已忽略', alias, target)
            continue
        index.setdefault(str(alias).strip().lower(), hit)
    _CJK_KEYS = sorted((k for k in index if _CJK_RE.search(k)),
                       key=len, reverse=True)
    _INDEX = index
    return index


def glossary_size():
    """词表规模（行数 / 可作入口的词面数），给 `get_translate_providers` 如实报覆盖力。"""
    index = _form_index()
    return {'rows': len(_GLOSSARY), 'forms': len(index)}


# ==================== offline 档 ====================
def _column_of(target):
    """目标语言在词表里是第几列；不在十种之内返回 None（调用方必须报错，不许硬翻）。"""
    try:
        return GLOSSARY_LANGS.index(target)
    except ValueError:
        return None


def _match_cjk_run(run, index, column):
    """一段中日韩连续字里做最长匹配，返回 (拼出来的译文, 命中字数, 总字数)。"""
    out = []
    hit_chars = 0
    i = 0
    total = len(run)
    while i < total:
        best = None
        for key in _CJK_KEYS:
            if len(key) > total - i:
                continue
            if run[i:i + len(key)].lower() == key:
                best = key
                break
        if best:
            row = index[best]
            out.append(row[column])
            hit_chars += len(best)
            i += len(best)
        else:
            out.append(run[i])
            i += 1
    return ''.join(out), hit_chars, total


def offline_translate(text, target):
    """词表级翻译：整句命中 → 多词短语命中 → 单词命中 → 原样抄回。

    返回 `(译文, coverage)`。coverage = 命中的「词」占全部词数的比例（数字、标点不计算在内：
    它们本来就不需要翻，算进分母会把覆盖率算低、骗人）。
    全句没命中任何一个词时 coverage 是 0.0、译文等于原文——这正是兜底该有的样子。
    """
    raw = str(text or '')
    if not raw.strip():
        return raw, None                     # 空文本不参与覆盖率统计
    index = _form_index()
    column = _column_of(target)
    if column is None:
        return raw, 0.0
    whole = raw.strip().lower()
    hit = index.get(whole)
    if hit is not None:
        return hit[column], 1.0

    tokens = [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(raw)]
    if not tokens:
        return raw, None
    pieces = []
    cursor = 0                # 上一个替换结束在 raw 里的位置（分隔符原样抄）
    matched_units = 0.0
    total_units = 0.0
    i = 0
    while i < len(tokens):
        word, start, end = tokens[i]
        digits_only = word.isdigit()
        if not digits_only:
            total_units += 1
        row = None
        span = 1
        # 1) 拉丁多词短语：从最长往下试，命中就一次吃掉这几个词
        if not digits_only and _MAX_WORDS > 1 and not _CJK_RE.search(word):
            for size in range(min(_MAX_WORDS, len(tokens) - i), 1, -1):
                window = tokens[i:i + size]
                # 短语只在「词与词之间正好一个空格」时匹配：否则 "table - of" 会被拼成
                # "table of" 命中不该命中的地方，改出来的句子连不上原文
                joined = ' '.join(w[0] for w in window).lower()
                gap_ok = all(window[k][2] + 1 == window[k + 1][1]
                             and raw[window[k][2]:window[k + 1][1]] == ' '
                             for k in range(size - 1))
                candidate = index.get(joined)
                if candidate is not None and gap_ok:
                    row, span = candidate, size
                    break
        # 2) 单词命中
        if row is None and not digits_only:
            if _CJK_RE.search(word):
                new, hit_chars, total_chars = _match_cjk_run(word, index, column)
                pieces.append(raw[cursor:start])
                pieces.append(new)
                cursor = end
                matched_units += (hit_chars / float(total_chars)) if total_chars else 0.0
                i += 1
                continue
            row = index.get(word.lower())
        if row is not None:
            # 命中范围是 span 个词（短语就是好多个），所以替换要一次跳到**最后一个词**的末尾，
            # 不然中间那几个原词会跟着后面的分隔文本一起再抄一遍（译文里就出现「内容目录 of contents」）
            stop = tokens[i + span - 1][2]
            pieces.append(raw[cursor:start])
            pieces.append(row[column])
            cursor = stop
            matched_units += span
            for extra in tokens[i + 1:i + span]:
                if not extra[0].isdigit():
                    total_units += 1
        i += span
    pieces.append(raw[cursor:])
    result = ''.join(pieces)
    coverage = (matched_units / total_units) if total_units else None
    return result, coverage


class OfflineProvider:
    """随包词表兜底档：永远可用、不联网、不要密钥，但**它不是机器翻译**。

    交给用户之前必须带着 `coverage` 和 `note='fallback_glossary'` 一起说清楚，
    否则就是拿单词卡冒充译文。
    """

    name = PROVIDER_OFFLINE
    needs = ()

    @property
    def configured(self):
        return True

    def translate_batch(self, texts, source, target):
        if _column_of(target) is None:
            return {'ok': False, 'error': 'unsupported_target',
                    'detail': {'target': target, 'supported': list(TARGET_LANGS)}}
        rows = []
        # coverage 与 texts **逐位对齐**（算不出来的那一格给 None，不是干脆少一项）：
        # 少一项会让调用方按下标配对位时错位，把 A 句的命中率记到 B 句头上。
        coverages = []
        for raw in texts:
            dst, coverage = offline_translate(raw, target)
            rows.append({'src': raw, 'dst': dst})
            coverages.append(None if coverage is None else round(coverage, 4))
        return {'ok': True, 'results': rows, 'coverage': coverages,
                'note': 'fallback_glossary'}


# ==================== http 档 ====================
class HttpProvider:
    """可配置服务档：地址没配就是「没有服务」，不编译文、不静默退回词表。

    只在 `translate_url` 非空时才谈「可用」；`translate_api_key` 可空（匿名服务）。
    网络类失败（超时 / 连不上 / 5xx）重试一次就放弃；4xx 不重试（重试不改变结论）。
    """

    name = PROVIDER_HTTP
    needs = ('translate_url',)

    def __init__(self, settings=None):
        conf = settings or {}
        self.url = str(conf.get('translate_url') or '').strip()
        self.key = str(conf.get('translate_api_key') or '').strip()
        self.timeout = _as_float(conf.get('translate_timeout'), DEFAULT_TIMEOUT)
        self.batch_max = _as_int(conf.get('translate_batch_max'), MAX_BATCH)
        self.lang_map = conf.get('translate_lang_map') or {}

    @property
    def configured(self):
        return bool(self.url)

    def _map(self, code):
        """本软件的语言码 → 服务方要的那种写法（没配改名表就原样发）。"""
        return self.lang_map.get(code, code) if isinstance(self.lang_map, dict) else code

    def translate_batch(self, texts, source, target):
        if not self.url:
            return {'ok': False, 'error': 'no_url',
                    'detail': {'setting': 'translate_url'}}
        if not self.url.lower().startswith(('http://', 'https://')):
            return {'ok': False, 'error': 'bad_url', 'detail': {'url': self.url}}
        payload = {'texts': list(texts), 'source': self._map(source or 'auto'),
                   'target': self._map(target), 'provider': self.name}
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        last_code = 'unreachable'
        last_detail = ''
        attempts = 0
        for attempt in range(1, RETRY + 2):
            attempts = attempt
            try:
                raw = self._post(body)
            except _urlerror.HTTPError as error:
                code = int(getattr(error, 'code', 0) or 0)
                last_detail = 'HTTP %s' % code
                if 400 <= code < 500:
                    # 4xx：地址/密钥/请求体错了，重试还是同一个答案，直接如实报错
                    return {'ok': False, 'error': 'bad_status', 'attempts': attempts,
                            'detail': {'status': code, 'attempts': attempts,
                                       'timeout': self.timeout,
                                       'url': _safe_url(self.url)}}
                last_code = 'server_error'
            except (socket.timeout, TimeoutError) as error:
                # 3.10+ 里 socket.timeout 就是 TimeoutError，两个名字一起写只为兼容老解释器
                last_code, last_detail = 'timeout', '%s (limit %ss)' % (error, self.timeout)
            except _urlerror.URLError as error:
                reason = getattr(error, 'reason', error)
                if isinstance(reason, (socket.timeout, TimeoutError)):
                    last_code, last_detail = 'timeout', str(reason)
                else:
                    last_code, last_detail = 'unreachable', '%s: %s' % (
                        type(reason).__name__, reason)
            except Exception as error:            # 解码炸了、对端提前断开……都算服务不可用
                last_code, last_detail = 'unreachable', '%s: %s' % (
                    type(error).__name__, error)
            else:
                # 拿到了正文：形状不对/条数不对/空译文都是「服务没答上」，当场如实报错，
                # 不重试（同一份请求重发只会得到同一个坏答案），也不半途返回几格空译文
                parsed = _parse_response(raw, texts)
                parsed['attempts'] = attempts
                return parsed
            if attempt <= RETRY:
                logging.info('translate: 第 %d 次请求没成（%s %s），重试一次',
                             attempts, last_code, last_detail)
        return {'ok': False, 'error': last_code, 'attempts': attempts,
                'detail': {'attempts': attempts, 'reason': last_detail, 'status': None,
                           'url': _safe_url(self.url), 'timeout': self.timeout}}

    def _post(self, body):
        """一次 POST，只回正文字节。任何 HTTP 层错误都原样抛给上面的循环去归类。"""
        request = _urlrequest.Request(self.url, data=body)
        request.add_header('Content-Type', 'application/json; charset=utf-8')
        request.add_header('Accept', 'application/json')
        # UA 里不写密钥也不写本机名，只留一个能对着日志认出来的标识
        request.add_header('User-Agent', 'PDFToolkit-translate/%d' % MODULE_VERSION)
        if self.key:
            request.add_header('Authorization', 'Bearer %s' % self.key)
        with _urlrequest.urlopen(request, timeout=self.timeout) as response:
            # urlopen 对 >=400 的状态码是抛 HTTPError，能走到这里就是 2xx/3xx；
            # 但仍要防一手「中间层把错误码吞了还回 200」——那种空壳答案比报错更坏。
            status = getattr(response, 'status', None) or response.getcode()
            raw = response.read()
        if status and int(status) >= 400:
            raise _urlerror.HTTPError(self.url, int(status), 'status %s' % status, {}, None)
        return raw


def _safe_url(url):
    """日志/报错里只放地址的主机与路径，query 里可能被误当密钥的东西一律抹掉。"""
    text = str(url or '')
    return text.split('?', 1)[0][:120]


def _parse_response(raw, texts):
    """把服务返回的字节收成 `results`。形状不对/条数不对/空译文 → 如实失败。"""
    try:
        data = json.loads(raw.decode('utf-8', 'replace'))
    except Exception as error:
        return {'ok': False, 'error': 'bad_payload',
                'detail': {'reason': 'not json: %s' % error}}
    rows = data
    if isinstance(data, dict):
        for field in ('results', 'translations', 'data', 'items'):
            if isinstance(data.get(field), list):
                rows = data[field]
                break
    if not isinstance(rows, list):
        return {'ok': False, 'error': 'bad_payload',
                'detail': {'reason': 'no list of results'}}
    if len(rows) != len(texts):
        return {'ok': False, 'error': 'bad_payload',
                'detail': {'reason': 'want %d got %d' % (len(texts), len(rows))}}
    out = []
    for index, (src, item) in enumerate(zip(texts, rows)):
        if isinstance(item, str):
            dst = item
        elif isinstance(item, dict):
            dst = ''
            for field in ('dst', 'translation', 'text', 'target', 'translated'):
                if item.get(field) is not None:
                    dst = item[field]
                    break
            if not isinstance(dst, str):
                return {'ok': False, 'error': 'bad_payload',
                        'detail': {'reason': 'result %d not a string' % index}}
        else:
            return {'ok': False, 'error': 'bad_payload',
                    'detail': {'reason': 'result %d not usable' % index}}
        if not str(dst).strip() and str(src).strip():
            # 服务回了个空格，而原文不是空——**不能**当成翻好了（这正是「假装成功」的样子）
            return {'ok': False, 'error': 'bad_payload',
                    'detail': {'reason': 'empty translation at %d' % index,
                               'src': str(src)[:60]}}
        out.append({'src': src, 'dst': dst})
    return {'ok': True, 'results': out}


# ==================== provider 注册表 ====================
_REGISTRY = {
    PROVIDER_OFFLINE: OfflineProvider,
    PROVIDER_HTTP: HttpProvider,
}


def register(name, factory, replace=False):
    """登记一档 provider（第三方引擎、以及验收用的假 provider 都从这里进来）。

    factory 可以是类也可以是无参可调用对象；本层只要求它满足文件头那个最小接口。
    `replace=False` 时撞名直接拒绝——静默盖掉别人的 provider 会让「为什么跑的不是我那档」
    变成一桩查不出来的悬案。
    """
    key = str(name or '').strip()
    if not key or not callable(factory):
        return False
    if key in _REGISTRY and not replace:
        return False
    _REGISTRY[key] = factory
    return True


def unregister(name):
    """撤掉登记（验收脚本收尾用）；内置的两档撤不掉，防止把程序弄成没有 provider。"""
    key = str(name or '').strip()
    if key in (PROVIDER_OFFLINE, PROVIDER_HTTP):
        return False
    return _REGISTRY.pop(key, None) is not None


def provider_names():
    return sorted(_REGISTRY)


def get_provider(name='', settings=None):
    """按名字造一档 provider；名字没给就用配置里的默认档。取不到返回 None（调用方报错）。"""
    key = str(name or '').strip() or str(
        (settings or {}).get('translate_provider') or DEFAULT_PROVIDER).strip()
    factory = _REGISTRY.get(key)
    if factory is None:
        return None
    if key == PROVIDER_OFFLINE:
        return OfflineProvider()
    try:
        return factory(settings or {})
    except TypeError:
        return factory()


def providers(settings=None):
    """各档 provider 的可用性（**只报事实，不编话术**）：给 `get_translate_providers` 用。

    `configured=False` 的那一档现在就是不能用，`missing` 里给出缺哪个配置键，
    界面据此说「请先在配置里填写服务地址」而不是让用户猜。
    """
    conf = merged_settings(settings)
    default = str(conf.get('translate_provider') or DEFAULT_PROVIDER)
    rows = []
    for name in provider_names():
        try:
            provider = get_provider(name, conf)
        except Exception as error:
            logging.exception('translate: 构造 provider %s 失败', name)
            rows.append({'name': name, 'configured': False, 'default': name == default,
                         'missing': [], 'error': '%s: %s' % (type(error).__name__, error)})
            continue
        if provider is None:
            continue
        missing = [key for key in getattr(provider, 'needs', ())
                   if not str(conf.get(key) or '').strip()]
        rows.append({'name': name,
                     'configured': bool(getattr(provider, 'configured', True)) and not missing,
                     'default': name == default,
                     'missing': missing,
                     'url': _safe_url(conf.get('translate_url') or '') if name == PROVIDER_HTTP else '',
                     'needs_key': bool(conf.get('translate_api_key')) if name == PROVIDER_HTTP else False})
    for row in rows:
        if row['name'] == PROVIDER_OFFLINE:
            row['glossary'] = glossary_size()
    return rows


# ==================== 配置 ====================
def _as_float(value, default):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _as_int(value, default):
    try:
        out = int(float(value))
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def read_config_settings(path=None):
    """从配置文件里只挑 `translate_*` 那几个键。读不到就回空字典（用默认值）。

    不走 `api.BaseApi.load_config`：那要拿整个 Api 实例，而这一层在命令行/验收里
    也得能独立跑。配置文件是同一份（`core.paths.CONFIG_FILE`），键名一致，
    所以「用户在设置界面填的地址」和「自测里写的地址」是同一个入口。
    """
    target = path or CONFIG_FILE
    out = {}
    try:
        with open(target, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return out
    except Exception as error:
        logging.info('translate: 配置文件读不了（%s），按默认值走', error)
        return out
    if isinstance(data, dict):
        for key in CONFIG_KEYS:
            if key in data:
                out[key] = data[key]
    return out


def merged_settings(extra=None):
    """默认值 ← 配置文件 ← 调用方显式传入，一层层覆盖上来。"""
    out = dict(SETTINGS_DEFAULTS)
    out.update(read_config_settings())
    for key, value in (extra or {}).items():
        if key in CONFIG_KEYS and value is not None:
            out[key] = value
    out['translate_timeout'] = _as_float(out.get('translate_timeout'), DEFAULT_TIMEOUT)
    out['translate_batch_max'] = min(_as_int(out.get('translate_batch_max'), MAX_BATCH), 500)
    return out


# ==================== 缓存 ====================
_CACHE_LOCK = threading.RLock()
_MEM = {}            # (provider, key) -> 译文：进程内第一层，省掉重复读盘
_FILES = {}          # (provider, target) -> {key: 译文}
_SAVED = {}          # (provider, target) -> 落盘时的条数，用来判断要不要再写


def _safe_name(value):
    return re.sub(r'[^0-9A-Za-z._-]', '_', str(value or ''))[:40] or 'x'


def _cache_file(provider_name, target):
    return os.path.join(CACHE_DIR, 'tr-%s-%s.json' % (_safe_name(provider_name),
                                                      _safe_name(target)))


def _cache_load(provider_name, target):
    bucket = (provider_name, target)
    with _CACHE_LOCK:
        if bucket in _FILES:
            return _FILES[bucket]
        box = {}
        path = _cache_file(provider_name, target)
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
            if isinstance(data, dict) and data.get('v') == CACHE_VERSION:
                rows = data.get('rows')
                if isinstance(rows, dict):
                    # 存的是 [译文, 该句覆盖率或 None]。覆盖率必须一起存：
                    # 命中缓存的那一句如果报不出覆盖率，整页的 coverage 就得变成「不知道」，
                    # 界面就会在「明明翻过」的文件上显示没数字。
                    box = {str(k): (list(v) if isinstance(v, (list, tuple)) else [str(v), None])
                           for k, v in rows.items()}
        except FileNotFoundError:
            pass
        except Exception as error:
            logging.info('translate: 缓存读不了（%s），本次不用磁盘缓存', error)
        _FILES[bucket] = box
        _SAVED[bucket] = len(box)
        return box


def _cache_key(source, text):
    return '%s%s%s' % (str(source or 'auto'), CACHE_KEY_SEP, text)


def cache_get(provider_name, source, target, text):
    """命中返回 `(译文, 覆盖率或 None)`，没命中返回 None。**只缓存成功的译文**。"""
    key = _cache_key(source, text)
    mem = (provider_name, key)
    with _CACHE_LOCK:
        if mem in _MEM:
            return _MEM[mem]
    box = _cache_load(provider_name, target)
    with _CACHE_LOCK:
        value = box.get(key)
        if value is not None:
            _MEM[mem] = value
        return value


def cache_put(provider_name, source, target, text, translation, coverage=None):
    if not str(translation or '').strip() and str(text or '').strip():
        return                       # 空译文不进缓存（下次还得真问一遍）
    key = _cache_key(source, text)
    box = _cache_load(provider_name, target)
    with _CACHE_LOCK:
        box[key] = [translation, coverage]
        _MEM[(provider_name, key)] = box[key]


def cache_flush():
    """把内存里攒的译文写回磁盘缓存文件（超上限丢最旧的一半）。"""
    for bucket, box in list(_FILES.items()):
        with _CACHE_LOCK:
            if _SAVED.get(bucket) == len(box):
                continue
            provider_name, target = bucket
            path = _cache_file(provider_name, target)
            rows = dict(box)
            if len(rows) > CACHE_LIMIT:
                rows = dict(list(rows.items())[-(CACHE_LIMIT // 2):])
                box.clear()
                box.update(rows)
            payload = {'v': CACHE_VERSION, 'provider': provider_name,
                       'target': target, 'rows': rows}
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                tmp = path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                os.replace(tmp, path)
                _SAVED[bucket] = len(box)
            except Exception as error:
                logging.info('translate: 缓存写不下去（%s），只留内存里', error)


def cache_clear():
    """清掉全部翻译缓存（验收脚本之间要能互相隔离，别拿上一轮的缓存冒充「翻好了」）。"""
    with _CACHE_LOCK:
        _MEM.clear()
        _FILES.clear()
        _SAVED.clear()


# ==================== 对外的主入口 ====================
def target_supported(target):
    """目标语言必须在软件支持的那十种里。

    这一条不放宽：认不出来的代码硬翻下去，用户只会拿到一版「谁也不知道是什么」的译文。
    """
    return str(target or '') in TARGET_LANGS


def _cancelled(name, source, target, requests, cached, translated, pending):
    """取消时的返回形状：`translated` 是真的已经翻好的条数，**不是**「全都翻完了」。

    已翻好的那部分照旧进缓存（下次接着跑不浪费），但 success 一定是 False——
    界面/批处理只有看到 False 才会说「停在第几页」而不是报「翻译完成」。
    """
    return {'success': False, 'error': 'cancelled', 'provider': name,
            'target': target, 'source': source,
            'requests': requests, 'cached': cached,
            'translated': len(translated), 'pending': pending,
            'detail': {'translated': len(translated), 'pending': pending},
            'done': dict(translated)}


def translate_texts(texts, target, source='auto', provider='', settings=None,
                    should_stop=None, use_cache=True):
    """把一组文本翻成 target，返回译文和如实的统计。

    成功：`{'success': True, 'provider', 'target', 'source', 'results': [{'src','dst'}],
    'count', 'cached', 'requests', 'coverage', 'unmatched', 'note'}`
      * `results` 与传进来的 `texts` **同长度同顺序**（空串/纯空白原样交回，不计覆盖率）；
      * `coverage` 只在 provider 自己能报的时候有（offline 词表必给，http 看服务方给不给）；
      * `unmatched` = 译文与原文一模一样的条数（兜底档里就是「没翻到」的条数）。
    失败：`{'success': False, 'error': 码, 'detail': 原料, 'provider': 档名}`——
      一句译文都不编，也不返回空的 `results` 冒充翻完。中途取消时带 `translated` 已翻好的条数。
    """
    started = time.monotonic()
    if isinstance(texts, str):
        rows_in = [texts]
    elif isinstance(texts, (list, tuple)):
        rows_in = [str(x) if x is not None else '' for x in texts]
    elif texts is None:
        rows_in = []
    else:
        rows_in = [str(texts)]
    if not rows_in:
        return _error('no_texts')
    if not any(raw.strip() for raw in rows_in):
        # 全是空串/纯空白：没有任何要翻的东西。这时候「成功」等于弹一句「翻译完成」而什么都没做，
        # 所以和空列表同一档处理（也是「空文本边界不崩、不编译文」那条要求的意思）。
        return _error('no_texts', detail={'received': len(rows_in)})
    target = str(target or '').strip()
    if not target_supported(target):
        return _error('unsupported_target', target=target, supported=list(TARGET_LANGS))

    conf = merged_settings(settings)
    name = str(provider or '').strip() or str(conf.get('translate_provider') or DEFAULT_PROVIDER)
    prov = get_provider(name, conf)
    if prov is None:
        return _error('unknown_provider', provider=name, known=provider_names())
    if name == PROVIDER_HTTP and not conf.get('translate_url'):
        # 提前判一次：这样「没配服务地址」的报错不带任何网络尝试，快且可复现
        return _error('no_url', setting='translate_url', provider=name)

    batch_max = int(conf.get('translate_batch_max') or MAX_BATCH)
    source = str(source or 'auto').strip() or 'auto'
    translated = {}
    cache_hits = 0
    requests = 0
    coverages = []          # 这一趟真问回来的逐句覆盖率
    cached_coverages = []   # 从缓存里捡回来的逐句覆盖率（同一个数存过一次就够了）
    pending = []
    seen = set()
    for raw in rows_in:
        if not raw.strip():
            continue
        if raw in seen:
            continue
        seen.add(raw)
        hit = cache_get(name, source, target, raw) if use_cache else None
        if hit is None:
            pending.append(raw)
        else:
            translated[raw] = hit[0]
            cache_hits += 1
            # 命中缓存的那一句把当初记下的覆盖率一起捡回来（None 就是「当时也算不出来」）
            if hit[1] is not None:
                cached_coverages.append(float(hit[1]))

    for start in range(0, len(pending), batch_max):
        if should_stop is not None and should_stop():
            cache_flush()
            return _cancelled(name, source, target, requests, cache_hits, translated,
                              len(pending) - start)
        chunk = pending[start:start + batch_max]
        payload = prov.translate_batch(chunk, source, target)
        requests += 1
        if not isinstance(payload, dict) or not payload.get('ok'):
            cache_flush()
            box = payload if isinstance(payload, dict) else {}
            # 失败形状：码在顶层（界面按码分支），服务方给的原料在 detail，
            # 「已经翻好几句」和「试了几次」也一并带回去——用户停在哪、要不要换服务，靠这两个数。
            result = {'success': False, 'error': box.get('error') or 'provider_failed',
                      'provider': name, 'target': target, 'source': source,
                      'requests': requests, 'cached': cache_hits,
                      'translated': len(translated)}
            if box.get('detail') is not None:
                result['detail'] = box['detail']
            if box.get('attempts') is not None:
                result['attempts'] = box['attempts']
            return result
        rows_out = payload.get('results') or []
        if len(rows_out) != len(chunk):
            cache_flush()
            return _error('bad_payload', provider=name,
                          detail={'reason': 'want %d got %d' % (len(chunk), len(rows_out))})
        # provider 若报 coverage 就必须与 texts 逐位对齐；对不齐就当它没报（不猜、不错位记到别的句子上）
        chunk_cov = payload.get('coverage')
        cov_ok = isinstance(chunk_cov, list) and len(chunk_cov) == len(chunk)
        for pos, (src, item) in enumerate(zip(chunk, rows_out)):
            if not isinstance(item, dict):
                cache_flush()
                return _error('bad_payload', provider=name,
                              detail={'reason': 'result is not an object'})
            dst = item.get('dst')
            if not isinstance(dst, str) or (not dst.strip() and src.strip()):
                cache_flush()
                return _error('bad_payload', provider=name,
                              detail={'reason': 'missing translation', 'src': src[:60]})
            translated[src] = dst
            unit = float(chunk_cov[pos]) if cov_ok and chunk_cov[pos] is not None else None
            if unit is not None:
                coverages.append(unit)
            if use_cache:
                cache_put(name, source, target, src, dst, unit)
    if use_cache:
        cache_flush()
    if should_stop is not None and should_stop():
        # 最后一批刚跑完时点的取消：结果是真的，但必须如实标 cancelled，
        # 否则调用方会以为整本翻完了（进度条也会停在「100%」）
        cache_flush()
        return _cancelled(name, source, target, requests, cache_hits, translated, 0)

    results = []
    for raw in rows_in:
        if not raw.strip():
            results.append({'src': raw, 'dst': raw, 'blank': True})
        else:
            results.append({'src': raw, 'dst': translated.get(raw, raw)})
    unmatched = sum(1 for r in results
                    if not r.get('blank') and r['dst'] == r['src'])
    known = coverages + cached_coverages
    return {'success': True, 'provider': name, 'target': target, 'source': source,
            'results': results, 'count': len(results),
            'cached': cache_hits, 'requests': requests,
            'coverage': round(sum(known) / len(known), 4) if known else None,
            'unmatched': unmatched,
            'note': getattr(prov, 'note', '') or (
                'fallback_glossary' if name == PROVIDER_OFFLINE else ''),
            'elapsed_ms': int((time.monotonic() - started) * 1000)}
