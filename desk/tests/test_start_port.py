import socket
from django.test import SimpleTestCase
from scripts.start_local import check_port_available


class LocalStartupPortTests(SimpleTestCase):
    def test_live_listener_still_prevents_startup(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with self.assertRaisesMessage(RuntimeError, f'Порт {port} занят'):
                check_port_available(port)

    def test_closed_http_connection_does_not_prevent_immediate_restart(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
                peer, _ = listener.accept()
                peer.close()  # Server closes first, retaining its TCP TIME_WAIT.
                self.assertEqual(client.recv(1), b'')
        check_port_available(port)
