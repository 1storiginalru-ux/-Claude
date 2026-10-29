from django.contrib import admin
from django.contrib.auth import views as auth
from django.urls import path
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from desk import views
from desk.replies import views as reply_views

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
    path('threads/<int:pk>/assessment/', views.review_assessment, name='review_assessment'),
    path('products/', views.products, name='products'),
    path('products/<int:pk>/', views.product_detail, name='product'),
    path('products/<int:pk>/rules/', views.product_rules, name='product_rules'),
    path('voc/', views.voc, name='voc'),
    path('imports/', views.imports, name='imports'),
    path('integrations/', views.integrations, name='integrations'),
    path('tone/', views.tone, name='tone'),
    path('ai/', views.ai_queue, name='ai_queue'),
    path('replies/', reply_views.drafts, name='reply_drafts'),
    path('replies/materials/', reply_views.materials, name='reply_materials'),
    path('replies/materials/<int:pk>/', reply_views.materials, name='reply_material_edit'),
    path('replies/rules/', reply_views.rules, name='reply_rules'),
    path('replies/rules/<int:pk>/', reply_views.rules, name='reply_rule_edit'),
    path('replies/knowledge/', reply_views.knowledge, name='reply_knowledge'),
    path('replies/knowledge/<int:pk>/', reply_views.knowledge, name='reply_knowledge_edit'),
    path('replies/tests/', reply_views.tests, name='reply_tests'),
    path('replies/tests/<int:pk>/run/', reply_views.test_run, name='reply_test_run'),
    path('replies/tests/results/<int:pk>/', reply_views.test_feedback, name='reply_test_feedback'),
    path('incidents/<int:pk>/', views.incident_detail, name='incident'),
] + staticfiles_urlpatterns()
