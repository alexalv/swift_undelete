#!/usr/bin/env python

import unittest
from swift.common import swob
from swift_undelete import middleware as md


class FakeApp(object):
    def __init__(self):
        self.responses = []  # fill in later
        self._calls = []

    def __call__(self, env, start_response):
        req = swob.Request(env)

        self._calls.append((
            req.method, req.path,
            # mutable dict; keep a copy so subsequent calls can't change it
            swob.HeaderKeyDict(req.headers)))

        if len(self.responses) > 1:
            resp = self.responses.pop(0)
        else:
            resp = self.responses[0]

        status = resp['status']
        headers = resp.get('headers', [])
        body_iter = resp.get('body_iter', [])
        start_response(status, headers)
        return body_iter

    @property
    def calls(self):
        """
        Returns the calls received by this application as a list of
        (method, path) pairs.
        """
        return [x[:2] for x in self._calls]

    @property
    def calls_with_headers(self):
        """
        Returns the calls received by this application as a list of
        (method, path, headers) tuples.
        """
        return self._calls


class TestMiddleware(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.undelete = md.filter_factory({})(self.app)

    def call_mware(self, req, expect_exception=False):
        status = [None]
        headers = [None]

        def start_response(s, h, ei=None):
            status[0] = s
            headers[0] = h

        body_iter = self.undelete(req.environ, start_response)
        body = ''
        caught_exc = None
        try:
            for chunk in body_iter:
                body += chunk
        except Exception as exc:
            if expect_exception:
                caught_exc = exc
            else:
                raise

        if expect_exception:
            return status[0], headers[0], body, caught_exc
        else:
            return status[0], headers[0], body

    def test_account_passthrough(self):
        """
        Account requests are passed through unmodified.
        """
        self.app.responses = [{'status': '200 OK'}]

        req = swob.Request.blank('/v1/a')
        req.method = 'DELETE'

        status, _, _ = self.call_mware(req)
        self.assertEqual(status, "200 OK")
        self.assertEqual(self.app.calls, [('DELETE', '/v1/a')])

    def test_container_passthrough(self):
        """
        Container requests are passed through unmodified.
        """
        self.app.responses = [{'status': '200 OK'}]
        req = swob.Request.blank('/v1/a/c')
        req.method = 'DELETE'

        status, _, _ = self.call_mware(req)
        self.assertEqual(status, "200 OK")
        self.assertEqual(self.app.calls, [('DELETE', '/v1/a/c')])
