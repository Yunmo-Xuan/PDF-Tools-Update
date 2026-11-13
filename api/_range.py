"""页码范围字符串的解析。

「1-5, 8, 11-13」这种写法在拆分、文本导出、图片提取、涂黑、书签几处都要用，
实现只留这一份：各处自己解析迟早会出现「拆分页码有效、导出文本无效」这种对不上的行为。
"""


def parse_page_range(spec, total):
    """
    解析成升序去重的 1-based 页码列表；认不出来的片段直接跳过。

    宽容解析是 6.0.4 定下来的口径（用户在输入框里多打个空格不该报错），
    空结果由调用方决定怎么提示，见 select_pages()。
    """
    pages = set()
    for part in str(spec or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                a, b = part.split('-', 1)
                a, b = int(a.strip()), int(b.strip())
            except Exception:
                continue
            # 倒序区间（如 5-3）原实现会得到空集合然后静默写出空 PDF，
            # 这里显式按小→大处理
            if a > b:
                a, b = b, a
            # 先夹到 [1, total] 再展开：不夹的话「1-999999999」会先把三个亿个
            # 整数塞进集合才去筛，界面就卡在桥线程上直到内存耗尽，且取消不了
            a = max(a, 1)
            b = min(b, total)
            if a > b:
                continue
            pages.update(range(a, b + 1))
        else:
            try:
                pages.add(int(part))
            except Exception:
                continue
    return sorted(p for p in pages if 1 <= p <= total)


def select_pages(spec, total):
    """
    新能力统一的入口：返回 (页码列表, 错误信息)。

    空范围表示「整个文档」——这是用户点的「全部页面」的语义，不是漏填；
    但填了却一页都没落在文档内属于输入错误，必须报出来而不是闷声导出一个空文件。
    """
    text = str(spec or "").strip()
    if not text or text.lower() in ("all", "全部", "全部页面"):
        return list(range(1, total + 1)), None
    pages = parse_page_range(text, total)
    if not pages:
        return None, f"页码范围「{text}」没有落在文档内（文档共 {total} 页）"
    return pages, None
