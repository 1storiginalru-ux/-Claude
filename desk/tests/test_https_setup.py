"""Exercise server activation and rollback without touching server paths or ACME."""
import argparse
import os
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase
from deploy import enable_https as setup
from deploy.environment import read_environment

DOMAIN = 'voice.example.ru'
IP = '192.0.2.1'
ORIGINAL = b'# preserve comments\nDJANGO_SETTINGS_MODULE=config.production\nDJANGO_ALLOWED_HOSTS=localhost,127.0.0.1\nVOC_HTTPS=0\nVOC_YANDEX_AI_KEY="literal $(command) `command` $HOME"\nPGPASSWORD="private-test-only"\n'


class HttpsSetupTests(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.paths = {key: self.root / value for key,value in {
            'ENV':'etc/voice/voice.env', 'SITE':'nginx/sites-available/voice.conf',
            'LINK':'nginx/sites-enabled/voice.conf', 'WEBROOT':'www/acme', 'LE':'letsencrypt'}.items()}
        for path in self.paths.values(): path.parent.mkdir(parents=True, exist_ok=True)
        for key,value in self.paths.items():
            context = patch.object(setup,key,value); context.start(); self.addCleanup(context.stop)
        setup.ENV.write_bytes(ORIGINAL); setup.ENV.chmod(0o600)
        self.output = StringIO()
        self.commands = []

    def command(self, *command, capture=False):
        self.commands.append(command)
        if command[:2] == ('certbot','certonly'):
            folder=setup.LE/'live'/DOMAIN; folder.mkdir(parents=True,exist_ok=True)
            (folder/'fullchain.pem').write_text('fixture-cert');(folder/'privkey.pem').write_text('fixture-key')
        return SimpleNamespace(stdout='')

    def request(self, domain, port, path, **kwargs):
        if port==80 and path.startswith('/.well-known/acme-challenge/'):return 200,[]
        if port==80:return 301,[('Location','https://'+domain+'/')]
        return 200,[('Strict-Transport-Security','max-age=86400'),('Set-Cookie','csrftoken=fixture; Path=/; Secure; SameSite=Lax')]

    def activate(self, command=None, request=None):
        with patch.object(setup,'preflight'), patch.object(setup,'wait_for_web'), \
                patch.object(setup,'run',side_effect=command or self.command), \
                patch.object(setup,'local_request',side_effect=request or self.request), redirect_stdout(self.output):
            setup.activate(DOMAIN,IP)

    def test_domain_validation_rejects_config_and_path_injection(self):
        self.assertEqual(setup.domain_name('Voice.Example.RU'),DOMAIN)
        for value in ['https://voice.example.ru','*.example.ru','voice.example.ru;','a..ru','a\n.ru','../example.ru','-a.example.ru','a_.example.ru']:
            with self.subTest(value=value),self.assertRaises(argparse.ArgumentTypeError):setup.domain_name(value)

    def test_readiness_waits_for_nginx_reload_before_reporting_failure(self):
        with patch.object(setup,'local_request',side_effect=[OSError('not ready'),(404,[]),(200,[])]) as request, \
             patch.object(setup.time,'sleep') as sleep:
            self.assertEqual(setup.wait_for_response(DOMAIN,443,'/health/',tls=True),(200,[]))
            self.assertEqual(request.call_count,3);self.assertEqual(sleep.call_count,2)

    def test_redirect_waits_until_old_bootstrap_workers_stop_serving_http(self):
        with patch.object(setup,'local_request',side_effect=[(503,[]),(301,[('Location','https://'+DOMAIN+'/')])]) as request, \
             patch.object(setup.time,'sleep') as sleep:
            setup.wait_for_redirect(DOMAIN)
            self.assertEqual(request.call_count,2);sleep.assert_called_once_with(.25)

    def test_wrong_redirect_is_not_accepted_and_includes_observed_response(self):
        with patch.object(setup,'local_request',return_value=(301,[('Location','http://other.example.ru/')])) as request, \
             patch.object(setup.time,'sleep'):
            with self.assertRaisesMessage(ValueError,'HTTP 301, Location=') as error:
                setup.wait_for_redirect(DOMAIN)
            self.assertIn('http://other.example.ru/',str(error.exception))
            self.assertEqual(request.call_count,40)

    def test_activation_recovers_from_transient_bootstrap_response_after_https_is_ready(self):
        attempts=[]
        def request(domain,port,path,**kwargs):
            if port==80 and path=='/':
                attempts.append(path)
                if len(attempts)==1:return 503,[]
            return self.request(domain,port,path,**kwargs)
        with patch.object(setup.time,'sleep'):
            self.activate(request=request)
        self.assertEqual(len(attempts),2)
        self.assertTrue(setup.LINK.is_symlink())
        self.assertEqual(read_environment(setup.ENV)['VOC_HTTPS'],'1')
        self.assertEqual(self.commands.count(('systemctl','restart','voice-web')),1)

    def test_environment_changes_only_https_keys_and_keeps_literal_secrets(self):
        content=setup.environment_bytes(DOMAIN)
        target=self.root/'result.env';setup.atomic_write(target,content,0o600)
        env=read_environment(target)
        self.assertEqual(env['VOC_YANDEX_AI_KEY'],'literal $(command) `command` $HOME')
        self.assertEqual(env['PGPASSWORD'],'private-test-only')
        self.assertEqual(env['VOC_HTTPS'],'1')
        self.assertIn(DOMAIN,env['DJANGO_ALLOWED_HOSTS'].split(','))
        self.assertEqual(env['DJANGO_CSRF_TRUSTED_ORIGINS'],'https://'+DOMAIN)
        self.assertEqual(target.stat().st_mode&0o777,0o600)
        self.assertEqual(setup.ENV.read_bytes(),ORIGINAL)
        setup.ENV.write_bytes(content)
        self.assertEqual(setup.environment_bytes(DOMAIN),content)

    def test_another_site_using_the_same_domain_is_detected(self):
        configuration='# configuration file /etc/nginx/other.conf:\nserver { server_name '+DOMAIN+'; }\n'
        self.assertEqual(setup.conflicting_site(configuration,DOMAIN),'/etc/nginx/other.conf')
        own='# configuration file '+str(setup.LINK)+':\nserver_name '+DOMAIN+';\n'
        setup.SITE.write_bytes(setup.nginx_site(DOMAIN,tls=False));setup.LINK.symlink_to(setup.SITE)
        self.assertIsNone(setup.conflicting_site(own,DOMAIN))
        self.assertIsNone(setup.conflicting_site(configuration.replace('server_name '+DOMAIN,'# server_name '+DOMAIN),DOMAIN))

    def test_bootstrap_does_not_publish_login_without_tls(self):
        bootstrap=setup.nginx_site(DOMAIN,tls=False).decode()
        self.assertIn('acme-challenge',bootstrap);self.assertIn('return 503',bootstrap)
        self.assertNotIn('proxy_pass',bootstrap)
        config=setup.nginx_site(DOMAIN,tls=True).decode()
        self.assertIn('POST $binary_remote_addr',config)
        self.assertIn('^/(login|admin/login)/$',config)
        self.assertIn('proxy_set_header X-Forwarded-For $remote_addr;',config)
        self.assertIn('proxy_set_header X-Forwarded-Proto $scheme;',config)
        self.assertIn('return 301 https://'+DOMAIN+'$request_uri;',config)

    def test_success_writes_private_backup_and_renewal_hook_without_restarting_workers(self):
        self.activate()
        self.assertEqual(read_environment(setup.ENV)['VOC_HTTPS'],'1')
        self.assertTrue(setup.LINK.is_symlink())
        hook=setup.LE/'renewal-hooks'/'deploy'/('voice-'+DOMAIN)
        self.assertTrue(hook.read_text().startswith('#!/bin/sh\n'+setup.MARKER+'; domain='+DOMAIN+'\n'))
        self.assertEqual(hook.stat().st_mode&0o777,0o755)
        backups=list((setup.ENV.parent/'backups').iterdir());self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].stat().st_mode&0o777,0o700)
        self.assertEqual((backups[0]/'0').read_bytes(),ORIGINAL)
        self.assertEqual((backups[0]/'0').stat().st_mode&0o777,0o600)
        self.assertEqual([cmd for cmd in self.commands if cmd[:2]==('systemctl','restart')],[('systemctl','restart','voice-web')])
        self.assertIn(('certbot','renew','--cert-name',DOMAIN,'--dry-run','--run-deploy-hooks'),self.commands)
        self.assertNotIn('private-test-only',self.output.getvalue())

    def test_acme_failure_restores_http_config_without_restarting_web(self):
        def command(*cmd,**kwargs):
            if cmd[:2]==('certbot','certonly'):raise subprocess.CalledProcessError(1,cmd)
            return self.command(*cmd,**kwargs)
        with self.assertRaises(subprocess.CalledProcessError):self.activate(command)
        self.assertEqual(setup.ENV.read_bytes(),ORIGINAL)
        self.assertFalse(setup.SITE.exists() or setup.LINK.is_symlink())
        self.assertFalse(any(cmd[:2]==('systemctl','restart') for cmd in self.commands))

    def test_keyboard_interrupt_also_restores_configuration(self):
        def command(*cmd,**kwargs):
            if cmd[:2]==('certbot','certonly'):raise KeyboardInterrupt()
            return self.command(*cmd,**kwargs)
        with self.assertRaises(KeyboardInterrupt):self.activate(command)
        self.assertEqual(setup.ENV.read_bytes(),ORIGINAL)
        self.assertFalse(setup.SITE.exists() or setup.LINK.is_symlink())

    def test_failed_login_security_check_restores_previous_active_site_and_settings(self):
        old=setup.nginx_site(DOMAIN,tls=True);setup.SITE.write_bytes(old);setup.LINK.symlink_to(setup.SITE)
        def request(domain,port,path,**kwargs):return 200,[('Strict-Transport-Security','max-age=86400')]
        with self.assertRaisesMessage(ValueError,'cookie'):self.activate(request=request)
        self.assertEqual(setup.ENV.read_bytes(),ORIGINAL);self.assertEqual(setup.SITE.read_bytes(),old)
        self.assertTrue(setup.LINK.is_symlink())
        self.assertEqual(self.commands.count(('systemctl','restart','voice-web')),2)

    def test_dry_run_failure_keeps_verified_https_running(self):
        def command(*cmd,**kwargs):
            if cmd[:2]==('certbot','renew'):raise subprocess.CalledProcessError(1,cmd)
            return self.command(*cmd,**kwargs)
        with self.assertRaises(subprocess.CalledProcessError):self.activate(command)
        self.assertEqual(read_environment(setup.ENV)['VOC_HTTPS'],'1')
        self.assertIn('listen 443 ssl;',setup.SITE.read_text())
        self.assertTrue(setup.LINK.is_symlink())
        self.assertEqual(self.commands.count(('systemctl','restart','voice-web')),1)
        self.assertIn('HTTPS уже работает',self.output.getvalue())

    def test_repeat_run_does_not_replace_active_tls_with_bootstrap(self):
        self.activate();original=setup.SITE.read_bytes();seen=[]
        def command(*cmd,**kwargs):
            if cmd==('systemctl','reload','nginx'):seen.append(setup.SITE.read_bytes())
            return self.command(*cmd,**kwargs)
        self.activate(command)
        self.assertTrue(all(config==original for config in seen))

    def test_wrong_dns_and_unmanaged_files_abort_before_changes(self):
        hook=setup.LE/'renewal-hooks'/'deploy'/('voice-'+DOMAIN)
        real_stat=Path.stat
        def root_owned(path,*args,**kwargs):
            result=real_stat(path,*args,**kwargs)
            if path==setup.ENV:
                fields=list(result);fields[4]=0;return os.stat_result(fields)
            return result
        with patch.object(setup.os,'geteuid',return_value=0),patch.object(setup.shutil,'which',return_value='/fixture'), \
             patch.object(Path,'stat',new=root_owned),patch.object(setup,'wait_for_web'), \
             patch.object(setup,'run',return_value=SimpleNamespace(stdout='')) as run:
            with patch.object(setup.socket,'getaddrinfo',return_value=[(None,None,None,None,('192.0.2.2',443))]):
                with self.assertRaisesMessage(ValueError,'DNS'):setup.preflight(DOMAIN,IP,hook)
                run.assert_not_called()
            with patch.object(setup.socket,'getaddrinfo',return_value=[(None,None,None,None,(IP,443))]):
                setup.SITE.write_text('manual site, preserve')
                with self.assertRaisesMessage(ValueError,'не принадлежит'):setup.preflight(DOMAIN,IP,hook)
                self.assertEqual(setup.SITE.read_text(),'manual site, preserve')
                setup.SITE.write_bytes(setup.nginx_site(DOMAIN,tls=False))
                hook.parent.mkdir(parents=True)
                hook.write_text('#!/bin/sh\n'+setup.MARKER+'; domain='+DOMAIN+'\n')
                setup.preflight(DOMAIN,IP,hook)
