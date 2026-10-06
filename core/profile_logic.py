class ProfileLogic:
    """
    Encapsula as regras de geometria, agora baseadas em dois níveis estáticos (Borda e Centro)
    sem dependência da posição X (Zona de Borda ignorada).
    """
    
    # Defaults estáticos de segurança
    ESPESSURA_CENTRO = 10.0
    ESPESSURA_BORDA = 2.0
    TOLERANCIA_POSITIVA = 0.5

    @classmethod
    def get_specs(cls, esp_borda=None, centro=None, tol=None):
        try:
            from .models import SystemConfiguration
            config = SystemConfiguration.get_config()
            def_centro = config.espessura_centro_nominal
            def_borda = config.espessura_borda_nominal
            def_tol = config.tolerancia_positiva
        except Exception:
            def_centro = cls.ESPESSURA_CENTRO
            def_borda = cls.ESPESSURA_BORDA
            def_tol = cls.TOLERANCIA_POSITIVA

        E_Centro = float(centro) if centro is not None and str(centro).strip() != "" else def_centro
        E_Borda = float(esp_borda) if esp_borda is not None and str(esp_borda).strip() != "" else def_borda
        t = float(tol) if tol is not None and str(tol).strip() != "" else def_tol

        return {
            'borda': {'alvo': E_Borda, 'min': E_Borda, 'max': E_Borda + t},
            'centro': {'alvo': E_Centro, 'min': E_Centro, 'max': E_Centro + t}
        }

    @classmethod
    def validar_ponto(cls, x, y, esp_borda=None, centro=None, tol=None):
        """
        Valida o ponto comparando-o com o nível mais próximo (Borda ou Centro).
        O 'Limites' retornados serão os do nível identificado.
        """
        specs = cls.get_specs(esp_borda, centro, tol)
        
        # Identificar qual nível o ponto Y está mais próximo
        dist_borda = abs(y - specs['borda']['alvo'])
        dist_centro = abs(y - specs['centro']['alvo'])
        
        # Decisão do Nível Ativo baseada na proximidade vertical
        if dist_borda < dist_centro:
            ativo = specs['borda']
            zona_detectada = "Borda (Nível)"
        else:
            ativo = specs['centro']
            zona_detectada = "Centro (Nível)"
            
        # Validação
        is_ok = (y >= ativo['min'] and y <= ativo['max'])
        
        return {
            'is_ok': is_ok,
            'alvo': round(ativo['alvo'], 3),
            'lim_min': round(ativo['min'], 3),
            'lim_max': round(ativo['max'], 3),
            'zona': zona_detectada
        }
