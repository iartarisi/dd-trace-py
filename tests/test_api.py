import mock
import re
import socket
import threading
import time
import warnings

from unittest import TestCase

import pytest

from tests.test_tracer import get_dummy_tracer
from ddtrace.api import API, Response
from ddtrace.compat import iteritems, httplib, PY3
from ddtrace.vendor.six.moves import BaseHTTPServer


class _BaseHTTPRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    error_message_format = '%(message)s\n'
    error_content_type = 'text/plain'

    @staticmethod
    def log_message(format, *args):  # noqa: A002
        pass


class _TimeoutAPIEndpointRequestHandlerTest(_BaseHTTPRequestHandler):
    def do_PUT(self):
        # This server sleeps longer than our timeout
        time.sleep(5)


class _ResetAPIEndpointRequestHandlerTest(_BaseHTTPRequestHandler):

    def do_PUT(self):
        return


_HOST = '0.0.0.0'
_TIMEOUT_PORT = 8743
_RESET_PORT = _TIMEOUT_PORT + 1


def _make_server(port, request_handler):
    server = BaseHTTPServer.HTTPServer((_HOST, port), request_handler)
    t = threading.Thread(target=server.serve_forever)
    # Set daemon just in case something fails
    t.daemon = True
    t.start()
    return server, t


@pytest.fixture(scope='module')
def endpoint_test_timeout_server():
    server, thread = _make_server(_TIMEOUT_PORT, _TimeoutAPIEndpointRequestHandlerTest)
    try:
        yield thread
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture(scope='module')
def endpoint_test_reset_server():
    server, thread = _make_server(_RESET_PORT, _ResetAPIEndpointRequestHandlerTest)
    try:
        yield thread
    finally:
        server.shutdown()
        thread.join()


class ResponseMock:
    def __init__(self, content, status=200):
        self.status = status
        self.content = content

    def read(self):
        return self.content


class APITests(TestCase):

    def setUp(self):
        # DEV: Mock here instead of in tests, before we have patched `httplib.HTTPConnection`
        self.conn = mock.MagicMock(spec=httplib.HTTPConnection)
        self.api = API('localhost', 8126)

    def tearDown(self):
        del self.api
        del self.conn

    def test_typecast_port(self):
        api = API('localhost', u'8126')
        self.assertEqual(api.port, 8126)

    @mock.patch('logging.Logger.debug')
    def test_parse_response_json(self, log):
        tracer = get_dummy_tracer()
        tracer.debug_logging = True

        test_cases = {
            'OK': dict(
                js=None,
                log='Cannot parse Datadog Agent response, please make sure your Datadog Agent is up to date',
            ),
            'OK\n': dict(
                js=None,
                log='Cannot parse Datadog Agent response, please make sure your Datadog Agent is up to date',
            ),
            'error:unsupported-endpoint': dict(
                js=None,
                log='Unable to parse Datadog Agent JSON response: .*? \'error:unsupported-endpoint\'',
            ),
            42: dict(  # int as key to trigger TypeError
                js=None,
                log='Unable to parse Datadog Agent JSON response: .*? 42',
            ),
            '{}': dict(js={}),
            '[]': dict(js=[]),

            # Priority sampling "rate_by_service" response
            ('{"rate_by_service": '
             '{"service:,env:":0.5, "service:mcnulty,env:test":0.9, "service:postgres,env:test":0.6}}'): dict(
                js=dict(
                    rate_by_service={
                        'service:,env:': 0.5,
                        'service:mcnulty,env:test': 0.9,
                        'service:postgres,env:test': 0.6,
                    },
                ),
            ),
            ' [4,2,1] ': dict(js=[4, 2, 1]),
        }

        for k, v in iteritems(test_cases):
            log.reset_mock()

            r = Response.from_http_response(ResponseMock(k))
            js = r.get_json()
            assert v['js'] == js
            if 'log' in v:
                log.assert_called_once()
                msg = log.call_args[0][0] % log.call_args[0][1:]
                assert re.match(v['log'], msg), msg

    @mock.patch('ddtrace.compat.httplib.HTTPConnection')
    def test_put_connection_close(self, HTTPConnection):
        """
        When calling API._put
            we close the HTTPConnection we create
        """
        HTTPConnection.return_value = self.conn

        with warnings.catch_warnings(record=True) as w:
            self.api._put('/test', '<test data>', 1)

            self.assertEqual(len(w), 0, 'Test raised unexpected warnings: {0!r}'.format(w))

        self.conn.request.assert_called_once()
        self.conn.close.assert_called_once()

    @mock.patch('ddtrace.compat.httplib.HTTPConnection')
    def test_put_connection_close_exception(self, HTTPConnection):
        """
        When calling API._put raises an exception
            we close the HTTPConnection we create
        """
        HTTPConnection.return_value = self.conn
        # Ensure calling `request` raises an exception
        self.conn.request.side_effect = Exception

        with warnings.catch_warnings(record=True) as w:
            with self.assertRaises(Exception):
                self.api._put('/test', '<test data>', 1)

            self.assertEqual(len(w), 0, 'Test raised unexpected warnings: {0!r}'.format(w))

        self.conn.request.assert_called_once()
        self.conn.close.assert_called_once()


def test_flush_connection_timeout_connect():
    payload = mock.Mock()
    payload.get_payload.return_value = 'foobar'
    payload.length = 12
    api = API(_HOST, 2019)
    response = api._flush(payload)
    if PY3:
        assert isinstance(response, (OSError, ConnectionRefusedError))  # noqa: F821
    else:
        assert isinstance(response, socket.error)
    assert response.errno in (99, 111)


def test_flush_connection_timeout(endpoint_test_timeout_server):
    payload = mock.Mock()
    payload.get_payload.return_value = 'foobar'
    payload.length = 12
    api = API(_HOST, _TIMEOUT_PORT)
    response = api._flush(payload)
    assert isinstance(response, socket.timeout)


def test_flush_connection_reset(endpoint_test_reset_server):
    payload = mock.Mock()
    payload.get_payload.return_value = 'foobar'
    payload.length = 12
    api = API(_HOST, _RESET_PORT)
    response = api._flush(payload)
    if PY3:
        assert isinstance(response, (httplib.BadStatusLine, ConnectionResetError))  # noqa: F821
    else:
        assert isinstance(response, httplib.BadStatusLine)
