import logging

logger = logging.getLogger(__name__)

SCADA_UNMANAGED_TABLES = {
    'datapoints',
    'pointvalues',
    'pointvalueannotations',
}

class ScadaRouter:
    """
    Roteador de Banco de Dados Multi-DB para isolamento seguro do Scada-LTS.

    Diretrizes de Segurança Industrial:
    1. Modelos do Scada-LTS (managed=False) são lidos exclusivamente do banco 'scada'.
    2. Qualquer tentativa de escrita nos modelos do Scada-LTS lança PermissionError imediato.
    3. Migrações ('manage.py migrate') são terminantemente proibidas no banco 'scada'.
    4. Modelos gerenciados locais (LeituraScada, SystemConfiguration, auth, etc.) residem em 'default'.
    """

    def _is_scada_model(self, model):
        db_table = getattr(model._meta, 'db_table', '').lower()
        is_unmanaged = not getattr(model._meta, 'managed', True)
        return is_unmanaged or (db_table in SCADA_UNMANAGED_TABLES)

    def db_for_read(self, model, **hints):
        if self._is_scada_model(model):
            return 'scada'
        return 'default'

    def db_for_write(self, model, **hints):
        if self._is_scada_model(model):
            msg = f"Escrita bloqueada: O modelo '{model._meta.label}' do Scada-LTS é estritamente somente-leitura."
            logger.critical(msg)
            raise PermissionError(msg)
        return 'default'

    def allow_relation(self, obj1, obj2, **hints):
        is_scada1 = self._is_scada_model(obj1)
        is_scada2 = self._is_scada_model(obj2)
        if is_scada1 != is_scada2:
            return False
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Bloqueio absoluto: NENHUMA migração é permitida no banco 'scada'
        if db == 'scada':
            return False

        # Se houver hints com model
        model = hints.get('model')
        if model is not None and self._is_scada_model(model):
            return False

        # Modelos locais migram exclusivamente no banco 'default'
        return db == 'default'
