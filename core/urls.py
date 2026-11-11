from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('api/dados/', views.api_dados_scada, name='api_dados'),
    path('api/scans/', views.listar_scans, name='listar_scans'),
    path('api/material/', views.salvar_nome_material, name='salvar_nome_material'),
    path('exportar/csv/', views.exportar_csv, name='exportar_csv'),
    path('exportar/excel/', views.exportar_excel, name='exportar_excel'),
]
