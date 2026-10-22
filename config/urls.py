from django.contrib import admin
from django.contrib.auth import views as auth
from django.urls import path
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from desk import views

# Administration includes credentials binding names, so staff alone is not sufficient.
admin.site.has_permission = lambda request: request.user.is_active and request.user.is_superuser
admin.site.site_header = 'Voice · Администрирование'
urlpatterns = [
    path('health/', views.health, name='health'),
    path('admin/', admin.site.urls),
    path('login/', auth.LoginView.as_view(), name='login'),
    path('logout/', auth.LogoutView.as_view(), name='logout'),
    path('', views.inbox, name='inbox'),
    path('threads/<int:pk>/', views.thread_detail, name='thread'),
    path('threads/<int:pk>/source/', views.thread_source, name='thread_source'),
    path('threads/<int:pk>/action/', views.thread_action, name='thread_action'),
    path('products/', views.products, name='products'),
    path('products/<int:pk>/', views.product_detail, name='product'),
    path('voc/', views.voc, name='voc'),
    path('imports/', views.imports, name='imports'),
    path('integrations/', views.integrations, name='integrations'),
    path('tone/', views.tone, name='tone'),
    path('incidents/<int:pk>/', views.incident_detail, name='incident'),
] + staticfiles_urlpatterns()
