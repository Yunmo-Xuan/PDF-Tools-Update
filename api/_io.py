"""保存输出文件时用到的共用小工具。"""

import contextlib
import os
import tempfile


def same_path(a, b):
    """判断两个路径是否指向同一个文件（忽略大小写与相对的写法差异）。"""
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:
        return False


def _tmp_beside(target):
    directory = os.path.dirname(os.path.abspath(target))
    fd, tmp = tempfile.mkstemp(suffix=".tmp", dir=directory)
    return fd, tmp


def save_pdf_doc(doc, output_path, source=None, **kwargs):
    """
    保存并关闭 PyMuPDF 文档。

    PyMuPDF 不允许把文档直接保存回它自己打开的那个文件（只允许增量保存），
    而当输出路径与源文件相同时，Windows 又会因为源文件仍被占用而拒绝替换
    （WinError 5）。所以顺序必须是：
        存临时文件 -> 关闭文档 -> 原子替换
    先把 doc 关掉再替换，两者缺一不可。

    source 是给「成品是刚新建的文档」那种情况用的：缩放（F17）用 show_pdf_page
    从源文件搬内容，这份新文档会一直握着源文件的句柄，doc.name 却是空的——
    只靠 doc.name 判断就会漏掉「存回原文件」这一路（实测报
    code=2: cannot remove file ... Permission denied）。调用方把读进来的
    那个路径传进来，走的就是下面同一条临时文件 + 原子替换。

    注意：本函数负责关闭 doc，调用方不要再自己 close。
    """
    origin = source or getattr(doc, "name", None)
    if origin and same_path(origin, output_path):
        fd, tmp = _tmp_beside(output_path)
        os.close(fd)
        try:
            try:
                doc.save(tmp, **kwargs)
            finally:
                doc.close()
            os.replace(tmp, output_path)
        except Exception:
            # 保存失败和替换失败都要清掉临时文件，否则用户目录里会越攒越多孤儿 .tmp
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    else:
        try:
            doc.save(output_path, **kwargs)
        finally:
            doc.close()


def close_pdf(doc):
    """出错路径上安全关闭文档。"""
    try:
        doc.close()
    except Exception:
        pass


@contextlib.contextmanager
def atomic_output(output_path):
    """
    以二进制方式打开一个临时文件供写入，正常退出后才替换到目标路径。

    一律走临时文件，不分「输出是否等于源文件」：直接 open(target, 'wb') 会先把
    目标文件截断，写到一半抛异常就只剩一个几字节的空壳，用户原来那份好文件也没了。
    """
    target = os.path.abspath(output_path)
    fd, tmp = _tmp_beside(target)
    handle = os.fdopen(fd, "wb")
    try:
        yield handle
        handle.close()
        os.replace(tmp, target)
    except Exception:
        try:
            handle.close()
        except Exception:
            pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def unique_output_path(path):
    """
    程序自动起的名字不许撞掉用户已有的文件。

    走「保存为」对话框的路径不用这个函数——那是用户自己点名要覆盖的文件。
    """
    base, ext = os.path.splitext(path)
    candidate = path
    index = 2
    while os.path.exists(candidate):
        candidate = f"{base}_{index}{ext}"
        index += 1
    return candidate
