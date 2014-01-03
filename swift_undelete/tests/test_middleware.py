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


class MiddlewareTestCase(unittest.TestCase):
    """
    Just a base class for other test cases. Some setup, some utility methods.
    Nothing too exciting.
    """
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

        headerdict = swob.HeaderKeyDict(headers[0])
        if expect_exception:
            return status[0], headerdict, body, caught_exc
        else:
            return status[0], headerdict, body


class TestPassthrough(MiddlewareTestCase):
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


class TestObjectDeletion(MiddlewareTestCase):
    def test_deleting_nonexistent_object(self):
        pass

    def test_copy_to_existing_trash_container(self):
        self.app.responses = [
            # COPY request
            {'status': '201 Created',
             'headers': [('X-Sir-Not-Appearing-In-This-Response', 'yup')]},
            # DELETE request
            {'status': '204 No Content',
             'headers': [('X-Decadation', 'coprose')]}]

        req = swob.Request.blank('/v1/MY_account/cats/kittens.jpg')
        req.method = 'DELETE'

        status, headers, body = self.call_mware(req)
        self.assertEqual(status, "204 No Content")
        # the client gets whatever the DELETE coughed up
        self.assertNotIn('X-Sir-Not-Appearing-In-This-Response', headers)
        self.assertEqual(headers['X-Decadation'], 'coprose')

        self.assertEqual(2, len(self.app.calls))

        # First, we performed a COPY request to save the object into the trash.
        method, path, headers = self.app.calls_with_headers[0]
        self.assertEqual(method, 'COPY')
        self.assertEqual(path, '/v1/MY_account/cats/kittens.jpg')
        self.assertEqual(headers['Destination'], '.trash-cats/kittens.jpg')

        # Second, we actually perform the DELETE request (and send that
        # response to the client unaltered)
        method, path, headers = self.app.calls_with_headers[1]
        self.assertEqual(method, 'DELETE')
        self.assertEqual(path, '/v1/MY_account/cats/kittens.jpg')

    def test_delete_from_trash(self):
        self.app.responses = [{'status': '204 No Content'}]

        req = swob.Request.blank('/v1/a/.trash-borkbork/bork')
        req.method = 'DELETE'

        status, headers, body = self.call_mware(req)
        self.assertEqual(status, "204 No Content")
        self.assertEqual(self.app.calls,
                         [('DELETE', '/v1/a/.trash-borkbork/bork')])
