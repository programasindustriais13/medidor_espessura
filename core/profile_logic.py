class ProfileLogic:
    """
    Encapsula as regras de geometria, agora baseadas em dois níveis estáticos (Borda e Centro)
    sem dependência da posição X (Zona de Borda ignorada).
    """
    
    # Defaults
    ESPESSURA_CENTRO = 10.0
    ESPESSURA_BORDA = 2.0
    TOLERANCIA_POSITIVA = 0.5
    
    @classmethod
    def get_specs(cls, esp_borda=None, centro=None, tol=None):
        E_Centro = float(centro) if centro is not None else cls.ESPESSURA_CENTRO
        E_Borda = float(esp_borda) if esp_borda is not None else cls.ESPESSURA_BORDA
        t = float(tol) if tol is not None else cls.TOLERANCIA_POSITIVA
        
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
