from django import forms


class HistoricoFiltroForm(forms.Form):
    material = forms.CharField(label='Nome do material', max_length=150, required=False)
    ciclo = forms.CharField(label='ID do ciclo', max_length=50, required=False)
    inicio = forms.DateTimeField(
        label='De', required=False,
        input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S'],
        widget=forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}),
    )
    fim = forms.DateTimeField(
        label='Até', required=False,
        input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S'],
        widget=forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('inicio') and cleaned.get('fim') and cleaned['inicio'] > cleaned['fim']:
            raise forms.ValidationError('A data inicial deve ser anterior à data final.')
        return cleaned
