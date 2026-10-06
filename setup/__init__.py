import sys
import pymysql

pymysql.install_as_MySQLdb()

# Compatibilidade Django com Python 3.14 (correção de copy(super()) em BaseContext)
if sys.version_info >= (3, 14):
    import django.template.context as _dtc

    def _safe_basecontext_copy(self):
        duplicate = self.__class__.__new__(self.__class__)
        duplicate.__dict__.update(self.__dict__)
        duplicate.dicts = self.dicts[:]
        return duplicate

    _dtc.BaseContext.__copy__ = _safe_basecontext_copy
